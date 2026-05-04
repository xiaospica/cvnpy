# -*- coding: utf-8 -*-
"""
P2-1 实盘 / 模拟双轨架构 — 一键演示脚本

选 csi300_lgb_headless (bundle f6017) 作示例策略, 演示三种验证模式:

  --mode v1   双 sim gateway (验证多 Gateway 路由架构)
              GATEWAYS = [QMT_SIM_csi300_a (sim), QMT_SIM_csi300_b (sim)]
              STRATEGIES = [csi300_v1_a, csi300_v1_b]   两个独立模拟策略
              用途: 验证 R1-R5 多 gateway 路由 / DB 隔离 / EventEngine 不串味

  --mode v2   FakeQmtGateway live + sim shadow (验证 live+sim 双轨 + 信号同步)
              GATEWAYS = [QMT (fake_live), QMT_SIM_csi300_shadow (sim)]
              STRATEGIES = [csi300_v2_live (走 fake QMT), csi300_v2_shadow (signal_source=v2_live)]
              用途: 不依赖真实盘, 验证启动期校验 + 影子策略 selections.parquet 字节级同步

  --mode v3   真 QmtGateway live + sim shadow (盘中, 需券商仿真账户)
              GATEWAYS = [QMT (真 vnpy_qmt.QmtGateway), QMT_SIM_csi300_shadow (sim)]
              STRATEGIES = [csi300_v3_live (走真 QMT), csi300_v3_shadow (signal_source=v3_live)]
              用途: 09:30-15:00 盘中跑, 验证真 miniqmt RPC connect / send_order / 回报路由

启动姿势:
    F:/Program_Home/vnpy/python.exe run_dual_track_demo.py --mode v1
    F:/Program_Home/vnpy/python.exe run_dual_track_demo.py --mode v2
    F:/Program_Home/vnpy/python.exe run_dual_track_demo.py --mode v3 --qmt-account YOUR_PAPER_ACCOUNT

特性:
- 启动前**自动清理**当前 demo 用到的 sim_db / ml_output / replay_history.db (无确认提示)
- 自动 spawn webtrader uvicorn 8001 (mlearnweb 通过此端拉数据)
- 启动后提示如何启 mlearnweb 双 uvicorn + 前端
- Ctrl+C 退出后给出验证 cmd (sim_db 行数 / 信号字节级 / 前端访问入口)

详见 vnpy_ml_strategy/docs/dual_track.md.
"""

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List


# ─── sys.path / env 注入 (与 run_ml_headless.py 同源) ──────────────────────
os.environ["VNPY_DOCK_BACKEND"] = "ads"
_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE / "vendor" / "qlib_strategy_core"
if _CORE_DIR.exists() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

# P0-1/P0-2: load .env (与 run_ml_headless 同源逻辑)
from dotenv import load_dotenv  # noqa: E402

_DOTENV_FILE = os.getenv("DOTENV_FILE")
if _DOTENV_FILE and (_HERE / _DOTENV_FILE).exists():
    load_dotenv(_HERE / _DOTENV_FILE, override=False)
elif (_HERE / ".env.production").exists():
    load_dotenv(_HERE / ".env.production", override=False)
elif (_HERE / ".env").exists():
    load_dotenv(_HERE / ".env", override=False)

_QLIB_SOURCE = Path(os.getenv("QLIB_SOURCE_ROOT", r"F:\Quant\code\qlib_strategy_dev"))
if (_QLIB_SOURCE / "qlib" / "__init__.py").exists() and str(_QLIB_SOURCE) not in sys.path:
    sys.path.insert(0, str(_QLIB_SOURCE))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


# ═══════════════════════════════════════════════════════════════════════════
# 配置常量 (从 .env 读)
# ═══════════════════════════════════════════════════════════════════════════

# 选 csi300_lgb_headless (bundle f6017) 作 demo 示例策略 (与 strategies.production.yaml 一致).
# .env 没配 VNPY_MODEL_ROOT 时报错 (不留兼容默认).
_VNPY_MODEL_ROOT = os.environ.get("VNPY_MODEL_ROOT")
if not _VNPY_MODEL_ROOT:
    raise RuntimeError(
        "VNPY_MODEL_ROOT 未设. 检查 .env / .env.production 是否含此字段."
    )
