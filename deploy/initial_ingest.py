"""[P2-2] 一次性历史数据回填脚本.

跑一遍把 tushare 历史数据拉下来 + 生成 filter snapshot + dump qlib bin,
让推理服务器在第一次启动 vnpy_headless 之前就有完整的回放可用数据.

**正常运行的部署机不需要这个脚本** — vnpy_tushare_pro 的 20:00 cron
(由 vnpy_headless 主进程内部调度) 会自动每日拉. 本脚本只在以下两种场景用:
  1. 推理机首次部署: bundle 已 rsync 过来, 但 daily_ingest 从没跑过, qlib_data_bin
     缺历史 → 第一次启动 vnpy_headless 前用此脚本预热.
  2. 灾备恢复: qlib_data_bin / snapshots 误删, 需要从 tushare 重拉一段历史.

用法:
    # 单日 (默认今日):
    F:/Program_Home/vnpy/python.exe deploy/initial_ingest.py
    F:/Program_Home/vnpy/python.exe deploy/initial_ingest.py --date 20260505

    # 范围回填 (上线场景): 起止 YYYYMMDD, 包含两端, 逐日跑
    F:/Program_Home/vnpy/python.exe deploy/initial_ingest.py --from 20260101 --to 20260505

    # 强制覆盖已有 merged 快照 (默认跳过)
    F:/Program_Home/vnpy/python.exe deploy/initial_ingest.py --date 20260505 --force

行为:
    1. 加载 .env.production (走 dotenv, 与 run_ml_headless.py 同源逻辑)
    2. 读 config/strategies.production.yaml, 收集所有 bundle_dir
    3. 读每个 bundle 的 filter_config.json, 聚合 filter_chain_specs
    4. 构造 TushareDatafeedPro (走 vt_setting.json 的 tushare datafeed.password)
    5. set_filter_chain_specs(specs) + ingest_today(date) 逐日跑
    6. 每日打印 stages_done / merged_rows / qlib calendar 末尾日期
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
sys.path.append('..')

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent  # deploy/.. → repo root


def _bootstrap_repo_imports() -> None:
    """与 run_ml_headless.py 同源: vendor / .env / repo root 加 sys.path."""
    os.environ.setdefault("VNPY_DOCK_BACKEND", "ads")

    # 1. repo root → import vnpy_tushare_pro / vnpy_ml_strategy
    if str(_REPO) not in sys.path:
        sys.path.insert(0, str(_REPO))

    # 2. vendor/qlib_strategy_core → qlib runtime (虽然初始 ingest 不直接 import qlib,
    # 但 vnpy_tushare_pro 内部可能间接依赖)
    core_dir = _REPO / "vendor" / "qlib_strategy_core"
    if core_dir.exists() and str(core_dir) not in sys.path:
        sys.path.insert(0, str(core_dir))

    # 3. .env.production 优先, 然后 .env
    try:
        from dotenv import load_dotenv  # noqa: WPS433
    except ImportError:
        # dotenv 缺失就走系统 env 兜底
        return
    for candidate in (".env.production", ".env"):
        p = _REPO / candidate
        if p.exists():
            load_dotenv(p, override=False)
            break


def _load_strategies_yaml() -> dict:
    import yaml
    yaml_path = Path(
        os.getenv("STRATEGIES_CONFIG", "config/strategies.production.yaml")
    )
    if not yaml_path.is_absolute():
        yaml_path = _REPO / yaml_path
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"strategies yaml 不存在: {yaml_path} — "
            f"先拷 config/strategies.example.yaml 后填 bundle_dir"
        )
    text = yaml_path.read_text(encoding="utf-8")
    text = os.path.expandvars(text)
    return yaml.safe_load(text)


def _collect_filter_chain_specs(cfg: dict) -> dict:
    """逐 strategy 读 bundle filter_config.json, 聚合成
    {filter_id: {schema_version, universe, filter_id, filter_chain, ...}}.

    多个策略指向同一 filter_id 时按"后写覆盖" — 因为 filter_config 本身就是
    bundle 自带的, 同 filter_id 在不同 bundle 间应当一致 (训练侧契约).
    """
    specs: dict = {}
    for s in cfg.get("strategies", []) or []:
        bundle_dir = (s.get("setting_override") or {}).get("bundle_dir", "")
        if not bundle_dir:
            print(f"[ingest] 跳过 {s.get('strategy_name')}: 无 bundle_dir")
            continue
        bundle = Path(bundle_dir)
        if not bundle.is_absolute():
            bundle = _REPO / bundle
        fc_path = bundle / "filter_config.json"
        if not fc_path.exists():
            print(f"[ingest] 跳过 {s.get('strategy_name')}: filter_config.json 缺失 ({fc_path})")
            continue
        try:
            spec = json.loads(fc_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[ingest] 跳过 {s.get('strategy_name')}: filter_config.json 解析失败 — {exc}")
            continue
        filter_id = spec.get("filter_id")
        if not filter_id:
            print(f"[ingest] 跳过 {s.get('strategy_name')}: filter_config.json 缺 filter_id 字段")
            continue
        specs[filter_id] = spec
        print(f"[ingest] 收集 filter_id={filter_id} (来自 {s.get('strategy_name')})")
    return specs


def _trade_dates(date_from: str, date_to: str) -> list[str]:
    """简单按自然日生成 [from, to] 闭区间 YYYYMMDD 列表; 非交易日由
    ingest_today 内部 _is_trade_date gate 跳过, 这里不重做日历."""
    d0 = datetime.strptime(date_from, "%Y%m%d").date()
    d1 = datetime.strptime(date_to, "%Y%m%d").date()
    if d0 > d1:
        raise ValueError(f"--from > --to: {date_from} > {date_to}")
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="首次部署 / 灾备恢复时一次性历史数据回填.",
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--date", help="单日 YYYYMMDD (默认今日, 非交易日自动跳过)")
    g.add_argument("--from", dest="date_from", help="范围回填起 YYYYMMDD")
    ap.add_argument("--to", dest="date_to", help="范围回填止 YYYYMMDD (与 --from 配对)")
    ap.add_argument("--force", action="store_true",
                    help="对已有 merged 快照仍重写 (默认跳过 stage 1)")
    args = ap.parse_args()

    if args.date_from and not args.date_to:
        ap.error("--from 需配 --to")
    if args.date_to and not args.date_from:
        ap.error("--to 需配 --from")

    _bootstrap_repo_imports()

    # 必须 .env 加载后再 import (vnpy 的 SETTINGS 在 import 时初始化)
    print("[ingest] 加载 strategies.production.yaml...")
    cfg = _load_strategies_yaml()
    specs = _collect_filter_chain_specs(cfg)
    if not specs:
        print("[ingest] ⚠️ 没收集到任何 filter_chain_specs — strategies yaml 没有指向任何"
              "含 filter_config.json 的 bundle. 终止.")
        return 1
    print(f"[ingest] 共 {len(specs)} 个 filter_id: {list(specs.keys())}")

    print("[ingest] 初始化 TushareDatafeedPro (vt_setting.json datafeed.password)...")
    try:
        from vnpy_tushare_pro import Datafeed
    except Exception as exc:
        print(f"[ingest] ❌ import vnpy_tushare_pro 失败: {exc}")
        print("        检查: vnpy 主 Python 是否装了 vnpy + vnpy_tushare_pro")
        return 2
    dp = Datafeed()
    pipeline = getattr(dp, "daily_ingest_pipeline", None)
    if pipeline is None:
        print("[ingest] ❌ TushareDatafeedPro.daily_ingest_pipeline 未启用; "
              "在 .env.production 设 ML_DAILY_INGEST_ENABLED=1 后重试.")
        return 3
    pipeline.set_filter_chain_specs(specs)

    # 跑日期 — 单日 / 范围
    if args.date_from:
        dates = _trade_dates(args.date_from, args.date_to)
    elif args.date:
        dates = [args.date]
    else:
        dates = [datetime.now().strftime("%Y%m%d")]

    print(f"[ingest] 计划跑 {len(dates)} 个日期: {dates[0]} → {dates[-1]}")

    n_done, n_skipped = 0, 0
    for d in dates:
        print(f"\n[ingest] === {d} ===")
        try:
            r = pipeline.ingest_today(d, force=args.force)
        except Exception as exc:
            print(f"[ingest] ❌ {d} ingest 失败: {exc}")
            continue
        if r.get("skipped"):
            print(f"[ingest] {d} 非交易日, 跳过")
            n_skipped += 1
            continue
        n_done += 1
        print(
            f"[ingest] ✓ {d}: stages={r.get('stages_done')} "
            f"merged={r.get('merged_rows')} "
            f"filtered={r.get('filtered_today_rows')} "
            f"qlib_calendar_last={r.get('qlib_calendar_last_date')} "
            f"({r.get('duration_s', 0):.1f}s)"
        )

    print(f"\n[ingest] 完成: {n_done} 个交易日成功, {n_skipped} 个非交易日跳过.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
