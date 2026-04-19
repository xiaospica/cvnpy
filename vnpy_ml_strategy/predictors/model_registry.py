"""ModelRegistry — 轻量 manifest 校验.

subprocess 模式下主进程不加载模型本体, 只需在 on_init 时:
1. 确认 bundle_dir 存在且含 params.pkl + task.json
2. 读 manifest.json (如果有) 校验 bundle_version 与当前主进程期望匹配
3. 缓存 manifest 信息供 webtrader 查询 (不 import qlib)

模型 mtime 检测 / 热刷新不在主进程做 — 每次子进程启动时自然重新加载.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


SUPPORTED_BUNDLE_VERSIONS = {1}


class BundleIncompatibleError(RuntimeError):
    """bundle_version 不在 SUPPORTED_BUNDLE_VERSIONS 里."""


class ModelRegistry:
    """主进程侧的 bundle 元数据缓存."""

    def __init__(self):
        self._manifests: Dict[str, Dict[str, Any]] = {}

    def register(self, bundle_dir: str) -> Dict[str, Any]:
        """校验 bundle 并缓存 manifest. 返回 manifest dict.

        校验项:
        - bundle_dir 存在
        - params.pkl + task.json 齐全
        - manifest.json (若存在) 的 bundle_version 在支持列表里
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

        self._manifests[str(p)] = manifest
        return manifest

    def get(self, bundle_dir: str) -> Optional[Dict[str, Any]]:
        return self._manifests.get(str(Path(bundle_dir)))

    def list_registered(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._manifests)
