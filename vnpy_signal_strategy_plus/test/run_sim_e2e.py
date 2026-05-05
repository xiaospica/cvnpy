# -*- coding: utf-8 -*-
"""命令行启动器：sim 网关 + 测试策略 + WebTrader (无 GUI)。

与 ``run_sim.py`` 的差异：

- **无 Qt GUI**：纯 EventEngine 主循环，可在 SSH/Docker/编排器子进程里跑。
- **自动 connect**：从 ``test_setting.json`` 读 sim 配置，启动后立即 connect。
- **自动加载并启动策略**：``EtfIntraTestStrategy``，避免 GUI 手动操作。
- **自动启动 WebTrader 服务**：在主进程启动 ZMQ RpcServer，再 subprocess 起
  uvicorn 提供 HTTP REST/WS 给前端实盘监控。

用法::

    F:/Program_Home/vnpy/python.exe -m vnpy_signal_strategy_plus.test.run_sim_e2e \\
        --config vnpy_signal_strategy_plus/test/test_setting.json

启动后：

- sim 网关已 connect（账户=test_setting.sim.account_id，初始资金=connect_setting.模拟资金）
- 策略 ``EtfIntraTestStrategy`` 已 init+start（轮询 mysql stock_trade）
- HTTP API: http://127.0.0.1:8001/docs（账号 vnpy/vnpy 见 ``.vntrader/web_trader_setting.json``）
- WS: ws://127.0.0.1:8001/api/v1/ws

按 Ctrl+C 优雅关停（先停策略，再停 webtrader 子进程，最后退出主进程）。

注意：``vendor/qlib_strategy_core`` 的 sys.path 注入与 ``run_sim.py`` 保持一致，
否则 vnpy_ml_strategy 等依赖 qlib 的模块会 import 失败。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal as signal_mod
import subprocess
import sys
import threading
import time
from pathlib import Path

# 与 run_sim.py 一致：把 vendor/qlib_strategy_core 注入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_QLIB_CORE = _PROJECT_ROOT / "vendor" / "qlib_strategy_core"
if _QLIB_CORE.exists() and str(_QLIB_CORE) not in sys.path:
    sys.path.insert(0, str(_QLIB_CORE))

# 关键：把项目根注入 sys.path 之首，确保 ``import vnpy_webtrader`` 加载工程版
# (F:\Quant\vnpy\vnpy_strategy_dev\vnpy_webtrader\，含 list_strategies / get_node_health
# 等 37 个方法的新版本)，而不是 site-packages 的 vnpy_webtrader 1.1.0 老版本（只有
# 5 个方法，会导致前端 5173 调 /api/v1/strategy 报 KeyError 500）。
# 这也保证 vnpy_qmt_sim / vnpy_signal_strategy_plus / vnpy_qmt 等工程内子包都
# 走工程版而非（如果有的话）site-packages 旧版。
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("VNPY_DOCK_BACKEND", "ads")

from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402

from vnpy_qmt_sim import QmtSimGateway  # noqa: E402
from vnpy_signal_strategy_plus import SignalStrategyPlusApp  # noqa: E402
from vnpy_webtrader import WebTraderApp  # noqa: E402


def resolve_setting_path(template_path: Path) -> Path:
    """优先 ``.local.json`` 副本，fallback 到模板。"""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("run_sim_e2e")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------- 启动 ----------------


def boot(setting_path: Path, logger: logging.Logger):
    """启动主引擎、sim 网关、策略、webtrader。返回 (main_engine, web_proc, stop_event)。"""
    with open(setting_path, "r", encoding="utf-8") as f:
        setting = json.load(f)

    sim_cfg = setting["sim"]
    gateway_name = sim_cfg.get("gateway_name", "QMT_SIM")
    connect_setting = dict(sim_cfg.get("connect_setting", {}))
    # 显式传入"账户"以确保 persistence 文件名 = sim_{gateway_name}.db
    connect_setting.setdefault("账户", sim_cfg.get("account_id", gateway_name))

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(QmtSimGateway, gateway_name=gateway_name)
    main_engine.add_app(SignalStrategyPlusApp)
    main_engine.add_app(WebTraderApp)
    logger.info(f"[boot] gateway/app 注册完成 gateway={gateway_name}")

    # vnpy_webtrader 来源校验：必须是工程版（37+ 方法）。site-packages 1.1.0 老
    # 版本只有 5 个方法，会导致前端 5173 调 /api/v1/strategy 报 KeyError 500。
    import vnpy_webtrader.engine as _vwe
    if "Quant" not in _vwe.__file__:
        logger.warning(
            f"[boot] ⚠️ vnpy_webtrader.engine 加载自 {_vwe.__file__}，"
            "可能是 site-packages 老版本！前端策略卡片会失败。"
            "检查 sys.path 是否包含项目根。"
        )

    # 1) connect sim gateway —— 内部会启动 md / td / persistence
    main_engine.connect(connect_setting, gateway_name)
    logger.info(f"[boot] sim 网关已 connect: 资金={connect_setting.get('模拟资金')}")

    # 2) signal engine: load 策略类 + 注册策略实例
    signal_engine = main_engine.get_engine("SignalStrategyPlus")
    signal_engine.init_engine()  # 触发 load_strategy_class（扫描 strategies/）
    if "EtfIntraTestStrategy" not in signal_engine.classes:
        raise RuntimeError(
            "EtfIntraTestStrategy 未被加载。检查："
            f"{_PROJECT_ROOT}/vnpy_signal_strategy_plus/strategies/etf_intra_test_strategy.py"
        )
    signal_engine.add_strategy("EtfIntraTestStrategy")
    strategy_name = setting.get("strategy_name", "etf_intra_test")
    if strategy_name not in signal_engine.strategies:
        raise RuntimeError(
            f"strategy_name 不一致：test_setting.strategy_name={strategy_name}，"
            f"engine 中已注册 {list(signal_engine.strategies)}"
        )
    signal_engine.init_strategy(strategy_name)
    signal_engine.start_strategy(strategy_name)
    logger.info(f"[boot] 策略 {strategy_name} init+start 完成")

    # 3) webtrader 服务
    web_proc = None
    web_cfg = setting.get("webtrader", {}) or {}
    if web_cfg.get("enable", True):
        web_engine = main_engine.get_engine("RpcService")
        if web_engine is None:
            logger.warning("[boot] RpcService engine 未找到，跳过 webtrader 启动")
        else:
            # 诊断：打印实际类型，便于排查"set_node_info AttributeError"等怪异错
            logger.info(
                f"[boot] WebEngine 实例类型 = "
                f"{type(web_engine).__module__}.{type(web_engine).__name__}; "
                f"has set_node_info = {hasattr(web_engine, 'set_node_info')}; "
                f"has start_server = {hasattr(web_engine, 'start_server')}"
            )
            rep = web_cfg.get("rep_address", "tcp://127.0.0.1:2014")
            pub = web_cfg.get("pub_address", "tcp://127.0.0.1:4102")
            # set_node_info 在某些版本/环境不存在；没有也不影响 RPC 功能，只影响节点
            # 元信息显示。用 getattr 防御，避免阻塞 webtrader 启动。
            set_node = getattr(web_engine, "set_node_info", None)
            if callable(set_node):
                try:
                    set_node(
                        node_id=web_cfg.get("node_id", "e2e-sim"),
                        display_name=web_cfg.get("display_name", "e2e-sim"),
                    )
                except Exception as exc:
                    logger.warning(f"[boot] set_node_info 调用失败（忽略）: {exc}")
            else:
                logger.info("[boot] WebEngine 缺 set_node_info（跳过；不影响 RPC 功能）")

            rpc_started = False
            try:
                web_engine.start_server(rep, pub)
                rpc_started = True
                logger.info(f"[boot] WebEngine RpcServer 启动 REP={rep} PUB={pub}")
                # 诊断：列出 RpcServer 实际注册的函数（曾出现 list_strategies/
                # get_node_health 注册不上、前端调 /api/v1/strategy 报 500 的问题）
                try:
                    funcs = sorted(getattr(web_engine.server, "_functions", {}).keys())
                    logger.info(f"[boot] RpcServer 已注册 {len(funcs)} 个 RPC: {funcs}")
                except Exception:
                    pass
            except Exception as exc:
                logger.error(f"[boot] start_server 失败: {exc}; webtrader 不可用，主流程继续")

            if rpc_started:
                host = str(web_cfg.get("http_host", "127.0.0.1"))
                port = str(web_cfg.get("http_port", "8001"))
                cmd = [
                    sys.executable, "-m", "uvicorn",
                    "vnpy_webtrader.web:app",
                    f"--host={host}", f"--port={port}",
                ]
                # 把测试用的 ZMQ 地址通过 env 传给 uvicorn 子进程，
                # vnpy_webtrader/deps.py 优先读 env 覆盖 .vntrader/web_trader_setting.json。
                child_env = dict(os.environ)
                child_env["VNPY_WEB_REQ_ADDRESS"] = rep
                child_env["VNPY_WEB_SUB_ADDRESS"] = pub
                web_proc = subprocess.Popen(
                    cmd,
                    cwd=str(_PROJECT_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=child_env,
                )
                logger.info(
                    f"[boot] uvicorn 子进程 pid={web_proc.pid} -> http://{host}:{port}/docs"
                )
                # 把 uvicorn 输出转发到主进程 stdout，方便编排器抓
                threading.Thread(
                    target=_drain_proc_output,
                    args=(web_proc, logger, "uvicorn"),
                    daemon=True,
                ).start()

    return main_engine, web_proc


def _check_production_ports_free(logger: logging.Logger) -> None:
    """``--use-production-ports`` 启动前的护栏：若 2014/4102/8001 仍在监听，
    打印占用进程信息后 sys.exit(2)，避免和生产常驻 webtrader 端口冲突。"""
    try:
        import psutil
    except ImportError:
        logger.warning("[port-check] psutil 未安装，跳过端口冲突预检")
        return

    target_ports = {2014, 4102, 8001}
    holders: list[tuple[int, int, str]] = []
    for c in psutil.net_connections(kind="inet"):
        if c.status != psutil.CONN_LISTEN:
            continue
        if c.laddr and c.laddr.port in target_ports and c.pid:
            try:
                proc = psutil.Process(c.pid)
                holders.append((c.laddr.port, c.pid, proc.name()))
            except psutil.NoSuchProcess:
                continue
    if not holders:
        logger.info("[port-check] 2014/4102/8001 全部空闲，可占用")
        return

    logger.error("[port-check] 生产端口被占用，--use-production-ports 启动取消：")
    for port, pid, name in holders:
        logger.error(f"  port={port} PID={pid} name={name}")
    logger.error("请先停掉常驻 webtrader（PowerShell: Stop-Process -Id <PID> -Force），再重试。")
    sys.exit(2)


def _drain_proc_output(
    proc: subprocess.Popen, logger: logging.Logger, prefix: str
) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            logger.info(f"[{prefix}] {line}")


# ---------------- 主入口 ----------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="run_sim_e2e: 命令行启动 sim 网关 + 测试策略 + WebTrader（无 GUI）"
    )
    parser.add_argument(
        "--config",
        default=str(
            resolve_setting_path(
                _PROJECT_ROOT / "vnpy_signal_strategy_plus" / "test" / "test_setting.json"
            )
        ),
        help="test_setting.json 路径（默认优先 .local.json 副本）",
    )
    parser.add_argument(
        "--no-webtrader",
        action="store_true",
        help="不启动 WebTrader 服务（纯后台 sim+strategy）",
    )
    parser.add_argument(
        "--use-production-ports",
        action="store_true",
        help=(
            "覆盖 test_setting.webtrader 的端口为生产默认 (2014/4102/8001)，"
            "让 mlearnweb 前端 5173 能看到测试策略卡片。"
            "启动前需先停掉常驻 webtrader（否则端口冲突报错退出）。"
        ),
    )
    args = parser.parse_args()

    logger = _setup_logger()
    setting_path = Path(args.config)

    # 处理 --no-webtrader / --use-production-ports：写入临时 setting 文件
    setting_overrides_needed = args.no_webtrader or args.use_production_ports
    if setting_overrides_needed:
        with open(setting_path, "r", encoding="utf-8") as f:
            setting = json.load(f)
        web = setting.setdefault("webtrader", {})
        if args.no_webtrader:
            web["enable"] = False
        if args.use_production_ports:
            # 启动前检查 2014/4102/8001 是否被占（多半是生产 webtrader）
            _check_production_ports_free(logger)
            web["rep_address"] = "tcp://127.0.0.1:2014"
            web["pub_address"] = "tcp://127.0.0.1:4102"
            web["http_port"] = "8001"
            logger.warning(
                "[main] --use-production-ports：覆盖 webtrader 端口为生产默认 "
                "(2014/4102/8001)；前端 5173 现在会显示本测试策略"
            )
        tmp = setting_path.with_name(f".tmp_{setting_path.name}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(setting, f, ensure_ascii=False, indent=2)
        setting_path = tmp

    main_engine, web_proc = boot(setting_path, logger)

    stop_event = threading.Event()

    def _shutdown(*_args):
        if stop_event.is_set():
            return
        stop_event.set()
        logger.info("[shutdown] 接收到信号，开始优雅关停...")

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal_mod, sig_name, None)
        if sig is None:
            continue
        try:
            signal_mod.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    logger.info("[main] 主循环就绪；按 Ctrl+C 关停")
    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        # 1) 停 webtrader 子进程
        if web_proc is not None and web_proc.poll() is None:
            logger.info(f"[shutdown] terminate uvicorn pid={web_proc.pid}")
            try:
                web_proc.terminate()
                web_proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                logger.warning("[shutdown] uvicorn terminate 超时，kill")
                web_proc.kill()
        # 2) 停策略
        try:
            signal_engine = main_engine.get_engine("SignalStrategyPlus")
            signal_engine.stop_all_strategies()
        except Exception as exc:
            logger.warning(f"[shutdown] 策略停止异常: {exc}")
        # 3) 关主引擎
        try:
            main_engine.close()
        except Exception as exc:
            logger.warning(f"[shutdown] main_engine.close 异常: {exc}")
        logger.info("[shutdown] 完成")
        # 给后台守护线程一点点收尾时间，但不阻塞
        time.sleep(0.5)


if __name__ == "__main__":
    main()
