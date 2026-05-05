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
sys.path.append('.')

# ─── sys.path 注入 ─────────────────────────────────────────────────────
os.environ["VNPY_DOCK_BACKEND"] = "ads"
_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE / "vendor" / "qlib_strategy_core"
if _CORE_DIR.exists() and str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))


# ─── P0-1/P0-2: load .env + yaml config ────────────────────────────────
# 优先级:
#   1. env 变量 DOTENV_FILE (e.g. .env.staging)
#   2. .env.production (如存在)
#   3. .env (如存在)
#   4. 系统 env (Machine scope)
#   5. .env.example 中的注释默认值 (仅参考, 不会自动加载)
from dotenv import load_dotenv  # noqa: E402

_DOTENV_FILE = os.getenv("DOTENV_FILE")
if _DOTENV_FILE and (_HERE / _DOTENV_FILE).exists():
    load_dotenv(_HERE / _DOTENV_FILE, override=False)
elif (_HERE / ".env.production").exists():
    load_dotenv(_HERE / ".env.production", override=False)
elif (_HERE / ".env").exists():
    load_dotenv(_HERE / ".env", override=False)
# 如果都没有, 继续走系统 env / 启动期会因关键 env 缺失而 raise

# vendor/qlib_strategy_core/ 已在 sys.path[0] (line 53-55), 推理子进程也走它.
# vnpy 主进程不直接 import qlib, 所以这是唯一所需 sys.path. 部署机不需要外部
# qlib_strategy_dev 仓库.


def _load_yaml_config(yaml_path: Path) -> dict:
    """加载 yaml 配置, ${VAR} 占位符按 .env / 系统 env 展开.

    用 os.path.expandvars 而非 string.Template — 前者支持 ${VAR} 与 $VAR 两种;
    在 .yaml 中只用 ${VAR} 形式 (避免与 yaml 自身语法冲突).

    缺失的 env 变量会保留 ${VAR} 字面量; 后续启动期 dict 解构时会报"路径含 $"
    使其暴露 (而非默默走错路径).
    """
    import yaml
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"strategies yaml 不存在: {yaml_path}\n"
            f"拷贝 config/strategies.example.yaml 到此路径并按部署填实际值."
        )
    text = yaml_path.read_text(encoding="utf-8")
    text = os.path.expandvars(text)
    return yaml.safe_load(text)


# 加载 yaml: 路径默认 config/strategies.production.yaml
_STRATEGIES_YAML = Path(
    os.getenv("STRATEGIES_CONFIG", "config/strategies.production.yaml")
)
if not _STRATEGIES_YAML.is_absolute():
    _STRATEGIES_YAML = _HERE / _STRATEGIES_YAML
_CFG = _load_yaml_config(_STRATEGIES_YAML)


# ─── P1-2: loguru 日志滚动 (在加载 yaml 后, 业务模块 import 前) ─────────
# vnpy / engine / scheduler 都用 loguru. 这里配置 100MB rotation + 14 天
# retention + zip 压缩, 防止无 rotation 几周后磁盘塞满.
from vnpy_common.log_setup import setup_logger  # noqa: E402

setup_logger(process_name="vnpy_headless")


# ─── 从 yaml 解析 GATEWAYS / STRATEGIES / 共享 base setting ─────────────

def _build_gateways(cfg: dict) -> list:
    """yaml gateways[] 各 entry 的 base 字段引用 gateway_base_settings 解析为 setting dict."""
    base_pool: dict[str, dict] = cfg.get("gateway_base_settings", {})
    out = []
    for gw in cfg["gateways"]:
        base_name = gw.get("base", "")
        base_setting = dict(base_pool.get(base_name, {}))
        # 允许 yaml 中 inline 'setting' 字段直接覆盖 base
        inline_setting = gw.get("setting") or {}
        base_setting.update(inline_setting)
        out.append({
            "kind": gw["kind"],
            "name": gw["name"],
            "setting": base_setting,
        })
    return out


GATEWAYS = _build_gateways(_CFG)
STRATEGY_BASE_SETTING = dict(_CFG["strategy_base_setting"])
STRATEGIES = list(_CFG["strategies"])

# 显式 set QS_DATA_ROOT 到环境变量 (engine.run_inference / run_inference_range 内部用 os.getenv)
# 之前的版本用 os.environ.setdefault, 现在改成由 .env 提供; 这里仅做兜底防错.
if not os.getenv("QS_DATA_ROOT"):
    raise RuntimeError(
        "QS_DATA_ROOT 未设. 检查 .env (或 .env.production) 是否存在并含此字段."
    )


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
                # P0-3: ML_DAILY_INGEST_ENABLED 显式设 "0" (默认 "1" 启用)
                # 实盘 / 模拟回放都依赖 daily_ingest 产 filter snapshot, 没它
                # 21:00 推理走 strict raise. 这里直接 abort 让用户感知.
                raise RuntimeError(
                    "DailyIngestPipeline 未启用 (ML_DAILY_INGEST_ENABLED='0').\n"
                    "实盘 / 回放都需要它产 filter snapshot, 否则 21:00 推理将"
                    "因 'filter snapshot 不存在' raise.\n"
                    "解决: 在 .env.production 中设 ML_DAILY_INGEST_ENABLED=1, "
                    "或 (仅研发机无 tushare) 显式删除该策略."
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
