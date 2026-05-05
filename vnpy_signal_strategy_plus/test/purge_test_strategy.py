# -*- coding: utf-8 -*-
"""清理 e2e 测试策略 ``etf_intra_test`` 的残留状态。

用途：当前端 ``localhost:5173`` 上看到 ``etf_intra_test`` 但无法删除时，用本
脚本批量清掉所有持久化痕迹。**不会**触碰生产策略 ``mysql_signal_setting.json``
的其他条目。

清理项目：

1. **MySQL ``stock_trade``**：``DELETE WHERE stg='etf_intra_test'``。
2. **Redis Stream**：``XTRIM <stream> MAXLEN 0`` 清掉积压消息。
3. **sim 网关持久化**：删除 ``D:/vnpy_data/state/sim_QMT_SIM.{db,db-shm,db-wal,lock}``
   （仅当未被进程占用时；若占用先杀进程）。
4. **占用我们端口的残留进程**（**12014/14102/18001 + 旧默认 2014/4102/8001**）：
   仅警告，不主动杀（怕误杀生产 webtrader），用户自行判断。

注意：``etf_intra_test`` 策略实例本身只存在于运行中的主进程内存中——
进程一停就消失，无需"删除"。前端仍能看到它通常是 **mlearnweb 历史快照库**
的残留（独立项目，不在本工程内），需在那边的数据库里清。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import redis
from sqlalchemy import create_engine, text


def resolve_setting_path(template_path: Path) -> Path:
    """优先 ``.local.json`` 副本（含真实密码、加 .gitignore），fallback 到模板。"""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


def load_setting(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def purge_mysql(setting: dict) -> None:
    m = setting["mysql"]
    stg = setting["strategy_name"]
    url = f"mysql+pymysql://{m['user']}:{m['password']}@{m['host']}:{m['port']}/{m['db']}"
    engine = create_engine(url, pool_pre_ping=True)
    with engine.begin() as conn:
        res = conn.execute(
            text("DELETE FROM stock_trade WHERE stg=:stg"),
            {"stg": stg},
        )
        print(f"[mysql] DELETE FROM stock_trade WHERE stg='{stg}' -> {res.rowcount} 行")
    engine.dispose()


def purge_redis(setting: dict) -> None:
    r = setting["redis"]
    stream = r["stream_key"]
    rds = redis.Redis(
        host=r["host"], port=int(r["port"]),
        password=r.get("password") or None, db=int(r.get("db", 0)),
    )
    try:
        rds.xtrim(stream, maxlen=0, approximate=False)
        print(f"[redis] XTRIM {stream} MAXLEN 0 OK")
    except redis.ResponseError as exc:
        print(f"[redis] XTRIM 跳过（stream 不存在）: {exc}")


def purge_sim_db(setting: dict) -> None:
    sim = setting["sim"]
    state_dir = Path(sim["db_dir"])
    acc = sim["account_id"]
    candidates = [
        state_dir / f"sim_{acc}.db",
        state_dir / f"sim_{acc}.db-shm",
        state_dir / f"sim_{acc}.db-wal",
        state_dir / f"sim_{acc}.lock",
    ]
    for p in candidates:
        if p.exists():
            try:
                p.unlink()
                print(f"[sim] 删除 {p}")
            except OSError as exc:
                print(f"[sim] 无法删除 {p}（仍被进程占用？）: {exc}")
        else:
            print(f"[sim] 跳过 {p}（不存在）")


def warn_port_holders(setting: dict) -> None:
    """检查端口占用，警告但不杀。"""
    try:
        import psutil
    except ImportError:
        print("[port] 跳过（psutil 未安装）")
        return

    web = setting.get("webtrader", {}) or {}
    rep = web.get("rep_address", "tcp://127.0.0.1:12014")
    pub = web.get("pub_address", "tcp://127.0.0.1:14102")
    http = web.get("http_port", "18001")

    def _port_of(zmq_addr: str) -> int:
        return int(zmq_addr.rsplit(":", 1)[-1])

    target_ports = {
        _port_of(rep), _port_of(pub), int(http),
        # 也提示生产默认端口（怕用户改回去了）
        2014, 4102, 8001,
    }
    found = []
    for c in psutil.net_connections(kind="inet"):
        if c.status != psutil.CONN_LISTEN:
            continue
        if c.laddr and c.laddr.port in target_ports:
            try:
                proc = psutil.Process(c.pid)
                found.append((c.laddr.port, c.pid, proc.name(), proc.exe()))
            except psutil.NoSuchProcess:
                continue
    if found:
        print("[port] 发现监听者（请确认是否生产服务，必要时手动 Stop-Process）：")
        for port, pid, name, exe in found:
            print(f"  port={port} PID={pid} name={name} exe={exe}")
    else:
        print("[port] 关键端口均空闲（含生产默认 2014/4102/8001）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="清理 e2e 测试策略 etf_intra_test 的残留（mysql / redis / sim db）"
    )
    parser.add_argument(
        "--config",
        default=str(
            resolve_setting_path(Path(__file__).resolve().parent / "test_setting.json")
        ),
    )
    parser.add_argument("--skip-mysql", action="store_true")
    parser.add_argument("--skip-redis", action="store_true")
    parser.add_argument("--skip-sim-db", action="store_true")
    args = parser.parse_args()

    setting = load_setting(Path(args.config))
    print(f"[purge] strategy_name={setting['strategy_name']}")

    warn_port_holders(setting)

    if not args.skip_mysql:
        purge_mysql(setting)
    if not args.skip_redis:
        purge_redis(setting)
    if not args.skip_sim_db:
        purge_sim_db(setting)

    print("[purge] 完成")


if __name__ == "__main__":
    main()