DEMO_BUNDLE_DIR = os.getenv(
    "DEMO_BUNDLE_DIR",  # demo 专用覆盖, 默认走 .env VNPY_MODEL_ROOT + 默认 run_id
    f"{_VNPY_MODEL_ROOT}/f6017411b44c4c7790b63c5766b93964",
)

# 基础 setting (sim gateway 共享, 与 strategies.production.yaml 中 qmt_sim 同源)
_QS_DATA_ROOT = os.environ.get("QS_DATA_ROOT") or "D:/vnpy_data"
QMT_SIM_BASE_SETTING: Dict[str, Any] = {
    "模拟资金": 1_000_000.0,
    "部分成交率": 0.0,
    "拒单率": 0.0,
    "订单超时秒数": 30,
    "成交延迟毫秒": 0,
    "报单上报延迟毫秒": 0,
    "卖出持仓不足拒单": "是",
    "行情源": "merged_parquet",
    "merged_parquet_merged_root": f"{_QS_DATA_ROOT}/snapshots/merged",
    "merged_parquet_reference_kind": "today_open",
    "merged_parquet_fallback_days": 10,
    "merged_parquet_stale_warn_hours": 48,
    "启用持久化": "是",
    # [A2] 状态文件统一到 ${QS_DATA_ROOT}/state/
    "持久化目录": f"{_QS_DATA_ROOT}/state",
}

# 实盘 miniqmt 默认 setting (V3 用; 资金账号 由 --qmt-account 注入, 路径走 .env)
QMT_SETTING_TEMPLATE: Dict[str, Any] = {
    "资金账号": "",   # ← V3 必填 (--qmt-account)
    "客户端路径": os.getenv(
        "QMT_CLIENT_PATH",
        r"E:/迅投极速交易终端 睿智融科版/userdata_mini",
    ),
}

STRATEGY_BASE_SETTING: Dict[str, Any] = {
    "inference_python": os.environ.get(
        "INFERENCE_PYTHON",
        r"E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe",
    ),
    "provider_uri": f"{_QS_DATA_ROOT}/qlib_data_bin",
    "trigger_time": "21:00",
    "buy_sell_time": "09:26",
    "output_root": os.environ.get("ML_OUTPUT_ROOT", "D:/ml_output"),
    "lookback_days": 60,
    "subprocess_timeout_s": 300,
    "baseline_path": "",
    "monitor_window_days": 30,
    "risk_degree": 0.95,
    "enable_trading": True,
    "topk": 7,
    "n_drop": 1,
    "replay_start_date": "2026-01-27",
    "bundle_dir": DEMO_BUNDLE_DIR,
}

WEBTRADER_HTTP_PORT = 8001


# ═══════════════════════════════════════════════════════════════════════════
# Step 1 · 自动清理状态 (无 confirm, 让 demo 一键跑通)
# ═══════════════════════════════════════════════════════════════════════════

