# -*- coding: utf-8 -*-
"""Redis Stream -> MySQL stock_trade 中转进程。

监听一个或多个 Redis Stream（每个 stream 对应一个生产端策略），把收到的下单
信号写入 MySQL `stock_trade` 表，供 vnpy_signal_strategy_plus 中基于
MySQL 轮询的策略消费。

架构要点：
- 每个订阅一个守护线程，独立 xreadgroup 阻塞拉取，互不影响。
- 写库时序：INSERT -> commit -> xack。MySQL 写失败则不 ack，消息留在
  Redis Consumer Group 的 PEL，下次重启或下次消费由 Consumer 端补偿。
- 字段透传：Redis JSON 的 code 直接写入 MySQL，由策略层
  ``convert_code_to_vnpy_type`` 负责剥后缀（"518880.SH"/"518880"/
  "518880.SSE" 都能正确转成 ``518880.SSE``）。
- ``stg`` 字段：使用配置中的 ``target_stg`` 覆盖 payload 里的 ``stg``，
  支持 Redis 端策略名 与 MySQL 端策略 key 解耦（迁移期常见）。
- ``amt`` / ``empty`` 字段：当前 stock_trade 表无对应列，bridge 静默
  丢弃（INFO 级日志）。CSV 测试通过 INITIAL_CAPITAL 把 amt 反推为
  pct，绕开 schema 限制。

Stock ORM 必须与 ``vnpy_signal_strategy_plus.mysql_signal_strategy.Stock``
保持字段一致。任何 schema 变更需同步两处。
"""
from __future__ import annotations

import argparse
import json
import logging
import signal as signal_mod
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import redis
from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, String,
                        create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker


def resolve_setting_path(template_path: Path) -> Path:
    """优先 ``.local.json`` 副本（含真实密码、加 .gitignore），fallback 到模板。"""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path

Base = declarative_base()


class Stock(Base):
    """与 mysql_signal_strategy.Stock 字段一致的 ORM 镜像。

    单独定义而非 import，避免 bridge 进程拉起整套 vnpy 依赖
    （mysql_signal_strategy 顶部 import vnpy_ctp 等）。schema 变更必须
    两处同步修改。
    """

    __tablename__ = "stock_trade"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), nullable=False)
    pct = Column(Float, nullable=False)
    type = Column(String(32), nullable=False)
    price = Column(Float, nullable=False)
    stg = Column(String(32), nullable=False)
    remark = Column(DateTime, nullable=False)
    processed = Column(Boolean, default=False)


# ---------------- 配置 ----------------


@dataclass
class RedisCfg:
    host: str
    port: int
    password: str = ""
    db: int = 0
    consumer_group: str = "order_group"
    consumer_name: str = "bridge-1"
    block_ms: int = 60_000
    count: int = 3


@dataclass
class MySQLCfg:
    host: str
    port: int
    user: str
    password: str
    db: str


@dataclass
class Subscription:
    stream_key: str
    target_stg: str


@dataclass
class BridgeConfig:
    redis: RedisCfg
    mysql: MySQLCfg
    subscriptions: list[Subscription]
    log_dir: str = "logs/redis_bridge"
    log_level: str = "INFO"

    @classmethod
    def from_file(cls, path: Path) -> "BridgeConfig":
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        log = data.get("log", {}) or {}
        return cls(
            redis=RedisCfg(**data["redis"]),
            mysql=MySQLCfg(**data["mysql"]),
            subscriptions=[Subscription(**s) for s in data["subscriptions"]],
            log_dir=log.get("dir", "logs/redis_bridge"),
            log_level=log.get("level", "INFO"),
        )


# ---------------- MySQL 写入 ----------------


