# -*- coding: utf-8 -*-
"""ML 策略无 Qt 启动脚本 — 支持单/多 gateway 多策略沙盒 + 实盘/模拟双轨.

启动姿势:
    F:/Program_Home/vnpy/python.exe run_ml_headless.py

P2-1 双轨架构: GATEWAYS 中每条自带 kind 字段, 按需混部:
    kind="live"      vnpy_qmt.QmtGateway      (真 miniqmt, ≤1 个, 单账户约束)
    kind="sim"       vnpy_qmt_sim.QmtSimGateway (本地撮合, 任意条数)
    kind="fake_live" vnpy_ml_strategy.test.fakes.FakeQmtGateway (开发桩, 仅 V2 验证用)

典型配置:
    模式 A · 全模拟双策略 (默认):
        GATEWAYS = [
            {"kind": "sim", "name": "QMT_SIM_csi300",   "setting": {...}},
            {"kind": "sim", "name": "QMT_SIM_csi300_2", "setting": {...}},
        ]
        每 sim gateway 独立 sim_<name>.db, mlearnweb 前端各自一条权益曲线.

    模式 B · 实盘单策略:
        GATEWAYS = [{"kind": "live", "name": "QMT", "setting": QMT_SETTING}]
        STRATEGIES = [{"gateway_name": "QMT", ...}]

    模式 C · 实盘 + 同信号影子 (P2-1 双轨核心):
        GATEWAYS = [
            {"kind": "live", "name": "QMT",                    "setting": QMT_SETTING},
            {"kind": "sim",  "name": "QMT_SIM_csi300_shadow",  "setting": {...}},
        ]
        STRATEGIES = [
            {"strategy_name": "csi300_live",        "gateway_name": "QMT",                   ...},
            {"strategy_name": "csi300_live_shadow", "gateway_name": "QMT_SIM_csi300_shadow",
             "setting_override": {..., "signal_source_strategy": "csi300_live"}},
        ]
        影子策略复用上游 selections.parquet, 仅撮合走不同 gateway, 用于评估
        模拟柜台真实度 / 实盘 A/B 对照.

与 run_sim.py 的差异:
    - 无 Qt 依赖, 可在 Windows Service / docker 里跑
    - 配置写死在脚本顶部, 不走 UI
    - 默认起 QmtSimGateway (模拟), 不碰真实 QmtGateway, 避免手滑下错单
"""

import os
import signal
import sys
import time
from pathlib import Path


# ─── sys.path 注入 ─────────────────────────────────────────────────────
os.environ["VNPY_DOCK_BACKEND"] = "ads"
_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE / "vendor" / "qlib_strategy_core"
if _CORE_DIR.exists() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
_QLIB_SOURCE = Path(os.getenv("QLIB_SOURCE_ROOT", r"F:\Quant\code\qlib_strategy_dev"))
if (_QLIB_SOURCE / "qlib" / "__init__.py").exists() and str(_QLIB_SOURCE) not in sys.path:
    sys.path.insert(0, str(_QLIB_SOURCE))


# ─── QmtSimGateway 默认参数（模拟 gateway 共享） ───────────────────────
QMT_SIM_BASE_SETTING = {
    "模拟资金": 1_000_000.0,
    "部分成交率": 0.0,
    "拒单率": 0.0,
    "订单超时秒数": 30,
    "成交延迟毫秒": 0,
    "报单上报延迟毫秒": 0,
    "卖出持仓不足拒单": "是",
    "行情源": "merged_parquet",
    "merged_parquet_merged_root": r"D:\vnpy_data\snapshots\merged",
    # today_open：撮合用当日**原始**(未复权) open（对齐"次日 09:30 开盘成交"语义）
    "merged_parquet_reference_kind": "today_open",
    "merged_parquet_fallback_days": 10,
    "merged_parquet_stale_warn_hours": 48,
    "启用持久化": "是",
    "持久化目录": r"F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state",
    # "账户" 字段不写：QmtSimGateway.connect 会用 gateway_name 兜底，
    # 多 gateway 实例之间天然有不同 account_id（独立 SQLite 文件）。
}


# ─── QmtGateway 实盘参数 ────────────────────────────────────────────────
QMT_SETTING = {
    "资金账号": "",
    "客户端路径": r"E:\迅投极速交易终端 睿智融科版\userdata_mini",
}


