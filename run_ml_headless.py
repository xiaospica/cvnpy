# -*- coding: utf-8 -*-
"""ML 策略无 Qt 启动脚本 — 支持单/多 gateway 多策略沙盒。

启动姿势:
    F:/Program_Home/vnpy/python.exe run_ml_headless.py

两种模式:
    模式 A · 实盘 miniqmt（单 gateway）
        USE_GATEWAY_KIND = "QMT"
        GATEWAYS = [{"name": "QMT", "setting": QMT_SETTING}]
        STRATEGIES = [{"gateway_name": "QMT", ...}, ...]   # 所有策略共用 QMT 网关
        约束：miniqmt 单进程单账户，多策略只能合并到一个账户

    模式 B · 模拟多策略沙盒（多 gateway，方案 Y）
        USE_GATEWAY_KIND = "QMT_SIM"
        GATEWAYS = [
            {"name": "QMT_SIM_csi300", "setting": {...}},
            {"name": "QMT_SIM_zz500",  "setting": {...}},
        ]
        STRATEGIES = [
            {"gateway_name": "QMT_SIM_csi300", ...},
            {"gateway_name": "QMT_SIM_zz500",  ...},
        ]
        每策略独立 SQLite + 独立账户，mlearnweb 前端三策略各自一条权益曲线

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


# ─── 模式选择 ───────────────────────────────────────────────────────────
USE_GATEWAY_KIND = "QMT_SIM"   # "QMT_SIM" | "QMT"


# ─── QmtSimGateway 默认参数（模拟模式各 gateway 共享） ───────────────────
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
    "merged_parquet_reference_kind": "prev_close",
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


# ─── Gateways 列表 ─────────────────────────────────────────────────────
# 模拟模式下默认 1 个 gateway（与历史行为等价）；启用多策略沙盒在下面注释处加。
# 实盘模式下必为 1 个 gateway（miniqmt 单账户约束）。

if USE_GATEWAY_KIND == "QMT_SIM":
    GATEWAYS = [
        {"name": "QMT_SIM_csi300", "setting": dict(QMT_SIM_BASE_SETTING)},
        # 启用多策略沙盒示例（取消注释下方两行 + 同步加 STRATEGIES）：
        # {"name": "QMT_SIM_zz500",   "setting": dict(QMT_SIM_BASE_SETTING)},
        # {"name": "QMT_SIM_alldata", "setting": dict(QMT_SIM_BASE_SETTING)},
    ]
elif USE_GATEWAY_KIND == "QMT":
    GATEWAYS = [{"name": "QMT", "setting": QMT_SETTING}]
else:
    raise ValueError(f"unknown USE_GATEWAY_KIND: {USE_GATEWAY_KIND}")


# ─── ML 策略基础参数（所有策略共用） ───────────────────────────────────
QS_DATA_ROOT = os.getenv("QS_DATA_ROOT", r"D:/vnpy_data")
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
    # **安全开关 — 默认干跑**
    "enable_trading": False,
}


# ─── 策略列表 ──────────────────────────────────────────────────────────
# 每条策略一个 add_strategy 调用。gateway_name 必须在 GATEWAYS 中存在。
# 实盘模式下所有策略指向同一个 "QMT"；模拟沙盒下各指向自己的 QMT_SIM_*。

STRATEGIES = [
    {
        "strategy_name": "csi300_lgb_headless",
        "strategy_class": "QlibMLStrategy",
        "gateway_name": "QMT_SIM_csi300" if USE_GATEWAY_KIND == "QMT_SIM" else "QMT",
        "setting_override": {
            "bundle_dir": os.getenv(
                "BUNDLE_DIR",
                r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/ab2711178313491f9900b5695b47fa98",
            ),
            "topk": 7,
            "n_drop": 1,
            "cash_per_order": 100_000,
        },
    },
    # 多策略沙盒示例（仅 QMT_SIM 模式下意义；记得同步打开上面 GATEWAYS 的对应行）：
    # {
    #     "strategy_name": "zz500_lgb_headless",
    #     "strategy_class": "QlibMLStrategy",
    #     "gateway_name": "QMT_SIM_zz500",
    #     "setting_override": {
    #         "bundle_dir": os.getenv("BUNDLE_DIR_ZZ500", r"...zz500_bundle..."),
    #         "topk": 5, "n_drop": 1, "cash_per_order": 200_000,
    #     },
    # },
]


TRIGGER_ON_STARTUP = True
ENABLE_WEBTRADER = True


# ─── 主函数 ────────────────────────────────────────────────────────────


def main() -> int:
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 挂 gateway 类
    if USE_GATEWAY_KIND == "QMT_SIM":
        from vnpy_qmt_sim import QmtSimGateway as _GatewayClass
    else:
        from vnpy_qmt import QmtGateway as _GatewayClass

    # 注册所有 gateway 实例
    for gw in GATEWAYS:
        main_engine.add_gateway(_GatewayClass, gateway_name=gw["name"])

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

    if ENABLE_WEBTRADER:
        web_engine = main_engine.get_engine(WEB_APP_NAME)
        web_engine.start_server("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
        print("[headless] webtrader RPC server started on tcp://127.0.0.1:2014 / 4102")

    # 校验：每个策略的 gateway_name 必须在 GATEWAYS 中
    valid_gw_names = {gw["name"] for gw in GATEWAYS}
    started: list[str] = []

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
        print("[headless] main_engine.close()...")
        main_engine.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
