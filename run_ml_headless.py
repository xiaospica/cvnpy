# -*- coding: utf-8 -*-
"""ML 策略无 Qt 启动示例 — 脚本化运行, 方便冒烟测试和定时任务.

启动姿势:
    F:/Program_Home/vnpy/python.exe run_ml_headless.py

功能:
    1. 启动 MainEngine, 不用 Qt, 不起 MainWindow
    2. 挂 QmtSimGateway 模拟账户
    3. 挂 MLStrategyApp + TushareProApp + WebTraderApp
    4. 按配置创建 + 初始化 + 启动 QlibMLStrategy 一个实例
    5. 可选立即触发一次 pipeline (--trigger-now)
    6. 进入主循环阻塞, Ctrl+C 优雅退出

与 run_sim.py 的差异:
    - 无 Qt 依赖, 可在 Windows Service / docker 里跑
    - 策略配置写死在脚本顶部的 CONFIG dict, 不走 UI
    - 默认只起 QmtSimGateway (模拟), 不碰真实 QmtGateway, 避免手滑下错单

使用要点:
    1. 首次使用请先把 CONFIG 里的 BUNDLE_DIR 改成你本地 Phase 1 产出的 bundle 路径
    2. 若要启动真实交易, 改 ENABLE_TRADING=True (默认 False 干跑)
    3. 推理子进程走研究机 Python 3.11, INFERENCE_PYTHON 路径务必对
"""

import os
import signal
import sys
import time
from pathlib import Path


# ─── sys.path 注入 ─────────────────────────────────────────────────────
# 和 run_sim.py 保持一致: 让 vnpy_ml_strategy 能 import qlib_strategy_core
# (用于 subprocess 入口 vnpy_strategy_core.cli.run_inference 的模块查找)
os.environ["VNPY_DOCK_BACKEND"] = "ads"
_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE / "vendor" / "qlib_strategy_core"
if _CORE_DIR.exists() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
_QLIB_SOURCE = Path(os.getenv("QLIB_SOURCE_ROOT", r"F:\Quant\code\qlib_strategy_dev"))
if (_QLIB_SOURCE / "qlib" / "__init__.py").exists() and str(_QLIB_SOURCE) not in sys.path:
    sys.path.insert(0, str(_QLIB_SOURCE))


# ─── 策略 / 网关配置 (按需改) ──────────────────────────────────────────

# Gateway 选一个 — QMT_SIM 是模拟盘, QMT 是 miniQMT 实盘
USE_GATEWAY = "QMT_SIM"   # "QMT_SIM" | "QMT"

# QmtSimGateway 连接参数
QMT_SIM_SETTING = {
    "账户": "test_headless",
    "模拟资金": 1000000.0,
    "部分成交率": 0.0,
    "拒单率": 0.0,
    "订单超时秒数": 30,
    "成交延迟毫秒": 0,
    "报单上报延迟毫秒": 0,
    "卖出持仓不足拒单": "是",
}

# QmtGateway (真 miniQMT) 连接参数 — 仅当 USE_GATEWAY=="QMT"
QMT_SETTING = {
    # 请按你的 miniQMT 客户端填
    "资金账号": "",
    "客户端路径": r"E:\迅投极速交易终端 睿智融科版\userdata_mini",
}

# ML 策略配置
STRATEGY_NAME = "csi300_lgb_headless"
STRATEGY_CLASS = "QlibMLStrategy"
# Phase 4 v2: 实盘数据根目录. 训练侧 rsync 过来的 bundle 也推荐落在这里.
QS_DATA_ROOT = os.getenv("QS_DATA_ROOT", r"D:/vnpy_data")
VNPY_MODEL_ROOT = os.getenv("VNPY_MODEL_ROOT", r"D:/vnpy_data/models")

STRATEGY_SETTING = {
    # 模型 bundle — 训练机 rsync 到 {VNPY_MODEL_ROOT}/{exp}/{run_id}/
    # 可用 env BUNDLE_DIR 覆盖, 不设则用训练机默认路径便于本地开发
    "bundle_dir": os.getenv(
        "BUNDLE_DIR",
        r"F:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/ab2711178313491f9900b5695b47fa98",
    ),

    # Python 3.11 研究机环境 (subprocess 推理入口)
    "inference_python": os.getenv(
        "INFERENCE_PYTHON",
        r"E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe",
    ),

    # qlib bin 根. Phase 4 v2 默认指向 QS_DATA_ROOT/qlib_data_bin
    # (DailyIngestPipeline 每日 20:00 重建)
    "provider_uri": os.getenv("QS_PROVIDER_URI", f"{QS_DATA_ROOT}/qlib_data_bin"),

    # 调度 — 21:00 配合 20:00 拉数 cron
    "trigger_time": "21:00",
    # 选股 / 下单
    "topk": 7,
    "n_drop": 1,
    "cash_per_order": 100000,
    "gateway": USE_GATEWAY,
    # 推理产出落盘 (不是数据根, 是每次推理的 3 文件 + selections)
    "output_root": os.getenv("ML_OUTPUT_ROOT", r"D:/ml_output"),
    # 推理
    "lookback_days": 60,
    "subprocess_timeout_s": 300,
    # baseline.parquet 默认从 bundle_dir 里取; 想指向别的路径在这里填
    "baseline_path": "",
    # 监控窗口
    "monitor_window_days": 30,
    # **安全开关 — 默认干跑**
    "enable_trading": False,
}