def _cleanup_demo_state(strategy_names: List[str], gateway_names: List[str]) -> None:
    """清理 demo 涉及的 sim_db / ml_output / replay_history.db.

    与 scripts/reset_sim_state.py --all 等价的 inline 实现, 跳过 confirm.
    仅清 demo 涉及到的策略 / gateway, **不影响** 其他用户已有数据.
    """
    print("=" * 60)
    print("Step 1 · 清理 demo 状态")
    print("=" * 60)

    # 1a. sim_db (按 demo gateway_names) — [A2] 已统一到 ${QS_DATA_ROOT}/state
    sim_state_dir = Path(_QS_DATA_ROOT) / "state"
    if sim_state_dir.exists():
        for gw_name in gateway_names:
            for suffix in (".db", ".db-shm", ".db-wal", ".lock"):
                p = sim_state_dir / f"sim_{gw_name}{suffix}"
                if p.exists():
                    try:
                        p.unlink()
                        print(f"  删 {p.name}")
                    except OSError as exc:
                        print(f"  ⚠️ 删 {p.name} 失败: {exc}")

    # 1b. ml_output (按 demo strategy_names)
    ml_output_root = Path(os.getenv("ML_OUTPUT_ROOT", r"D:/ml_output"))
    for strat_name in strategy_names:
        d = ml_output_root / strat_name
        if d.exists():
            try:
                shutil.rmtree(d)
                print(f"  删 {d}")
            except OSError as exc:
                print(f"  ⚠️ 删 {d} 失败: {exc}")

    # 1c. replay_history.db (demo 重置后由 vnpy 端按需重写)
    qs_root = Path(os.getenv("QS_DATA_ROOT", r"D:/vnpy_data"))
    for suffix in ("", "-shm", "-wal"):
        p = qs_root / "state" / f"replay_history.db{suffix}"
        if p.exists():
            try:
                p.unlink()
                print(f"  删 {p}")
            except OSError as exc:
                print(f"  ⚠️ 删 {p} 失败: {exc}")

    # 1d. mlearnweb.db 中 demo strategy 的 replay_settle 行 (避免与上次 demo 数据混淆)
    mlearnweb_db = Path(r"F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db")
    if mlearnweb_db.exists():
        try:
            con = sqlite3.connect(str(mlearnweb_db), timeout=2)
            cur = con.cursor()
            placeholders = ",".join("?" * len(strategy_names))
            n = cur.execute(
                f"DELETE FROM strategy_equity_snapshots "
                f"WHERE source_label='replay_settle' AND strategy_name IN ({placeholders})",
                strategy_names,
            ).rowcount
            con.commit()
            con.close()
            print(f"  删 mlearnweb.db replay_settle 行: {n} (strategy in {strategy_names})")
        except Exception as exc:
            print(f"  ⚠️ 清 mlearnweb.db 失败 (非致命): {exc}")

    print("[cleanup] done\n")


# ═══════════════════════════════════════════════════════════════════════════
# Step 2 · 按 mode 构造 GATEWAYS + STRATEGIES
# ═══════════════════════════════════════════════════════════════════════════

