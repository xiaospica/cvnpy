"""ModelRegistry — 轻量 manifest 校验 + filter_config 加载.

subprocess 模式下主进程不加载模型本体, 只需在 on_init 时:
1. 确认 bundle_dir 存在且含 params.pkl + task.json
2. 读 manifest.json 校验 bundle_version
3. 读 filter_config.json (Phase 2 强制要求) 校验 filter_id ↔ filter_chain 一致性
4. 缓存供 webtrader / engine 查询 (不 import qlib)

模型 mtime 检测 / 热刷新不在主进程做 — 每次子进程启动时自然重新加载.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


SUPPORTED_BUNDLE_VERSIONS = {1}
FILTER_CONFIG_SCHEMA_VERSIONS = {1}


class BundleIncompatibleError(RuntimeError):
    """bundle_version 不在 SUPPORTED_BUNDLE_VERSIONS 里."""


class FilterConfigError(RuntimeError):
    """filter_config.json 缺失 / schema 错 / id↔chain 不一致."""


def _validate_filter_id_chain(filter_id: str, filter_chain: List[Dict[str, Any]]) -> None:
    """启动期一致性校验: filter_id 必须以 chain name 序列结尾.

    与 strategy_dev/config.py 的 validate_filter_id_chain_consistency 同一规则,
    在实盘端独立实现 (避免反向依赖训练侧 strategy_dev 包).
    """
    suffix = "_".join(f["name"] for f in filter_chain)
    if not filter_id.endswith(suffix):
        raise FilterConfigError(
            f"filter_id={filter_id!r} 不以 chain name 序列 {suffix!r} 结尾; "
            f"chain: {[f['name'] for f in filter_chain]}. "
            f"约定: filter_id = '{{universe}}_{{'_'.join(f.name for f in chain)}}'"
        )


class ModelRegistry:
    """主进程侧的 bundle 元数据缓存."""

    def __init__(self):
        self._manifests: Dict[str, Dict[str, Any]] = {}
        # bundle_dir → filter_config dict (Phase 2 强制存在)
        self._filter_configs: Dict[str, Dict[str, Any]] = {}

    def register(self, bundle_dir: str) -> Dict[str, Any]:
        """校验 bundle 并缓存 manifest + filter_config. 返回 manifest dict.

        校验项:
        - bundle_dir 存在
        - params.pkl + task.json + filter_config.json 齐全
        - manifest.json (若存在) 的 bundle_version 在支持列表里
        - filter_config.json schema_version 兼容
        - filter_config.json filter_id ↔ filter_chain 一致

        Phase 2 强制 filter_config.json 存在: 老 bundle 缺失时 raise 提示用
        ``scripts/backfill_filter_config.py --apply`` 一次性迁移.
        """
        p = Path(bundle_dir)
        if not p.exists() or not p.is_dir():
            raise FileNotFoundError(f"bundle dir not found: {bundle_dir}")

        required = ["params.pkl", "task.json"]
        missing = [f for f in required if not (p / f).exists()]
        if missing:
            raise FileNotFoundError(f"bundle {bundle_dir} missing files: {missing}")

        manifest_path = p / "manifest.json"
        manifest: Dict[str, Any] = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            version = manifest.get("bundle_version")
            if version not in SUPPORTED_BUNDLE_VERSIONS:
                raise BundleIncompatibleError(
                    f"bundle_version={version} not in supported set {SUPPORTED_BUNDLE_VERSIONS}"
                )

        # Phase 2: 强制读 filter_config.json
        filter_cfg_path = p / "filter_config.json"
        if not filter_cfg_path.exists():
            raise FilterConfigError(
                f"bundle {bundle_dir} 缺 filter_config.json (Phase 2 跨端 filter 契约). "
                f"老 bundle 一次性迁移: 在 qlib_strategy_dev 工程跑 "
                f"`E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe "
                f"scripts/backfill_filter_config.py --apply`"
            )
        try:
            filter_cfg = json.loads(filter_cfg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise FilterConfigError(f"读 {filter_cfg_path} 失败: {exc}") from exc

        # schema 校验
        schema_v = filter_cfg.get("schema_version")
        if schema_v not in FILTER_CONFIG_SCHEMA_VERSIONS:
            raise FilterConfigError(
                f"filter_config.json schema_version={schema_v} 不在支持列表 "
                f"{FILTER_CONFIG_SCHEMA_VERSIONS}"
            )

        filter_id = filter_cfg.get("filter_id") or ""
        filter_chain = filter_cfg.get("filter_chain") or []
        if not filter_id or not isinstance(filter_chain, list) or not filter_chain:
            raise FilterConfigError(
                f"filter_config.json 缺 filter_id 或 filter_chain: {filter_cfg}"
            )
        _validate_filter_id_chain(filter_id, filter_chain)

        self._manifests[str(p)] = manifest
        self._filter_configs[str(p)] = filter_cfg
        return manifest

    def get(self, bundle_dir: str) -> Optional[Dict[str, Any]]:
        return self._manifests.get(str(Path(bundle_dir)))

    def get_filter_config(self, bundle_dir: str) -> Optional[Dict[str, Any]]:
        """返回该 bundle 的 filter_config 字典 (来自 filter_config.json).

        engine.run_inference / run_inference_range 用它派生 snapshot 路径
        ``snapshots/filtered/{filter_id}_{date}.parquet``.
        """
        return self._filter_configs.get(str(Path(bundle_dir)))

    def list_registered(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._manifests)

    def list_filter_configs(self) -> Dict[str, Dict[str, Any]]:
        """返回 {bundle_dir: filter_config} 全部已注册的. 供 MLEngine.list_active_filter_configs 用."""
        return dict(self._filter_configs)