class MySQLWriter:
    """封装 SQLAlchemy 会话；每次写入独立 session 短事务。"""

    def __init__(self, cfg: MySQLCfg):
        url = (
            f"mysql+pymysql://{cfg.user}:{cfg.password}"
            f"@{cfg.host}:{cfg.port}/{cfg.db}"
        )
        self.engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
        self.Session = sessionmaker(bind=self.engine)

    def insert_signal(
        self,
        payload: dict,
        target_stg: str,
        logger: logging.Logger,
    ) -> bool:
        """写一条信号到 stock_trade。失败返回 False（外层不应 xack）。

        :param payload: 已 utf-8 解码的 dict，所有 value 是 str（Redis Stream 字段语义）。
        :param target_stg: 配置里的 target_stg，覆盖 payload['stg']。
        """
        try:
            code = str(payload["code"])
            pct = float(payload.get("pct", 0) or 0)
            type_ = str(payload["type"])
            price = float(payload.get("price", 0) or 0)
            remark_str = str(payload["remark"])
            remark_dt = datetime.strptime(remark_str, "%Y-%m-%d %H:%M:%S")
        except KeyError as exc:
            logger.error(f"[mysql] payload 缺字段 {exc}: {payload}")
            return False
        except ValueError as exc:
            logger.error(f"[mysql] payload 字段解析失败 {exc}: {payload}")
            return False

        # amt / empty 字段当前 stock_trade 表不支持，仅记日志
        for ignore_field in ("amt", "empty"):
            if ignore_field in payload:
                logger.info(
                    f"[mysql] 忽略 payload['{ignore_field}']={payload[ignore_field]} "
                    f"(stock_trade 无对应列)"
                )

        session = self.Session()
        try:
            row = Stock(
                code=code,
                pct=pct,
                type=type_,
                price=price,
                stg=target_stg,
                remark=remark_dt,
                processed=False,
            )
            session.add(row)
            session.commit()
            logger.info(
                f"[mysql] insert ok id={row.id} stg={target_stg} "
                f"code={code} type={type_} pct={pct} price={price} remark={remark_str}"
            )
            return True
        except Exception as exc:
            session.rollback()
            logger.error(f"[mysql] insert 失败: {exc}; payload={payload}")
            return False
        finally:
            session.close()


# ---------------- Redis 消费 ----------------


class StreamConsumer(threading.Thread):
    """单个 Redis Stream 的消费线程。"""

    def __init__(
        self,
        sub: Subscription,
        redis_client: redis.Redis,
        cfg: RedisCfg,
        on_message: Callable[[Subscription, dict], bool],
        logger: logging.Logger,
        stop_event: threading.Event,
    ):
        super().__init__(name=f"consumer-{sub.stream_key}", daemon=True)
        self.sub = sub
        self.redis = redis_client
        self.cfg = cfg
        self.on_message = on_message
        self.logger = logger
        self.stop_event = stop_event

    def _ensure_group(self) -> None:
        """幂等创建 consumer group；stream 不存在时 mkstream=True 自动建。"""
        try:
            self.redis.xgroup_create(
                name=self.sub.stream_key,
                groupname=self.cfg.consumer_group,
                id="0",
                mkstream=True,
            )
            self.logger.info(
                f"[{self.sub.stream_key}] group {self.cfg.consumer_group} created"
            )
        except redis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                self.logger.info(
                    f"[{self.sub.stream_key}] group {self.cfg.consumer_group} 已存在"
                )
            else:
                raise

    @staticmethod
    def _decode(raw: dict) -> dict:
        out = {}
        for k, v in raw.items():
            key = k.decode("utf-8") if isinstance(k, bytes) else str(k)
            val = v.decode("utf-8") if isinstance(v, bytes) else str(v)
            out[key] = val
        return out

    def run(self) -> None:
        try:
            self._ensure_group()
        except Exception as exc:
            self.logger.exception(
                f"[{self.sub.stream_key}] 创建消费组失败，线程退出: {exc}"
            )
            return

        streams = {self.sub.stream_key: ">"}
        self.logger.info(
            f"[{self.sub.stream_key}] consumer started -> target_stg={self.sub.target_stg}"
        )

        while not self.stop_event.is_set():
            try:
                resp = self.redis.xreadgroup(
                    groupname=self.cfg.consumer_group,
                    consumername=self.cfg.consumer_name,
                    streams=streams,
                    block=self.cfg.block_ms,
                    count=self.cfg.count,
                )
            except redis.ConnectionError as exc:
                self.logger.error(
                    f"[{self.sub.stream_key}] redis 连接异常 {exc}; 3s 后重试"
                )
                self.stop_event.wait(3)
                continue
            except Exception as exc:
                self.logger.exception(
                    f"[{self.sub.stream_key}] xreadgroup 异常 {exc}; 3s 后重试"
                )
                self.stop_event.wait(3)
                continue

            if not resp:
                continue

            for _stream, items in resp:
                for msg_id, raw in items:
                    msg_id_s = (
                        msg_id.decode("utf-8") if isinstance(msg_id, bytes) else msg_id
                    )
                    payload = self._decode(raw)
                    self.logger.info(
                        f"[{self.sub.stream_key}] recv id={msg_id_s} payload={payload}"
                    )
                    try:
                        ok = self.on_message(self.sub, payload)
                    except Exception as exc:
                        self.logger.exception(
                            f"[{self.sub.stream_key}] handler 异常 id={msg_id_s} {exc}"
                        )
                        ok = False

                    if ok:
                        try:
                            self.redis.xack(
                                self.sub.stream_key,
                                self.cfg.consumer_group,
                                msg_id_s,
                            )
                        except Exception as exc:
                            self.logger.error(
                                f"[{self.sub.stream_key}] xack 失败 id={msg_id_s} {exc}"
                            )
                    else:
                        self.logger.warning(
                            f"[{self.sub.stream_key}] 不 ack id={msg_id_s}（留 PEL 等待重试）"
                        )

        self.logger.info(f"[{self.sub.stream_key}] consumer stopped")