def _build_config(mode: str, qmt_account: str = "") -> Dict[str, Any]:
    """按 --mode 返回 {GATEWAYS, STRATEGIES, gateway_names, strategy_names, label}."""
    if mode == "v1":
        # V1: 双 sim gateway, 各自独立策略 (不影子, 各跑各)
        return {
            "label": "V1 双 sim gateway (验证多 Gateway 路由)",
            "GATEWAYS": [
                {"kind": "sim", "name": "QMT_SIM_csi300_v1_a", "setting": dict(QMT_SIM_BASE_SETTING)},
                {"kind": "sim", "name": "QMT_SIM_csi300_v1_b", "setting": dict(QMT_SIM_BASE_SETTING)},
            ],
            "STRATEGIES": [
                {
                    "strategy_name": "csi300_v1_a",
                    "strategy_class": "QlibMLStrategy",
                    "gateway_name": "QMT_SIM_csi300_v1_a",
                    "setting_override": {"trigger_time": "21:00"},
                },
                {
                    "strategy_name": "csi300_v1_b",
                    "strategy_class": "QlibMLStrategy",
                    "gateway_name": "QMT_SIM_csi300_v1_b",
                    "setting_override": {"trigger_time": "21:15"},  # 错峰
                },
            ],
        }

    elif mode == "v2":
        # V2: FakeQmtGateway (live 命名, sim 内核) + sim shadow (复用上游信号)
        return {
            "label": "V2 FakeQmt + sim shadow (验证 live+sim 双轨 + 信号同步, 无实盘风险)",
            "GATEWAYS": [
                {"kind": "fake_live", "name": "QMT",                          "setting": dict(QMT_SIM_BASE_SETTING)},
                {"kind": "sim",       "name": "QMT_SIM_csi300_v2_shadow",     "setting": dict(QMT_SIM_BASE_SETTING)},
            ],
            "STRATEGIES": [
                {
                    "strategy_name": "csi300_v2_live",
                    "strategy_class": "QlibMLStrategy",
                    "gateway_name": "QMT",   # 走 FakeQmt (命名 validator 走 live 分支)
                    "setting_override": {"trigger_time": "21:00"},
                },
                {
                    "strategy_name": "csi300_v2_shadow",
                    "strategy_class": "QlibMLStrategy",
                    "gateway_name": "QMT_SIM_csi300_v2_shadow",
                    "setting_override": {
                        # ⚠️ 必须与上游 csi300_v2_live 一致 (启动期校验)
                        "topk": 7,
                        "n_drop": 1,
                        "signal_source_strategy": "csi300_v2_live",  # ← 关键: 复用上游信号
                    },
                },
            ],
        }

    elif mode == "v3":
        # V3: 真 QmtGateway live (券商仿真账户) + sim shadow
        if not qmt_account:
            raise ValueError(
                "--mode v3 需要 --qmt-account 参数 (券商仿真账户号). "
                "如还没开通仿真账户, 先用 --mode v2 (无实盘风险)."
            )
        qmt_setting = dict(QMT_SETTING_TEMPLATE)
        qmt_setting["资金账号"] = qmt_account
        return {
            "label": f"V3 真 QmtGateway (账号 {qmt_account}) + sim shadow (盘中跑, 仅 09:30-15:00)",
            "GATEWAYS": [
                {"kind": "live", "name": "QMT",                          "setting": qmt_setting},
                {"kind": "sim",  "name": "QMT_SIM_csi300_v3_shadow",     "setting": dict(QMT_SIM_BASE_SETTING)},
            ],
            "STRATEGIES": [
                {
                    "strategy_name": "csi300_v3_live",
                    "strategy_class": "QlibMLStrategy",
                    "gateway_name": "QMT",
                    "setting_override": {"trigger_time": "21:00"},
                },
                {
                    "strategy_name": "csi300_v3_shadow",
                    "strategy_class": "QlibMLStrategy",
                    "gateway_name": "QMT_SIM_csi300_v3_shadow",
                    "setting_override": {
                        "topk": 7,
                        "n_drop": 1,
                        "signal_source_strategy": "csi300_v3_live",
                    },
                },
            ],
        }

    else:
        raise ValueError(f"unknown mode {mode!r}, 必须是 v1 / v2 / v3 之一")


# ═══════════════════════════════════════════════════════════════════════════
# Step 3 · 启动期硬校验 (与 run_ml_headless._validate_startup_config 同源)
# ═══════════════════════════════════════════════════════════════════════════

def _validate_startup(GATEWAYS: List[Dict], STRATEGIES: List[Dict]) -> None:
    from vnpy_common.naming import validate_gateway_name

    # n_live (含 fake_live) ≤ 1
    n_live = sum(1 for g in GATEWAYS if g["kind"] in ("live", "fake_live"))
    if n_live > 1:
        raise ValueError(
            f"GATEWAYS 含 {n_live} 个 live/fake_live gateway, miniqmt 单进程单账户约束只允许 1 个"
        )

    gw_names = set()
    for gw in GATEWAYS:
        kind = gw["kind"]
        if kind not in ("live", "sim", "fake_live"):
            raise ValueError(f"非法 kind={kind!r}, 必须是 live / sim / fake_live")
        expected_class = "live" if kind in ("live", "fake_live") else "sim"
        validate_gateway_name(gw["name"], expected_class=expected_class)
        gw_names.add(gw["name"])

    # 影子策略与上游一致性
    by_name = {s["strategy_name"]: s for s in STRATEGIES}
    for s in STRATEGIES:
        if s["gateway_name"] not in gw_names:
            raise ValueError(
                f"策略 {s['strategy_name']} 引用的 gateway_name={s['gateway_name']!r} 不存在"
            )
        sso = (s.get("setting_override") or {}).get("signal_source_strategy", "")
        if sso:
            if sso not in by_name:
                raise ValueError(
                    f"策略 {s['strategy_name']!r} signal_source_strategy={sso!r} 不存在"
                )
            upstream = by_name[sso]
            for f in ("topk", "n_drop"):
                us = (upstream.get("setting_override") or {}).get(f, STRATEGY_BASE_SETTING.get(f))
                sv = (s.get("setting_override") or {}).get(f, STRATEGY_BASE_SETTING.get(f))
                if us != sv:
                    raise ValueError(
                        f"影子 {s['strategy_name']!r}.{f}={sv!r} 与上游 {sso!r}.{f}={us!r} 不一致"
                    )

    print(f"[validate] {n_live} live/fake_live, {len(GATEWAYS)-n_live} sim, {len(STRATEGIES)} strategies — 校验通过")


