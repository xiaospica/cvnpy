"""清理 vnpy_qmt_sim 模拟柜台 + ML 推理输出状态，方便回归测试干净起步。

用法:
    F:\\Program_Home\\vnpy\\python.exe scripts\\reset_sim_state.py              # 默认清持久化 + lock，不动 ml_output
    F:\\Program_Home\\vnpy\\python.exe scripts\\reset_sim_state.py --all        # 持久化 + lock + ml_output + replay_history.db 全清（强制重新批量推理）
    F:\\Program_Home\\vnpy\\python.exe scripts\\reset_sim_state.py --dry-run    # 只显示会删什么，不实际删
    F:\\Program_Home\\vnpy\\python.exe scripts\\reset_sim_state.py --gateway QMT_SIM_csi300  # 只清单个 gateway

清的位置:
    1. {trading_state}/sim_*.db / .db-shm / .db-wal / .lock  (账户/持仓/订单 SQLite)
    2. ml_output/{strategy_name}/  (--all 才清；包含 batch 推理产出 + selections)
    3. {QS_DATA_ROOT}/state/replay_history.db  (--all 才清；vnpy 本地回放权益历史,
       Phase 解耦 mlearnweb.db 后由 vnpy 端写本地 + mlearnweb fanout 拉)

不清的位置（给你保留）:
    - daily_merged_*.parquet 行情数据（你 tushare cron 拉的，不该动）
    - mlearnweb mlearnweb.db 训练记录数据库（与模拟柜台无关）
    - 日志文件（.vntrader/log/*）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# [A2] 状态文件统一到 D:/vnpy_data/state/ — 与 replay_history.db 集中, 便于备份.
# 老路径 vnpy_qmt_sim/.trading_state/ 已废弃, 升级时 mv 即可.
def _trading_state_dir() -> Path:
    qs_root = os.getenv("QS_DATA_ROOT", r"D:/vnpy_data")
    return Path(qs_root) / "state"


TRADING_STATE_DIR = _trading_state_dir()
ML_OUTPUT_ROOT = Path(os.getenv("ML_OUTPUT_ROOT", r"D:/ml_output"))

# Phase A1/B2 解耦后,vnpy 端在 {QS_DATA_ROOT}/state/replay_history.db 维护本地
# 回放权益历史,mlearnweb 通过 vnpy_webtrader endpoint 增量 fanout 拉.
# --all 时一并清掉,确保下次回放从空 db 开始 (避免老历史污染权益曲线对比).
def _replay_history_db_path() -> Path:
    qs_root = os.getenv("QS_DATA_ROOT", r"D:/vnpy_data")
    return Path(qs_root) / "state" / "replay_history.db"


def _list_persistence_files(trading_dir: Path, gateway_filter: str | None) -> list[Path]:
    """返回 trading_state/ 下与 gateway 相关的所有 sim_*.db / shm / wal / lock 文件。"""
    if not trading_dir.exists():
        return []
    targets: list[Path] = []
    for p in trading_dir.iterdir():
        if not p.is_file():
            continue
        if not p.name.startswith("sim_"):
            continue
        if gateway_filter and gateway_filter not in p.name:
            continue
        # 限定后缀避免误删
        if not (p.suffix in (".db", ".lock") or p.name.endswith(".db-shm") or p.name.endswith(".db-wal")):
            continue
        targets.append(p)
    return sorted(targets)


def _list_ml_output_dirs(ml_root: Path, gateway_filter: str | None) -> list[Path]:
    if not ml_root.exists():
        return []
    targets: list[Path] = []
    for p in ml_root.iterdir():
        if not p.is_dir():
            continue
        # 策略名通常含市场关键字（csi300/zz500/...），简单 contains 匹配 gateway 后缀
        if gateway_filter:
            sandbox = gateway_filter.replace("QMT_SIM_", "")
            if sandbox and sandbox not in p.name:
                continue
        targets.append(p)
    return sorted(targets)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="同时清 ml_output (推理产出)")
    ap.add_argument("--dry-run", action="store_true", help="只显示要删什么")
    ap.add_argument(
        "--gateway",
        default=None,
        help="只清单个 gateway 的状态，如 QMT_SIM_csi300；缺省清所有 sim_*",
    )
    args = ap.parse_args()

    persist_files = _list_persistence_files(TRADING_STATE_DIR, args.gateway)
    ml_dirs = _list_ml_output_dirs(ML_OUTPUT_ROOT, args.gateway) if args.all else []

    # --all 时把 replay_history.db (+ -shm / -wal) 也列入待删. --gateway 过滤
    # 不适用此 db (它按 strategy_name 列存,所有策略共表;跨策略选择性删需要 SQL,
    # 不在本脚本范围. --gateway 场景下用户应自己 sqlite3 删行).
    replay_db_files: list[Path] = []
    if args.all and not args.gateway:
        replay_db = _replay_history_db_path()
        for suffix in ("", "-shm", "-wal"):
            p = Path(str(replay_db) + suffix)
            if p.exists():
                replay_db_files.append(p)

    print(f"trading_state 目录: {TRADING_STATE_DIR}")
    print(f"  待清文件 ({len(persist_files)}):")
    for p in persist_files:
        size = p.stat().st_size if p.exists() else 0
        print(f"    {p.name:40s} {size/1024:>8.1f} KB")
    if not persist_files:
        print("    (无)")

    if args.all:
        print(f"\nml_output 根: {ML_OUTPUT_ROOT}")
        print(f"  待清子目录 ({len(ml_dirs)}):")
        for d in ml_dirs:
            child_count = sum(1 for _ in d.iterdir())
            print(f"    {d.name:40s} {child_count:>4d} 子项")
        if not ml_dirs:
            print("    (无)")

        print(f"\nreplay_history.db: {_replay_history_db_path()}")
        print(f"  待清文件 ({len(replay_db_files)}):")
        for p in replay_db_files:
            size = p.stat().st_size if p.exists() else 0
            print(f"    {p.name:40s} {size/1024:>8.1f} KB")
        if not replay_db_files:
            print("    (无 — 文件不存在或 --gateway 过滤模式跳过)")

    if args.dry_run:
        print("\n[dry-run] 未实际删除")
        return 0

    if not persist_files and not ml_dirs and not replay_db_files:
        print("\n无可清，退出")
        return 0

    confirm = input("\n确认清理? [y/N]: ").strip().lower()
    if confirm != "y":
        print("已取消")
        return 1

    n_failed = 0
    for p in persist_files:
        try:
            p.unlink()
            print(f"  删 {p.name}")
        except Exception as exc:
            print(f"  删 {p.name} 失败: {exc}")
            n_failed += 1

    for d in ml_dirs:
        try:
            shutil.rmtree(d)
            print(f"  删 {d}")
        except Exception as exc:
            print(f"  删 {d} 失败: {exc}")
            n_failed += 1

    for p in replay_db_files:
        try:
            p.unlink()
            print(f"  删 {p}")
        except Exception as exc:
            print(f"  删 {p} 失败: {exc}")
            n_failed += 1

    print(f"\n完成（{n_failed} 失败）")
    return 0 if n_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