# ---------------- 进程编排 ----------------


class BridgeProcess:
    def __init__(self, cfg: BridgeConfig, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.logger = self._setup_logger()
        self.redis = redis.Redis(
            host=cfg.redis.host,
            port=cfg.redis.port,
            password=cfg.redis.password or None,
            db=cfg.redis.db,
            socket_keepalive=True,
        )
        if dry_run:
            self.writer: Optional[MySQLWriter] = None
            self.logger.warning("[bridge] DRY-RUN：不写 MySQL，仅打印收到的信号")
        else:
            self.writer = MySQLWriter(cfg.mysql)
        self.stop_event = threading.Event()
        self.threads: list[StreamConsumer] = []

    def _setup_logger(self) -> logging.Logger:
        log_dir = Path(self.cfg.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"bridge_{datetime.now().strftime('%Y%m%d')}.log"

        logger = logging.getLogger("redis_to_mysql_bridge")
        logger.setLevel(getattr(logging, self.cfg.log_level.upper(), logging.INFO))
        # 防止重复 handler
        if logger.handlers:
            return logger

        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"
        )
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        return logger

    def _on_message(self, sub: Subscription, payload: dict) -> bool:
        if self.dry_run:
            self.logger.info(
                f"[dry-run] would write: stg={sub.target_stg} payload={payload}"
            )
            return True
        return self.writer.insert_signal(payload, sub.target_stg, self.logger)

    def _ping_redis(self) -> None:
        try:
            self.redis.ping()
            self.logger.info(
                f"[bridge] redis ping ok @ {self.cfg.redis.host}:{self.cfg.redis.port} db={self.cfg.redis.db}"
            )
        except Exception as exc:
            self.logger.error(f"[bridge] redis ping 失败: {exc}")
            raise

    def run(self) -> None:
        self._ping_redis()
        self.logger.info(
            f"[bridge] subscriptions: "
            f"{[(s.stream_key, s.target_stg) for s in self.cfg.subscriptions]}"
        )

        for sub in self.cfg.subscriptions:
            t = StreamConsumer(
                sub=sub,
                redis_client=self.redis,
                cfg=self.cfg.redis,
                on_message=self._on_message,
                logger=self.logger,
                stop_event=self.stop_event,
            )
            t.start()
            self.threads.append(t)

        self._install_signal_handlers()

        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1)
        except KeyboardInterrupt:
            self.stop()

        for t in self.threads:
            t.join(timeout=3)
        self.logger.info("[bridge] shutdown complete")

    def _install_signal_handlers(self) -> None:
        def _handler(_signum, _frame):
            self.stop()

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal_mod, sig_name, None)
            if sig is None:
                continue
            try:
                signal_mod.signal(sig, _handler)
            except (ValueError, OSError):
                # Windows 上 SIGTERM 在非主线程时不支持；忽略
                pass

    def stop(self) -> None:
        if not self.stop_event.is_set():
            self.logger.info("[bridge] stopping...")
            self.stop_event.set()


# ---------------- 入口 ----------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Redis Stream -> MySQL stock_trade 中转进程"
    )
    parser.add_argument(
        "--config",
        default=str(
            resolve_setting_path(
                Path(__file__).resolve().parent / "redis_bridge_setting.json"
            )
        ),
        help="redis_bridge_setting.json 路径（默认优先 .local.json 副本）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不写 MySQL，只 log 收到的信号（用于排错/抓包）",
    )
    args = parser.parse_args()

    cfg = BridgeConfig.from_file(Path(args.config))
    BridgeProcess(cfg, dry_run=args.dry_run).run()


if __name__ == "__main__":
    main()
