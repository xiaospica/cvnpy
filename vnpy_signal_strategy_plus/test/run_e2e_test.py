# -*- coding: utf-8 -*-
"""端到端回归测试编排器。

**前置条件（用户在编排器启动前完成）：**

1. ``mysql_signal_setting.json`` 不需改动；测试策略 ``EtfIntraTestStrategy``
   自带配置加载，从 ``test_setting.json`` 读 mysql 段。

2. **手动**在另一个终端启动 ``run_sim.py``（或 ``run_sim_e2e.py``，待后续实现）：

   - 取消 ``main_engine.add_gateway(QmtSimGateway, gateway_name="QMT_SIM")`` 的注释
   - GUI 内连接 QMT_SIM 网关，账户初始资金设置为 ``test_setting.json`` 的
     ``initial_capital``
   - 在 GUI 的 SignalStrategyPlus 面板里加载策略 ``EtfIntraTestStrategy``
     （strategy_name="etf_intra_test"），点击「初始化」+「启动」

3. 在 ``test_setting.json`` 中把 ``mysql.password`` / ``redis.password``
   替换为真实密码。

**编排器执行步骤：**

1. 前置检查（mysql/redis 连通性、sim db 文件路径存在）。
2. 清理旧状态：
   - DELETE FROM stock_trade WHERE stg='etf_intra_test' （可选）
   - XTRIM <stream> MAXLEN 0
3. 启动 bridge subprocess（``redis_to_mysql_bridge``，从 test_setting.json
   生成临时 bridge 配置）。
4. 调用 ``csv_to_redis_replay`` 注入信号到 Redis。
5. 等待消化：轮询 ``stock_trade.processed=True`` 比例，达到阈值或超时
   即停止等待。
6. 停止 bridge 子进程。
7. 调用 ``reconcile_trades.reconcile()`` 输出报告 + 退出码。

**当前实现的限制（明确告知用户）：**

- 信号 ``remark`` 的时间过滤：``mysql_signal_strategy.run_polling`` 只查
  当天（now() 起点）信号；若 CSV 是历史数据，需要 csv_to_redis 启用
  ``rebase_remark_to_today``。
- T+1 跨日逻辑：sim 网关的 ``yd_volume`` 只在 ``settle_end_of_day`` 后
  才更新；同一交易日内连续买卖会被 SELL 拒单。完整跨日回归需要走 sim
  的回放模式（td.counter._replay_now + 显式 settle），属后续 TODO。
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis
from sqlalchemy import create_engine, text


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # vnpy_strategy_dev/
PYTHON_EXE = sys.executable  # 用当前解释器，正常情况就是 F:\Program_Home\vnpy\python.exe


@dataclass
class E2EConfig:
    setting_path: Path
    setting: dict
    strategy_name: str
    redis_stream: str
    redis_host: str
    redis_port: int
    redis_password: str
    redis_db: int
    mysql_url: str
    mysql_purge: bool
    sim_db_path: Path
    sim_delete_db: bool
    wait_max_seconds: int
    wait_poll_interval: int

    @classmethod
    def load(cls, setting_path: Path) -> "E2EConfig":
        with open(setting_path, "r", encoding="utf-8") as f:
            setting = json.load(f)
        sim = setting["sim"]
        sim_db_path = Path(sim["db_dir"]) / f"sim_{sim['account_id']}.db"
        return cls(
            setting_path=setting_path,
            setting=setting,
            strategy_name=setting["strategy_name"],
            redis_stream=setting["redis"]["stream_key"],
            redis_host=setting["redis"]["host"],
            redis_port=int(setting["redis"]["port"]),
            redis_password=setting["redis"].get("password", "") or "",
            redis_db=int(setting["redis"].get("db", 0)),
            mysql_url=(
                f"mysql+pymysql://{setting['mysql']['user']}:"
                f"{setting['mysql']['password']}@"
                f"{setting['mysql']['host']}:{setting['mysql']['port']}/"
                f"{setting['mysql']['db']}"
            ),
            mysql_purge=bool(setting["mysql"].get("purge_before_replay", True)),
            sim_db_path=sim_db_path,
            sim_delete_db=bool(sim.get("delete_db_before_replay", False)),
            wait_max_seconds=int(setting["wait"]["max_seconds"]),
            wait_poll_interval=int(setting["wait"]["poll_interval"]),
        )


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("run_e2e_test")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------- 步骤实现 ----------------


def precheck(cfg: E2EConfig, logger: logging.Logger) -> None:
    """前置检查 mysql/redis/sim db 路径。"""
    # mysql
    engine = create_engine(cfg.mysql_url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    logger.info("[precheck] mysql ok")
    engine.dispose()

    # redis
    rds = redis.Redis(
        host=cfg.redis_host, port=cfg.redis_port,
        password=cfg.redis_password or None, db=cfg.redis_db,
    )
    rds.ping()
    logger.info("[precheck] redis ok")

    # sim db （可不存在；run_sim 启动时会自建）
    if cfg.sim_db_path.exists():
        logger.info(f"[precheck] sim db 已存在: {cfg.sim_db_path}")
    else:
        logger.warning(
            f"[precheck] sim db 不存在: {cfg.sim_db_path}（run_sim 启动后才会创建；"
            f"对账阶段必须存在，否则会失败）"
        )


def cleanup(cfg: E2EConfig, logger: logging.Logger) -> None:
    """清理 mysql / redis stream / sim db。"""
    if cfg.mysql_purge:
        engine = create_engine(cfg.mysql_url, pool_pre_ping=True)
        with engine.begin() as conn:
            res = conn.execute(
                text("DELETE FROM stock_trade WHERE stg = :stg"),
                {"stg": cfg.strategy_name},
            )
            logger.info(
                f"[cleanup] mysql DELETE FROM stock_trade stg='{cfg.strategy_name}'"
                f" -> {res.rowcount} 行"
            )
        engine.dispose()

    rds = redis.Redis(
        host=cfg.redis_host, port=cfg.redis_port,
        password=cfg.redis_password or None, db=cfg.redis_db,
    )
    try:
        rds.xtrim(cfg.redis_stream, maxlen=0, approximate=False)
        logger.info(f"[cleanup] XTRIM {cfg.redis_stream} maxlen=0")
    except redis.ResponseError as exc:
        logger.warning(f"[cleanup] XTRIM 跳过（stream 不存在）: {exc}")

    if cfg.sim_delete_db and cfg.sim_db_path.exists():
        backup = cfg.sim_db_path.with_suffix(
            f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        )
        shutil.move(str(cfg.sim_db_path), str(backup))
        logger.warning(
            f"[cleanup] sim db 已备份并移除：{cfg.sim_db_path} -> {backup}"
            f"\n  ⚠️ run_sim.py 必须重启才会重建持仓/账户状态"
        )


def make_bridge_setting(cfg: E2EConfig, logger: logging.Logger) -> Path:
    """从 test_setting.json 派生 bridge 配置临时文件。"""
    bridge_setting = {
        "redis": {
            "host": cfg.setting["redis"]["host"],
            "port": cfg.setting["redis"]["port"],
            "password": cfg.setting["redis"].get("password", ""),
            "db": cfg.setting["redis"].get("db", 0),
            "consumer_group": "order_group",
            "consumer_name": f"e2e-bridge-{datetime.now().strftime('%H%M%S')}",
            "block_ms": 5000,
            "count": 10,
        },
        "mysql": {
            "host": cfg.setting["mysql"]["host"],
            "port": cfg.setting["mysql"]["port"],
            "user": cfg.setting["mysql"]["user"],
            "password": cfg.setting["mysql"]["password"],
            "db": cfg.setting["mysql"]["db"],
        },
        "subscriptions": [
            {
                "stream_key": cfg.redis_stream,
                "target_stg": cfg.strategy_name,
            }
        ],
        "log": {
            "dir": str(PROJECT_ROOT / "logs" / "redis_bridge_e2e"),
            "level": "INFO",
        },
    }
    out = (
        PROJECT_ROOT / "vnpy_signal_strategy_plus" / "test" / "output"
        / "bridge_setting_e2e.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(bridge_setting, f, ensure_ascii=False, indent=2)
    logger.info(f"[bridge] 临时配置写入 {out}")
    return out


def start_bridge(
    bridge_setting_path: Path,
    logger: logging.Logger,
) -> subprocess.Popen:
    cmd = [
        PYTHON_EXE,
        "-m",
        "vnpy_signal_strategy_plus.scripts.redis_to_mysql_bridge",
        "--config",
        str(bridge_setting_path),
    ]
    logger.info(f"[bridge] 启动 subprocess: {' '.join(cmd)}")
    # bridge 自己用 logging.FileHandler 写 logs/redis_bridge_e2e/bridge_*.log，
    # 这里 stdout 重定向到独立日志文件而不是 PIPE：避免 PIPE buffer 写满后
    # bridge 子进程 print 阻塞、停止消费 redis（曾出现 8 秒就 hang 的 bug）。
    bridge_stdout = (
        PROJECT_ROOT / "logs" / "redis_bridge_e2e"
        / f"bridge_subprocess_{datetime.now().strftime('%Y%m%d_%H%M%S')}.stdout.log"
    )
    bridge_stdout.parent.mkdir(parents=True, exist_ok=True)
    fh = open(bridge_stdout, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=fh,
        stderr=subprocess.STDOUT,
        text=True,
    )
    proc._stdout_file = fh  # type: ignore[attr-defined]
    # 等 3 秒让 bridge 完成 ping + 创建消费组
    time.sleep(3)
    if proc.poll() is not None:
        fh.close()
        try:
            out = bridge_stdout.read_text(encoding="utf-8")
        except Exception:
            out = "(无法读取 stdout 文件)"
        raise RuntimeError(f"bridge 启动失败（exit={proc.returncode}）：\n{out}")
    logger.info(f"[bridge] pid={proc.pid} 已启动 stdout->{bridge_stdout}")
    return proc


def stop_bridge(proc: subprocess.Popen, logger: logging.Logger) -> None:
    if proc.poll() is not None:
        logger.info(f"[bridge] 已退出 exit={proc.returncode}")
        return
    logger.info(f"[bridge] terminating pid={proc.pid}")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("[bridge] terminate 超时，kill")
        proc.kill()
        proc.wait(timeout=5)
    fh = getattr(proc, "_stdout_file", None)
    if fh:
        try:
            fh.close()
        except Exception:
            pass
    logger.info(f"[bridge] 已停止 exit={proc.returncode}")


def inject_signals(cfg: E2EConfig, logger: logging.Logger) -> int:
    """调用 csv_to_redis_replay.replay 注入信号。"""
    from vnpy_signal_strategy_plus.test.csv_to_redis_replay import (
        ReplayConfig,
        load_signals,
        replay,
    )

    rcfg = ReplayConfig.from_test_setting(cfg.setting)
    payloads = load_signals(rcfg, logger)
    if not payloads:
        raise RuntimeError("[inject] 未解析出任何信号")
    sent = replay(rcfg, payloads, logger)
    return sent


def wait_for_consumption(cfg: E2EConfig, expected_count: int, logger: logging.Logger) -> None:
    """轮询 stock_trade.processed 比例，超时或达 99% 即返回。"""
    engine = create_engine(cfg.mysql_url, pool_pre_ping=True)
    deadline = time.time() + cfg.wait_max_seconds
    last_pending = -1
    while time.time() < deadline:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT "
                    "  SUM(CASE WHEN processed=1 THEN 1 ELSE 0 END) AS done, "
                    "  COUNT(*) AS total "
                    "FROM stock_trade WHERE stg=:stg"
                ),
                {"stg": cfg.strategy_name},
            ).fetchone()
        done = int(row.done or 0)
        total = int(row.total or 0)
        pending = total - done
        if pending != last_pending:
            logger.info(
                f"[wait] mysql 进度 {done}/{total} (注入={expected_count}, pending={pending})"
            )
            last_pending = pending
        if total >= expected_count and pending == 0:
            logger.info("[wait] 全部消化完成")
            engine.dispose()
            return
        time.sleep(cfg.wait_poll_interval)

    engine.dispose()
    logger.warning(
        f"[wait] 超时（{cfg.wait_max_seconds}s）尚有 {last_pending} 条未消化；"
        "继续做对账（结果可能 FAIL）"
    )


def run_reconcile(cfg: E2EConfig, logger: logging.Logger) -> int:
    from vnpy_signal_strategy_plus.test.reconcile_trades import reconcile

    return reconcile(cfg.setting_path, logger)


# ---------------- 主流程 ----------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="vnpy_signal_strategy_plus 端到端回归测试编排器"
    )
    parser.add_argument(
        "--config",
        default=str(
            PROJECT_ROOT / "vnpy_signal_strategy_plus" / "test" / "test_setting.json"
        ),
        help="test_setting.json 路径",
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="跳过 mysql/redis stream 清理（调试用）",
    )
    parser.add_argument(
        "--skip-bridge",
        action="store_true",
        help="跳过启动 bridge subprocess（用户已在另一终端跑 bridge 时使用）",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="只跑对账，不清理/不注入/不等待",
    )
    args = parser.parse_args()

    logger = _setup_logger()
    cfg = E2EConfig.load(Path(args.config))
    logger.info(
        f"[e2e] strategy={cfg.strategy_name} stream={cfg.redis_stream} "
        f"sim_db={cfg.sim_db_path}"
    )

    if args.reconcile_only:
        sys.exit(run_reconcile(cfg, logger))

    precheck(cfg, logger)

    if not args.skip_cleanup:
        cleanup(cfg, logger)

    bridge_proc: Optional[subprocess.Popen] = None
    if not args.skip_bridge:
        bridge_setting_path = make_bridge_setting(cfg, logger)
        bridge_proc = start_bridge(bridge_setting_path, logger)

    try:
        sent = inject_signals(cfg, logger)
        logger.info(
            f"[e2e] 已注入 {sent} 条信号；等待 strategy + bridge 消化（最多 {cfg.wait_max_seconds}s）"
        )
        wait_for_consumption(cfg, sent, logger)
    finally:
        if bridge_proc is not None:
            stop_bridge(bridge_proc, logger)

    logger.info("[e2e] 提示：sim 端 (run_sim.py) 仍在 GUI 中运行；")
    logger.info(
        "[e2e]      持仓终态对账前请确认 sim 已撮合完所有订单（看 sim db 中 sim_orders.status）"
    )

    code = run_reconcile(cfg, logger)
    sys.exit(code)


if __name__ == "__main__":
    main()