# ═══════════════════════════════════════════════════════════════════════════
# Step 4 · 启动 vnpy 主进程 (复用 run_ml_headless.main 主体, 简化)
# ═══════════════════════════════════════════════════════════════════════════

def _load_gateway_class(kind: str):
    """与 run_ml_headless._load_gateway_class 同源."""
    if kind == "sim":
        from vnpy_qmt_sim import QmtSimGateway
        return QmtSimGateway
    if kind == "live":
        from vnpy_qmt import QmtGateway
        return QmtGateway
    if kind == "fake_live":
        from vnpy_ml_strategy.test.fakes.fake_qmt_gateway import FakeQmtGateway
        return FakeQmtGateway
    raise ValueError(f"unknown kind {kind!r}")


def _start_vnpy_main_engine(GATEWAYS, STRATEGIES):
    """复用 run_ml_headless.main 的核心启动逻辑."""
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 注册 gateway 实例 (按 kind 各自挑类)
    for gw in GATEWAYS:
        cls = _load_gateway_class(gw["kind"])
        print(f"[demo] add_gateway kind={gw['kind']:9s} name={gw['name']:30s} class={cls.__name__}")
        main_engine.add_gateway(cls, gateway_name=gw["name"])

    from vnpy_tushare_pro import TushareProApp
    from vnpy_ml_strategy import MLStrategyApp
    from vnpy_webtrader import WebTraderApp
    from vnpy_webtrader.engine import APP_NAME as WEB_APP_NAME

    main_engine.add_app(TushareProApp)
    main_engine.add_app(MLStrategyApp)
    main_engine.add_app(WebTraderApp)

    # 连 gateway
    for gw in GATEWAYS:
        print(f"[demo] connecting {gw['name']}...")
        main_engine.connect(gw["setting"], gw["name"])
    time.sleep(2)

    # MLEngine
    from vnpy_ml_strategy import APP_NAME as ML_APP_NAME
    ml_engine = main_engine.get_engine(ML_APP_NAME)
    ml_engine.init_engine()
    print(f"[demo] MLEngine ready: classes={ml_engine.get_all_strategy_class_names()}")

    # webtrader RPC + HTTP
    web_engine = main_engine.get_engine(WEB_APP_NAME)
    web_engine.start_server("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
    print("[demo] webtrader RPC server: tcp://127.0.0.1:2014/4102")

    webtrader_proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "uvicorn", "vnpy_webtrader.web:app",
         "--host", "127.0.0.1", "--port", str(WEBTRADER_HTTP_PORT)],
        cwd=str(_HERE),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    print(f"[demo] webtrader HTTP uvicorn pid={webtrader_proc.pid} on http://127.0.0.1:{WEBTRADER_HTTP_PORT}")
    time.sleep(2)

    # 第一轮: add_strategy + init_strategy
    inited = []
    for s in STRATEGIES:
        setting = {**STRATEGY_BASE_SETTING, **s["setting_override"], "gateway": s["gateway_name"]}
        print(f"[demo] adding strategy {s['strategy_name']} → gateway={s['gateway_name']}")
        ml_engine.add_strategy(s["strategy_class"], s["strategy_name"], setting)
        if ml_engine.init_strategy(s["strategy_name"]):
            inited.append(s["strategy_name"])

    # 注入 filter_chain_specs
    from vnpy_tushare_pro.engine import APP_NAME as TS_APP_NAME
    ts_engine = main_engine.get_engine(TS_APP_NAME)
    ts_pipeline = getattr(ts_engine._get_tushare_datafeed(), "daily_ingest_pipeline", None)
    if ts_pipeline is not None:
        specs = ml_engine.list_active_filter_configs()
        ts_pipeline.set_filter_chain_specs(specs)
        print(f"[demo] DailyIngestPipeline filter_chain_specs ← {list(specs.keys())}")

    # 第二轮: start + 触发
    started = []
    for name in inited:
        if ml_engine.start_strategy(name):
            started.append(name)
            print(f"[demo] 立即触发 {name} pipeline (回放从 replay_start_date 起)...")
            ml_engine.run_pipeline_now(name)

    return main_engine, webtrader_proc, started