# ─── Gateways 列表 (P2-1 双轨架构: 每条 gateway 自带 kind) ────────────
# kind="live" → vnpy_qmt.QmtGateway (真 miniqmt, 受 miniqmt 单进程单账户约束 ≤1 个)
# kind="sim"  → vnpy_qmt_sim.QmtSimGateway (本地撮合, 任意条数, 各自独立 sim_<name>.db)
# kind="fake_live" → vnpy_ml_strategy.test.fakes.FakeQmtGateway (无实盘环境时
#                    模拟 'QMT' 命名 + sim 撮合内核的开发桩, 仅 P2-1 V2/V3
#                    验证用, 部署机不安装 vnpy_ml_strategy/test/ 目录)
#
# 双轨混部 (实盘 + 影子 + 独立纸面策略):
#   GATEWAYS = [
#       {"kind": "live",      "name": "QMT",                   "setting": QMT_SETTING},
#       {"kind": "sim",       "name": "QMT_SIM_csi300_paper",  "setting": dict(QMT_SIM_BASE_SETTING)},
#       {"kind": "sim",       "name": "QMT_SIM_csi300_shadow", "setting": dict(QMT_SIM_BASE_SETTING)},
#   ]
#
# 当前默认: 双策略并发模拟 (双 sim gateway 物理隔离). 命名规则见 vnpy_common/naming.py.
GATEWAYS = [
    {"kind": "sim", "name": "QMT_SIM_csi300",   "setting": dict(QMT_SIM_BASE_SETTING)},
    {"kind": "sim", "name": "QMT_SIM_csi300_2", "setting": dict(QMT_SIM_BASE_SETTING)},
    # 进一步扩展示例 (取消注释 + 同步加 STRATEGIES):
    # {"kind": "sim",  "name": "QMT_SIM_zz500",   "setting": dict(QMT_SIM_BASE_SETTING)},
    # {"kind": "live", "name": "QMT",             "setting": QMT_SETTING},  # 实盘混部
]


# ─── ML 策略基础参数（所有策略共用） ───────────────────────────────────
# QS_DATA_ROOT 同时 setenv：MLEngine.run_inference{,_range} 用 os.getenv 自动按
# live_end 拼 snapshots/filtered/csi300_filtered_{date}.parquet 覆盖 task.json
# 训练时点固化的 filter（否则回放到训练截止日之后全部 status=empty）。
os.environ.setdefault("QS_DATA_ROOT", r"D:/vnpy_data")
QS_DATA_ROOT = os.environ["QS_DATA_ROOT"]
VNPY_MODEL_ROOT = os.getenv("VNPY_MODEL_ROOT", r"D:/vnpy_data/models")

STRATEGY_BASE_SETTING = {
    "inference_python": os.getenv(
        "INFERENCE_PYTHON",
        r"E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe",
    ),
    "provider_uri": os.getenv("QS_PROVIDER_URI", f"{QS_DATA_ROOT}/qlib_data_bin"),
    "trigger_time": "21:00",
    "output_root": os.getenv("ML_OUTPUT_ROOT", r"D:/ml_output"),
    "lookback_days": 60,
    "subprocess_timeout_s": 300,
    "baseline_path": "",
    "monitor_window_days": 30,
    # qlib TopkDropoutStrategy 等权 cash 系数
    # 公式：buy_amount = floor(cash × risk_degree / n_buys / open / 100) × 100
    "risk_degree": 0.95,
    # **安全开关 — 默认干跑**
    "enable_trading": True,
}


# ─── 策略列表 ──────────────────────────────────────────────────────────
# 每条策略一个 add_strategy 调用。gateway_name 显式指向 GATEWAYS 中某一条。
#
# 双轨架构 (P2-1):
#   * 实盘策略 → kind=live gateway (真 miniqmt)
#   * 影子策略 → kind=sim gateway, signal_source_strategy=<上游实盘策略名>
#                (复用上游 selections.parquet, 不重复推理, 仅撮合差异)
#   * 独立模拟 → kind=sim gateway, 自己跑推理 (默认行为, signal_source_strategy="")
#
# ⚠️ 多策略 trigger_time 必须错开（推荐间隔 ≥ 10 分钟）— 但**影子策略不跑推理**,
#   不参与本校验 (signal_source_strategy 非空时跳过 trigger_time 检查).
#   单策略推理峰值 4-5 GB; 同 trigger_time 自跑推理的策略并发 → swap/OOM.
#   启动期 _validate_trigger_time_unique() 硬校验; escape hatch:
#   env ALLOW_TRIGGER_TIME_COLLISION=1.

