# -*- coding: utf-8 -*-
"""Redis Stream -> MySQL v2 signal journal bridge.

The canonical JoinQuant signal path is now:
JoinQuant -> Redis Stream -> trade_signal_events -> strategy_signal_applications.
The legacy MySQL signal table is intentionally not written by this process.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import signal as signal_mod
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import redis
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

from vnpy_signal_strategy_plus.signal_journal import (
    SignalJournalBase,
    normalize_trade_signal_payload,
    upsert_trade_signal_event,
)


def resolve_setting_path(template_path: Path) -> Path:
    """Prefer a sibling .local.json file, falling back to the template."""
    local = template_path.with_name(template_path.stem + ".local.json")
    return local if local.exists() else template_path


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
    connect_timeout: int = 10
    read_timeout: int = 30
    write_timeout: int = 30
    lock_wait_timeout: int = 10


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
    message_timeout_s: float = 30.0

    @classmethod
    def from_file(cls, path: Path) -> "BridgeConfig":
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        log = data.get("log", {}) or {}
        mysql_data = dict(data["mysql"])
        mysql_data["connect_timeout"] = _positive_int(
            mysql_data.get("connect_timeout"), 10
        )
        mysql_data["read_timeout"] = _positive_int(mysql_data.get("read_timeout"), 30)
        mysql_data["write_timeout"] = _positive_int(
            mysql_data.get("write_timeout"), 30
        )
        mysql_data["lock_wait_timeout"] = _positive_int(
            mysql_data.get("lock_wait_timeout"), 10
        )
        return cls(
            redis=RedisCfg(**data["redis"]),
            mysql=MySQLCfg(**mysql_data),
            subscriptions=[Subscription(**s) for s in data["subscriptions"]],
            log_dir=log.get("dir", "logs/redis_bridge"),
            log_level=log.get("level", "INFO"),
            message_timeout_s=_positive_float(data.get("message_timeout_s"), 30.0),
        )


def _positive_int(value: object, default: int) -> int:
    """Return a positive integer config value, falling back on invalid input."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _positive_float(value: object, default: float) -> float:
    """Return a positive float config value, falling back on invalid input."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class MySQLWriter:
    """Short-transaction writer for the v2 signal journal."""

    def __init__(self, cfg: MySQLCfg):
        url = URL.create(
            "mysql+pymysql",
            username=cfg.user,
            password=cfg.password,
            host=cfg.host,
            port=int(cfg.port),
            database=cfg.db,
        )
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={
                "connect_timeout": int(cfg.connect_timeout),
                "read_timeout": int(cfg.read_timeout),
                "write_timeout": int(cfg.write_timeout),
            },
        )
        lock_wait_timeout = int(cfg.lock_wait_timeout)

        @event.listens_for(self.engine, "connect")
        def _set_session_timeouts(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(
                    f"SET SESSION innodb_lock_wait_timeout={lock_wait_timeout}"
                )
                cursor.execute(f"SET SESSION lock_wait_timeout={lock_wait_timeout}")
            finally:
                cursor.close()

        self.Session = sessionmaker(bind=self.engine)
        SignalJournalBase.metadata.create_all(self.engine)

    def insert_signal(
        self,
        payload: dict,
        target_stg: str,
        logger: logging.Logger,
        *,
        stream_key: str = "",
        redis_id: str = "",
    ) -> bool:
        """Write one Redis payload into trade_signal_events.

        Redis is acked only after this method commits successfully.  Existing
        signal_uid rows are treated as success so a PEL retry is idempotent.
        """
        try:
            normalized = normalize_trade_signal_payload(
                payload,
                target_stg=target_stg,
                stream_key=stream_key,
                redis_id=redis_id,
            )
        except (KeyError, ValueError) as exc:
            logger.error(f"[mysql] payload parse failed {exc}: {payload}")
            return False

        session = self.Session()
        try:
            row, created = upsert_trade_signal_event(session, normalized)
            session.commit()
            logger.info(
                f"[mysql] signal_event {'insert' if created else 'exists'} "
                f"id={row.id} uid={row.signal_uid} stg={row.stg} code={row.code} "
                f"type={row.signal_type} pct={row.pct} price={row.price} "
                f"empty={int(bool(row.empty))} amt={row.amt} redis_id={redis_id}"
            )
            return True
        except Exception as exc:
            session.rollback()
            logger.error(f"[mysql] signal_event write failed: {exc}; payload={payload}")
            return False
        finally:
            session.close()


class StreamConsumer(threading.Thread):
    """Consumer thread for one Redis Stream."""

    def __init__(
        self,
        sub: Subscription,
        redis_client: redis.Redis,
        cfg: RedisCfg,
        on_message: Callable[[Subscription, dict, str], bool],
        logger: logging.Logger,
        stop_event: threading.Event,
        message_timeout_s: float,
        fatal_exit: Callable[[int], None],
    ):
        super().__init__(name=f"consumer-{sub.stream_key}", daemon=True)
        self.sub = sub
        self.redis = redis_client
        self.cfg = cfg
        self.on_message = on_message
        self.logger = logger
        self.stop_event = stop_event
        self.message_timeout_s = message_timeout_s
        self.fatal_exit = fatal_exit
        self._handler_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"handler-{sub.stream_key}",
        )

    def _ensure_group(self) -> None:
        """Idempotently create the consumer group."""
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
                    f"[{self.sub.stream_key}] group {self.cfg.consumer_group} exists"
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
                f"[{self.sub.stream_key}] create consumer group failed, exit: {exc}"
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
                    f"[{self.sub.stream_key}] redis connection error {exc}; retry in 3s"
                )
                self.stop_event.wait(3)
                continue
            except redis.ResponseError as exc:
                if "NOGROUP" in str(exc):
                    self.logger.warning(
                        f"[{self.sub.stream_key}] consumer group missing, recreating: {exc}"
                    )
                    try:
                        self._ensure_group()
                    except Exception as ensure_exc:
                        self.logger.exception(
                            f"[{self.sub.stream_key}] recreate consumer group failed: {ensure_exc}"
                        )
                    self.stop_event.wait(3)
                    continue
                self.logger.exception(
                    f"[{self.sub.stream_key}] xreadgroup error {exc}; retry in 3s"
                )
                self.stop_event.wait(3)
                continue
            except Exception as exc:
                self.logger.exception(
                    f"[{self.sub.stream_key}] xreadgroup error {exc}; retry in 3s"
                )
                self.stop_event.wait(3)
                continue

            if not resp:
                continue

            for _stream, items in resp:
                for msg_id, raw in items:
                    msg_id_s = (
                        msg_id.decode("utf-8") if isinstance(msg_id, bytes) else str(msg_id)
                    )
                    payload = self._decode(raw)
                    self.logger.info(
                        f"[{self.sub.stream_key}] recv id={msg_id_s} payload={payload}"
                    )
                    ok = self._call_on_message(payload, msg_id_s)

                    if ok:
                        try:
                            self.redis.xack(
                                self.sub.stream_key,
                                self.cfg.consumer_group,
                                msg_id_s,
                            )
                        except Exception as exc:
                            self.logger.error(
                                f"[{self.sub.stream_key}] xack failed id={msg_id_s} {exc}"
                            )
                    else:
                        self.logger.warning(
                            f"[{self.sub.stream_key}] not ack id={msg_id_s}; leave in PEL"
                        )

        self._handler_executor.shutdown(wait=False, cancel_futures=True)
        self.logger.info(f"[{self.sub.stream_key}] consumer stopped")

    def _call_on_message(self, payload: dict, redis_id: str) -> bool:
        """Run one message handler with a fail-fast timeout guard."""
        started = time.perf_counter()
        future = self._handler_executor.submit(
            self.on_message,
            self.sub,
            payload,
            redis_id,
        )
        try:
            ok = future.result(timeout=self.message_timeout_s)
        except concurrent.futures.TimeoutError:
            elapsed = time.perf_counter() - started
            self.logger.critical(
                f"[{self.sub.stream_key}] handler timeout id={redis_id} "
                f"elapsed={elapsed:.3f}s limit={self.message_timeout_s:.3f}s; "
                "fatal exit for supervisor restart"
            )
            self.fatal_exit(2)
            return False
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.logger.exception(
                f"[{self.sub.stream_key}] handler error id={redis_id} "
                f"elapsed={elapsed:.3f}s {exc}"
            )
            return False

        elapsed = time.perf_counter() - started
        self.logger.info(
            f"[{self.sub.stream_key}] handled id={redis_id} "
            f"ok={bool(ok)} elapsed={elapsed:.3f}s"
        )
        return bool(ok)


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
            self.logger.warning("[bridge] DRY-RUN: do not write MySQL")
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

    def _on_message(self, sub: Subscription, payload: dict, redis_id: str) -> bool:
        if self.dry_run:
            self.logger.info(
                f"[dry-run] would write stg={sub.target_stg} redis_id={redis_id} payload={payload}"
            )
            return True
        if self.writer is None:
            return False
        return self.writer.insert_signal(
            payload,
            sub.target_stg,
            self.logger,
            stream_key=sub.stream_key,
            redis_id=redis_id,
        )

    def _ping_redis(self) -> None:
        try:
            self.redis.ping()
            self.logger.info(
                f"[bridge] redis ping ok @ {self.cfg.redis.host}:{self.cfg.redis.port} db={self.cfg.redis.db}"
            )
        except Exception as exc:
            self.logger.error(f"[bridge] redis ping failed: {exc}")
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
                message_timeout_s=self.cfg.message_timeout_s,
                fatal_exit=self._fatal_exit,
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
        def _handler(signum, _frame):
            self.logger.info(f"[bridge] received signal {signum}")
            self.stop()

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal_mod, sig_name, None)
            if sig is None:
                continue
            try:
                signal_mod.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def stop(self) -> None:
        if not self.stop_event.is_set():
            self.logger.info("[bridge] stopping...")
            self.stop_event.set()

    def _fatal_exit(self, code: int) -> None:
        self.stop_event.set()
        logging.shutdown()
        os._exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Redis Stream -> MySQL trade_signal_events bridge"
    )
    parser.add_argument(
        "--config",
        default=str(
            resolve_setting_path(
                Path(__file__).resolve().parent / "redis_bridge_setting.json"
            )
        ),
        help="redis_bridge_setting.json path; .local.json is preferred when present",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write MySQL; log received signals only",
    )
    args = parser.parse_args()

    cfg = BridgeConfig.from_file(Path(args.config))
    BridgeProcess(cfg, dry_run=args.dry_run).run()


if __name__ == "__main__":
    main()