# ═══════════════════════════════════════════════════════════════════════════
# Step 5 · 提示用户启 mlearnweb + 验证
# ═══════════════════════════════════════════════════════════════════════════

def _print_mlearnweb_hint(strategy_names: List[str]) -> None:
    print("\n" + "=" * 60)
    print("Step 5 · 启动 mlearnweb 后端 + 前端 (另开终端)")
    print("=" * 60)
    print("# 1. 启 mlearnweb 双 uvicorn (research:8000 + live:8100) + 前端 (5173):")
    print("    cd /f/Quant/code/qlib_strategy_dev")
    print("    start_mlearnweb.bat E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe")
    print()
    print("# 2. 等待 ~5 min 让 replay_equity_sync_loop 拉到回放数据后, 浏览器访问:")
    print("    http://localhost:5173/live-trading")
    print(f"    期望: 看到 {len(strategy_names)} 个策略卡片, 名字 = {strategy_names}")
    print("    带 mode badge (实盘红 / 模拟绿), 各自一条权益曲线")


def _print_verification(mode: str, strategy_names: List[str], gateway_names: List[str]) -> None:
    print("\n" + "=" * 60)
    print(f"Step 6 · 退出后验证 cmd ({mode})")
    print("=" * 60)
    sim_state = Path(_QS_DATA_ROOT) / "state"

    print("\n# (a) sim_db 物理隔离 — 各 gateway 独立持仓 / 资金:")
    for gw in gateway_names:
        if gw.startswith("QMT_SIM"):
            print(f"  sqlite3 {sim_state}/sim_{gw}.db \"SELECT COUNT(*) FROM sim_trades\"")
        elif gw == "QMT":
            print(f"  # {gw}: V2 FakeQmt 内部走 sim, 也是 sim_QMT.db")
            print(f"  sqlite3 {sim_state}/sim_{gw}.db \"SELECT COUNT(*) FROM sim_trades\"")

    if mode in ("v2", "v3"):
        upstream = next(s for s in strategy_names if "live" in s)
        shadow = next(s for s in strategy_names if "shadow" in s)
        print(f"\n# (b) 信号同步字节级 (双轨核心): 影子 selections.parquet md5 == 上游")
        print(f"  python -c \"")
        print(f"  import hashlib, pathlib")
        print(f"  for d in pathlib.Path('D:/ml_output/{upstream}').iterdir():")
        print(f"      if not d.is_dir(): continue")
        print(f"      f1 = d / 'selections.parquet'")
        print(f"      f2 = pathlib.Path('D:/ml_output/{shadow}') / d.name / 'selections.parquet'")
        print(f"      if f1.exists() and f2.exists():")
        print(f"          a = hashlib.md5(f1.read_bytes()).hexdigest()")
        print(f"          b = hashlib.md5(f2.read_bytes()).hexdigest()")
        print(f"          print(d.name, 'EQUAL' if a == b else 'DIFFER (BUG!)')")
        print(f"  \"")
        print(f"  # 期望: 全部 EQUAL (双轨信号同步)")

    print("\n# (c) replay_history.db 回放权益已写本地 (A1/B2 解耦):")
    print(f"  sqlite3 D:/vnpy_data/state/replay_history.db \\")
    print(f"    \"SELECT strategy_name, COUNT(*), MIN(ts), MAX(ts) FROM replay_equity_snapshots GROUP BY strategy_name\"")

    print("\n# (d) mlearnweb 拉到 (启 mlearnweb 等 5 min 后):")
    print(f"  sqlite3 F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db \\")
    print(f"    \"SELECT strategy_name, COUNT(*) FROM strategy_equity_snapshots WHERE source_label='replay_settle' GROUP BY strategy_name\"")
    print(f"  # 期望: 每个策略各 ~60 行 (回放 64 个交易日)")