STRATEGIES = [
    {
        # 策略 1: 老 bundle f6017 (训练 run_id)
        "strategy_name": "csi300_lgb_headless",
        "strategy_class": "QlibMLStrategy",
        "gateway_name": "QMT_SIM_csi300",
        "setting_override": {
            "bundle_dir": os.getenv(
                "BUNDLE_DIR",
                r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/f6017411b44c4c7790b63c5766b93964",
            ),
            "topk": 7,
            "n_drop": 1,
            "trigger_time": "21:00",          # 错峰示例:  21:00
            "replay_start_date": "2026-01-27",
        },
    },
    {
        # 策略 2: 新 bundle c38e6c (训练 run_id)
        # 用独立 gateway → 独立 sim_QMT_SIM_csi300_2.db → 与策略 1 资金/持仓不冲突
        "strategy_name": "csi300_lgb_headless_2",
        "strategy_class": "QlibMLStrategy",
        "gateway_name": "QMT_SIM_csi300_2",
        "setting_override": {
            "bundle_dir": os.getenv(
                "BUNDLE_DIR_2",
                r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/c38e6cfdf549446fbb0d637549e4a245",
            ),
            "topk": 7,
            "n_drop": 1,
            "trigger_time": "21:15",          # 错峰示例: 21:15 (与策略 1 间隔 15 min)
            "replay_start_date": "2026-01-27",
        },
    },
    # 双轨示例 (P2-1) — 实盘 + 同信号影子 (要求上面 GATEWAYS 加 kind=live + kind=sim):
    # {
    #     "strategy_name": "csi300_live",
    #     "strategy_class": "QlibMLStrategy",
    #     "gateway_name": "QMT",                              # ← 实盘 gateway
    #     "setting_override": {"bundle_dir": ..., "topk": 7, "n_drop": 1, "trigger_time": "21:00"},
    # },
    # {
    #     "strategy_name": "csi300_live_shadow",
    #     "strategy_class": "QlibMLStrategy",
    #     "gateway_name": "QMT_SIM_csi300_shadow",            # ← sim gateway, 物理隔离
    #     "setting_override": {
    #         "bundle_dir": ...,                              # ← 与上游 csi300_live 同 bundle
    #         "topk": 7, "n_drop": 1,                          # ← 必须与上游一致
    #         "signal_source_strategy": "csi300_live",        # ← 复用上游 selections.parquet
    #     },
    # },
]


TRIGGER_ON_STARTUP = True
ENABLE_WEBTRADER = True
# 是否额外派生一个 uvicorn 子进程跑 vnpy_webtrader.web:app on 8001
# - 8001 = HTTP REST 入口（mlearnweb 通过 vnpy_nodes.yaml 配置的 base_url 拉数据）
# - 与 web_engine.start_server(tcp://2014, tcp://4102) 不同：那个是 RPC，不能给 mlearnweb 用
# 默认 True：开箱即用让 mlearnweb 能直接看到节点和策略
SPAWN_WEBTRADER_HTTP = True
WEBTRADER_HTTP_PORT = 8001


# ─── 主函数 ────────────────────────────────────────────────────────────