# 是否启动后立即触发一次 pipeline (不等 trigger_time)
TRIGGER_ON_STARTUP = True

# 是否挂载 WebTrader (mlearnweb 要读 /api/v1/ml/* 则必须开)
ENABLE_WEBTRADER = True


# ─── 主函数 ────────────────────────────────────────────────────────────


def main() -> int:
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    # 挂 gateway
    if USE_GATEWAY == "QMT_SIM":
        from vnpy_qmt_sim import QmtSimGateway
        main_engine.add_gateway(QmtSimGateway, gateway_name="QMT_SIM")
        connect_setting = QMT_SIM_SETTING
    elif USE_GATEWAY == "QMT":
        from vnpy_qmt import QmtGateway
        main_engine.add_gateway(QmtGateway, gateway_name="QMT")
        connect_setting = QMT_SETTING
    else:
        raise ValueError(f"unknown USE_GATEWAY: {USE_GATEWAY}")

    # 挂 app
    from vnpy_tushare_pro import TushareProApp
    from vnpy_ml_strategy import MLStrategyApp

    main_engine.add_app(TushareProApp)
    main_engine.add_app(MLStrategyApp)

    if ENABLE_WEBTRADER:
        from vnpy_webtrader import WebTraderApp
        from vnpy_webtrader.engine import APP_NAME as WEB_APP_NAME
        main_engine.add_app(WebTraderApp)

    # 连接 gateway (触发 query_account + 订阅主题)
    print(f"[headless] connecting gateway {USE_GATEWAY}...")
    main_engine.connect(connect_setting, USE_GATEWAY)
    time.sleep(2)  # 等连接完成; 真实场景建议监听 eContract 事件再 add_strategy

    # 拿 MLEngine
    from vnpy_ml_strategy import APP_NAME as ML_APP_NAME
    ml_engine = main_engine.get_engine(ML_APP_NAME)

    # init_engine 会触发 _autoload_strategy_classes 注册 QlibMLStrategy;
    # 以及启动 DailyTimeTaskScheduler. 不调的话 add_strategy 会找不到类.
    ml_engine.init_engine()
    print(f"[headless] MLEngine registered: {ml_engine.get_all_strategy_class_names()}")

    # WebTrader 的 RPC 服务器需要手动启动(UI widget 里是点"启动"按钮触发的).
    # 这一步之后, vnpy_webtrader.web:app uvicorn 才能连上 RPC 拿数据.
    if ENABLE_WEBTRADER:
        web_engine = main_engine.get_engine(WEB_APP_NAME)
        web_engine.start_server("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
        print("[headless] webtrader RPC server started on tcp://127.0.0.1:2014 / 4102")

    # 创建 + 初始化 + 启动策略
    print(f"[headless] adding strategy {STRATEGY_NAME} ({STRATEGY_CLASS})...")
    try:
        strat = ml_engine.add_strategy(STRATEGY_CLASS, STRATEGY_NAME, STRATEGY_SETTING)
    except Exception as exc:
        print(f"[headless] add_strategy 失败: {exc}")
        main_engine.close()
        return 1

    print(f"[headless] init_strategy({STRATEGY_NAME})...")
    if not ml_engine.init_strategy(STRATEGY_NAME):
        print("[headless] init 失败, 请检查 bundle_dir / provider_uri / gateway 连接")
        main_engine.close()
        return 2

    print(f"[headless] start_strategy({STRATEGY_NAME})...")
    if not ml_engine.start_strategy(STRATEGY_NAME):
        print("[headless] start 失败")
        main_engine.close()
        return 3

    # 可选: 立即触发一次
    if TRIGGER_ON_STARTUP:
        print(f"[headless] 立即触发 pipeline (subprocess 推理, 约 60-120s)...")
        ml_engine.run_pipeline_now(STRATEGY_NAME)

    # 主循环 — Ctrl+C 退出
    print(f"[headless] 策略已就绪. trigger_time={STRATEGY_SETTING['trigger_time']}. Ctrl+C 退出.")
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
        print("[headless] stop_strategy...")
        ml_engine.stop_strategy(STRATEGY_NAME)
        print("[headless] main_engine.close()...")
        main_engine.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