# ═══════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode", choices=["v1", "v2", "v3"], required=True,
        help="V1 双 sim / V2 FakeQmt + sim shadow / V3 真 QMT + sim shadow",
    )
    parser.add_argument(
        "--qmt-account", default="",
        help="V3 模式必填: 券商仿真账户号 (e.g. 1300012345). V1/V2 不需要.",
    )
    parser.add_argument(
        "--no-cleanup", action="store_true",
        help="跳过自动清理 (默认会清 sim_db / ml_output / replay_history.db / mlearnweb.db demo 行)",
    )
    args = parser.parse_args()

    # ─── Step 0: 配置 ─────────────────────────────────────────────────────
    print("=" * 60)
    print(f"P2-1 双轨架构 demo — {args.mode.upper()}")
    print("=" * 60)
    cfg = _build_config(args.mode, qmt_account=args.qmt_account)
    print(f"模式: {cfg['label']}")
    GATEWAYS = cfg["GATEWAYS"]
    STRATEGIES = cfg["STRATEGIES"]
    gateway_names = [g["name"] for g in GATEWAYS]
    strategy_names = [s["strategy_name"] for s in STRATEGIES]
    print(f"GATEWAYS:   {gateway_names}")
    print(f"STRATEGIES: {strategy_names}\n")

    # ─── Step 1: 清理状态 ──────────────────────────────────────────────────
    if not args.no_cleanup:
        _cleanup_demo_state(strategy_names, gateway_names)

    # ─── Step 2: 启动期校验 ────────────────────────────────────────────────
    print("=" * 60)
    print("Step 2 · 启动期硬校验 (命名 / kind / 一致性)")
    print("=" * 60)
    _validate_startup(GATEWAYS, STRATEGIES)

    # ─── Step 3: 启 vnpy 主进程 ───────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Step 3 · 启动 vnpy 主进程 + 多 gateway + 策略 + webtrader HTTP")
    print("=" * 60)
    main_engine, webtrader_proc, started = _start_vnpy_main_engine(GATEWAYS, STRATEGIES)
    if not started:
        print("[demo] 没有策略成功启动, 退出")
        main_engine.close()
        if webtrader_proc.poll() is None:
            webtrader_proc.terminate()
        return 1

    # ─── Step 5: 提示 mlearnweb ────────────────────────────────────────────
    _print_mlearnweb_hint(strategy_names)

    # ─── Step 4: 主循环 (Ctrl+C 退出) ─────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"Step 4 · {len(started)} 策略已就绪. 回放进行中, 等约 1 min batch 推理完成.")
    print(f"           Ctrl+C 退出, 退出后会输出验证 cmd.")
    print("=" * 60)
    stop_flag = {"stop": False}

    def _sigint(_sig, _frm):
        print("\n[demo] 收到 SIGINT, 退出中...")
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _sigint)
    try:
        while not stop_flag["stop"]:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for name in started:
            print(f"[demo] stop_strategy({name})")
            try:
                main_engine.get_engine("MlStrategy").stop_strategy(name)
            except Exception as exc:
                print(f"  ⚠️ stop_strategy 失败: {exc}")
        if webtrader_proc.poll() is None:
            print("[demo] terminate webtrader uvicorn...")
            try:
                webtrader_proc.terminate()
                webtrader_proc.wait(timeout=5)
            except Exception:
                try:
                    webtrader_proc.kill()
                except Exception:
                    pass
        print("[demo] main_engine.close()")
        main_engine.close()

    # ─── Step 6: 验证 cmd ──────────────────────────────────────────────────
    _print_verification(args.mode, strategy_names, gateway_names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