def _validate_trigger_time_unique() -> None:
    """启动期硬校验: 避免**自跑推理**的多策略同 trigger_time 触发 OOM.

    单策略推理峰值 4-5 GB; 同 trigger_time 多策略并发 → N×5GB → swap/OOM.

    P2-1 影子策略 (signal_source_strategy 非空) 复用上游 selections.parquet,
    不跑自己的推理 → 不参与本校验, 即使 trigger_time 与上游同也无冲突.

    escape hatch: env ALLOW_TRIGGER_TIME_COLLISION=1 跳过校验.
    """
    if os.getenv("ALLOW_TRIGGER_TIME_COLLISION") == "1":
        print(
            "[headless] WARN: ALLOW_TRIGGER_TIME_COLLISION=1, "
            "跳过 trigger_time 唯一性校验"
        )
        return
    seen: dict[str, str] = {}
    for s in STRATEGIES:
        # 影子策略不跑推理, 跳过 trigger_time 校验
        if (s.get("setting_override") or {}).get("signal_source_strategy"):
            continue
        # 优先 setting_override > STRATEGY_BASE_SETTING > 默认 21:00
        t = (
            (s.get("setting_override") or {}).get("trigger_time")
            or STRATEGY_BASE_SETTING.get("trigger_time")
            or "21:00"
        )
        if t in seen:
            raise ValueError(
                f"策略 {s['strategy_name']!r} 与 {seen[t]!r} trigger_time={t!r} 冲突; "
                "推理峰值 4-5GB, 多策略并发会 OOM. 请错开 ≥ 10 min, 或 "
                "设 env ALLOW_TRIGGER_TIME_COLLISION=1 跳过校验."
            )
        seen[t] = s["strategy_name"]


def _validate_signal_source_consistency() -> None:
    """P2-1: 影子策略 signal_source_strategy 必须与上游 bundle/topk/n_drop 一致.

    不一致 → 信号语义错位 (上游 selections.parquet 的 instrument 集合用错的
    bundle 算出来). 启动期硬校验.
    """
    by_name = {s["strategy_name"]: s for s in STRATEGIES}
    for s in STRATEGIES:
        sso = (s.get("setting_override") or {}).get("signal_source_strategy") or ""
        if not sso:
            continue
        if sso not in by_name:
            raise ValueError(
                f"策略 {s['strategy_name']!r} signal_source_strategy={sso!r} "
                f"不存在 (STRATEGIES 中无此 strategy_name)"
            )
        upstream = by_name[sso]
        if (upstream.get("setting_override") or {}).get("signal_source_strategy"):
            raise ValueError(
                f"策略 {s['strategy_name']!r} signal_source_strategy={sso!r} 本身"
                f"也是影子 (链式依赖). 影子必须直接指向独立推理的上游."
            )
        # 关键字段 bundle_dir / topk / n_drop 必须与上游严格相等
        for f in ("bundle_dir", "topk", "n_drop"):
            us = (upstream.get("setting_override") or {}).get(f)
            sv = (s.get("setting_override") or {}).get(f)
            if us != sv:
                raise ValueError(
                    f"影子策略 {s['strategy_name']!r}.{f}={sv!r} 与上游 "
                    f"{sso!r}.{f}={us!r} 不一致 — 信号会错位."
                )


def _validate_startup_config() -> None:
    """启动前对 GATEWAYS / STRATEGIES 做命名约定与一致性校验。

    详见 vnpy_common/naming.py 模块 docstring。
    任何不合规命名 / 引用不存在的 gateway / 配置错位 → 启动直接 raise.
    """
    from vnpy_common.naming import validate_gateway_name

    # P2-1: 实盘 gateway (kind=live) 受 miniqmt 单进程单账户约束, 至多 1 个.
    n_live = sum(1 for g in GATEWAYS if g.get("kind") == "live")
    if n_live > 1:
        raise ValueError(
            f"GATEWAYS 含 {n_live} 个 kind=live gateway, miniqmt 单进程单账户约束只允许 1 个."
        )

    # P2-1: 每个 gateway 按自己的 kind 校验命名 (混部时 sim+live+fake_live 各自独立)
    gw_names: set[str] = set()
    for gw in GATEWAYS:
        kind = gw.get("kind")
        if kind not in ("live", "sim", "fake_live"):
            raise ValueError(
                f"GATEWAYS 中 {gw.get('name')!r} 的 kind={kind!r} 非法, "
                f"必须是 live / sim / fake_live 之一."
            )
        # fake_live 命名约定为 'QMT' (与真 live 同, 用于命名 validator 走 live 分支)
        expected_class = "live" if kind in ("live", "fake_live") else "sim"
        validate_gateway_name(gw["name"], expected_class=expected_class)
        if gw["name"] in gw_names:
            raise ValueError(f"GATEWAYS 中 name={gw['name']!r} 重复")
        gw_names.add(gw["name"])

    for s in STRATEGIES:
        if s["gateway_name"] not in gw_names:
            raise ValueError(
                f"策略 {s['strategy_name']!r} 引用了未注册的 gateway_name={s['gateway_name']!r}。"
                f"已注册：{sorted(gw_names)}"
            )

    # P1-1: 多策略 trigger_time 错峰硬校验
    _validate_trigger_time_unique()
    # P2-1: 影子策略 signal_source_strategy 与上游一致性校验
    _validate_signal_source_consistency()


def main() -> int:
    _validate_startup_config()

    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # P2-1: 按每条 GATEWAY 自己的 kind 挑类 (混部 sim + live + fake_live).
    # lazy import 避免装了 vnpy_qmt 才能跑 sim 模式 (反之亦然).
    def _load_gateway_class(kind: str):
        if kind == "sim":
            from vnpy_qmt_sim import QmtSimGateway
            return QmtSimGateway
        if kind == "live":
            from vnpy_qmt import QmtGateway
            return QmtGateway
        if kind == "fake_live":
            from vnpy_ml_strategy.test.fakes.fake_qmt_gateway import FakeQmtGateway
            return FakeQmtGateway
        raise ValueError(f"unknown gateway kind: {kind!r}")

    # 注册所有 gateway 实例 (每条按 kind 挑类)
    for gw in GATEWAYS:
        cls = _load_gateway_class(gw["kind"])
        print(f"[headless] add_gateway kind={gw['kind']} name={gw['name']} class={cls.__name__}")
        main_engine.add_gateway(cls, gateway_name=gw["name"])

    # 挂 app
    from vnpy_tushare_pro import TushareProApp
    from vnpy_ml_strategy import MLStrategyApp

    main_engine.add_app(TushareProApp)
    main_engine.add_app(MLStrategyApp)

    if ENABLE_WEBTRADER:
        from vnpy_webtrader import WebTraderApp
        from vnpy_webtrader.engine import APP_NAME as WEB_APP_NAME
        main_engine.add_app(WebTraderApp)

    # 连接所有 gateway
    for gw in GATEWAYS:
        print(f"[headless] connecting gateway {gw['name']}...")
        main_engine.connect(gw["setting"], gw["name"])
    time.sleep(2)

    # 拿 MLEngine
    from vnpy_ml_strategy import APP_NAME as ML_APP_NAME
    ml_engine = main_engine.get_engine(ML_APP_NAME)

    ml_engine.init_engine()
    print(f"[headless] MLEngine registered: {ml_engine.get_all_strategy_class_names()}")

    webtrader_http_proc = None
    if ENABLE_WEBTRADER:
        web_engine = main_engine.get_engine(WEB_APP_NAME)
        web_engine.start_server("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
        print("[headless] webtrader RPC server started on tcp://127.0.0.1:2014 / 4102")

        # 派生 uvicorn 跑 webtrader HTTP REST server (mlearnweb 通过它拉数据)
        # 不嵌入主进程：vnpy_webtrader.web:app 是独立 ASGI app，需要自己的事件循环
        if SPAWN_WEBTRADER_HTTP:
            import subprocess
            webtrader_http_proc = subprocess.Popen(
                [
                    sys.executable, "-u", "-m", "uvicorn",
                    "vnpy_webtrader.web:app",
                    "--host", "127.0.0.1",
                    "--port", str(WEBTRADER_HTTP_PORT),
                ],
                cwd=str(_HERE),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            print(
                f"[headless] webtrader HTTP server (uvicorn) spawned pid={webtrader_http_proc.pid} "
                f"on http://127.0.0.1:{WEBTRADER_HTTP_PORT} "
                f"— mlearnweb 通过此端点拉数据"
            )
            time.sleep(2)  # 给 uvicorn 启动时间
            if webtrader_http_proc.poll() is not None:
                print(
                    f"[headless] WARN: webtrader HTTP uvicorn 提前退出 "
                    f"(rc={webtrader_http_proc.returncode}), mlearnweb 可能连不上"
                )

    # 校验：每个策略的 gateway_name 必须在 GATEWAYS 中
    valid_gw_names = {gw["name"] for gw in GATEWAYS}
    started: list[str] = []
    inited: list[str] = []

    # 第一轮: add_strategy + init_strategy. init_strategy 会触发 strategy.on_init
    # → MLEngine.validate_bundle → ModelRegistry.register, 把 bundle 的
    # filter_config.json 缓存到 ModelRegistry. 必须在 start_strategy 之前 (on_start
    # 可能立即触发 replay → 走 run_inference_range → 需要 filter_chain_specs 已注入).
    for strat_def in STRATEGIES:
        name = strat_def["strategy_name"]
        cls = strat_def["strategy_class"]
        gw_name = strat_def["gateway_name"]

        if gw_name not in valid_gw_names:
            print(f"[headless] 策略 {name} 引用了未注册的 gateway {gw_name}，跳过")
            continue

        setting = {
            **STRATEGY_BASE_SETTING,
            **strat_def["setting_override"],
            "gateway": gw_name,
        }

        print(f"[headless] adding strategy {name} ({cls}) → gateway={gw_name}...")
        try:
            ml_engine.add_strategy(cls, name, setting)
        except Exception as exc:
            print(f"[headless] add_strategy({name}) 失败: {exc}")
            continue

        if not ml_engine.init_strategy(name):
            print(f"[headless] init_strategy({name}) 失败")
            continue

        inited.append(name)

    # 第二轮: 把 bundle 声明的 filter_chain_specs 注入 DailyIngestPipeline. 之后
    # 20:00 daily_ingest 才知道要给哪些 filter_id 产 active/snapshot, run_inference
    # 也才能据此查找 {QS_DATA_ROOT}/snapshots/filtered/{filter_id}_{T}.parquet.
    if inited:
        try:
            from vnpy_tushare_pro.engine import APP_NAME as TS_APP_NAME
            ts_engine = main_engine.get_engine(TS_APP_NAME)
            ts_datafeed = ts_engine._get_tushare_datafeed()
            ts_pipeline = getattr(ts_datafeed, "daily_ingest_pipeline", None)
            if ts_pipeline is None:
                print(
                    "[headless] WARN: TushareDatafeedPro.daily_ingest_pipeline 为 None "
                    "(ML_DAILY_INGEST_ENABLED 未设为 1?), 跳过 filter_chain_specs 注入. "
                    "20:00 cron 无效, 也无法做实时推理."
                )
            else:
                specs = ml_engine.list_active_filter_configs()
                if not specs:
                    print(
                        "[headless] WARN: ml_engine.list_active_filter_configs() 返空; "
                        "策略未声明 bundle 或 filter_config 缺失? "
                        "DailyIngestPipeline.ingest_today 会 raise."
                    )
                ts_pipeline.set_filter_chain_specs(specs)
                print(
                    f"[headless] DailyIngestPipeline.filter_chain_specs 已注入 "
                    f"{len(specs)} 个 filter_id: {list(specs.keys())}"
                )
        except Exception as exc:
            print(f"[headless] 注入 filter_chain_specs 失败: {exc}")

    # 第三轮: start_strategy + 可选立即触发. 此时 filter_chain_specs 已就绪.
    for name in inited:
        if not ml_engine.start_strategy(name):
            print(f"[headless] start_strategy({name}) 失败")
            continue

        started.append(name)
        if TRIGGER_ON_STARTUP:
            print(f"[headless] 立即触发 {name} pipeline...")
            ml_engine.run_pipeline_now(name)

    if not started:
        print("[headless] 没有策略成功启动，退出")
        main_engine.close()
        return 1

    # 主循环
    print(f"[headless] {len(started)} 个策略已就绪: {started}. Ctrl+C 退出.")
    stop_flag = {"stop": False}

    def _sigint(_sig, _frm):
        print("\n[headless] 收到 SIGINT, 退出中...")
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _sigint)
    try:
        while not stop_flag["stop"]:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for name in started:
            print(f"[headless] stop_strategy({name})...")
            ml_engine.stop_strategy(name)
        if webtrader_http_proc is not None and webtrader_http_proc.poll() is None:
            print("[headless] terminating webtrader HTTP uvicorn...")
            try:
                webtrader_http_proc.terminate()
                webtrader_http_proc.wait(timeout=5)
            except Exception as exc:
                print(f"[headless] webtrader HTTP terminate failed: {exc}")
                try:
                    webtrader_http_proc.kill()
                except Exception:
                    pass
        print("[headless] main_engine.close()...")
        main_engine.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
