# -*- coding: utf-8 -*-
"""把交易记录 CSV 注入 Redis Stream，用于端到端回归测试。

用法::

    python -m vnpy_signal_strategy_plus.test.csv_to_redis_replay \
        --config vnpy_signal_strategy_plus/test/test_setting.json

输入 CSV 是聚宽（JoinQuant）历史成交流水（GBK 编码），列含
``日期 / 委托时间 / 标的 / 交易类型 / 成交数量 / 成交价`` 等。

关键转换：

- 标的：``黄金ETF(518880.XSHG)`` -> ``518880.SH``（XSHG=SH，XSHE=SZ）。
  bridge 写入 stock_trade 时直接透传，策略层
  ``convert_code_to_vnpy_type`` 会再剥后缀转 ``518880.SSE``。
- 数量：``35400股`` / ``-35400股`` -> 取绝对值后的整数（正负号已由"交易类型"
  字段表达）。
- 方向：``买`` -> ``BUY_LST``；``卖`` -> ``SELL_LST``。
- amt -> pct 反推：``pct = abs(amt) * price / initial_capital``。
  ⚠️ 测试时模拟柜台账户初始资金必须设置为同一个 ``initial_capital``，
  否则按 pct 算回的股数对不上。
- 时间：``日期`` + ``委托时间`` -> ``YYYY-MM-DD HH:MM:SS``，写入 ``remark`` 字段。

发送节奏：按时间戳排序后**瞬时全部 xadd**（依赖 bridge+strategy 的 50ms
轮询自然消化），不做 sleep。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import redis


CN_COLS = {
    "日期": "trade_date",
    "委托时间": "order_time",
    "品种": "category",
    "标的": "instrument",
    "交易类型": "side_cn",
    "下单类型": "order_type_cn",
    "成交数量": "filled_qty_str",
    "成交价": "filled_price",
    "成交额": "filled_amount",
    "委托数量": "order_qty",
    "委托价格": "order_price",
    "平仓盈亏": "close_pnl",
    "手续费": "commission",
    "状态": "status_cn",
    "最后更新时间": "last_update",
}

INSTRUMENT_RE = re.compile(r"\((\d{6}\.[A-Z]+)\)")

JQ_TO_REDIS_EXCHANGE = {
    "XSHG": "SH",
    "XSHE": "SZ",
}

DIRECTION_MAP = {
    "买": "BUY_LST",
    "卖": "SELL_LST",
}


@dataclass
class ReplayConfig:
    strategy_name: str
    initial_capital: float
    csv_path: Path
    csv_encoding: str
    redis_host: str
    redis_port: int
    redis_password: str
    redis_db: int
    stream_key: str
    trim_before_replay: bool
    # 时间过滤：date_range = ["YYYY-MM-DD", "YYYY-MM-DD"]，闭区间；空表示全部
    date_range: tuple[str, str] | None
    # 时间 rebase：把 remark 的日期部分改成今天，HH:MM:SS 保留。
    # mysql_signal_strategy.run_polling 默认只查"今天"信号；CSV 是历史
    # 日期时必须开启此选项，否则信号永远不会被策略消费。
    rebase_remark_to_today: bool

    @classmethod
    def from_test_setting(cls, setting: dict) -> "ReplayConfig":
        replay_setting = setting.get("replay", {}) or {}
        date_range_raw = replay_setting.get("date_range")
        date_range = (
            (str(date_range_raw[0]), str(date_range_raw[1]))
            if isinstance(date_range_raw, list) and len(date_range_raw) == 2
            else None
        )
        return cls(
            strategy_name=setting["strategy_name"],
            initial_capital=float(setting["initial_capital"]),
            csv_path=Path(setting["csv"]["transaction_path"]),
            csv_encoding=setting["csv"].get("encoding", "gbk"),
            redis_host=setting["redis"]["host"],
            redis_port=int(setting["redis"]["port"]),
            redis_password=setting["redis"].get("password", "") or "",
            redis_db=int(setting["redis"].get("db", 0)),
            stream_key=setting["redis"]["stream_key"],
            trim_before_replay=bool(setting["redis"].get("trim_before_replay", True)),
            date_range=date_range,
            rebase_remark_to_today=bool(replay_setting.get("rebase_remark_to_today", True)),
        )


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("csv_to_redis_replay")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def _parse_qty(qty_str: str) -> int:
    """解析 '35400股' / '-35400股' / '35400' 等，返回绝对值整数。"""
    if pd.isna(qty_str):
        return 0
    s = str(qty_str).strip().rstrip("股").rstrip("手").rstrip(",")
    try:
        return abs(int(float(s)))
    except ValueError:
        return 0


def _parse_instrument(raw: str) -> Optional[str]:
    """从 '黄金ETF(518880.XSHG)' 提取 '518880.SH'；提取失败返 None。"""
    if pd.isna(raw):
        return None
    m = INSTRUMENT_RE.search(str(raw))
    if not m:
        return None
    code, suffix = m.group(1).split(".")
    redis_suffix = JQ_TO_REDIS_EXCHANGE.get(suffix)
    if not redis_suffix:
        return None
    return f"{code}.{redis_suffix}"


def load_signals(cfg: ReplayConfig, logger: logging.Logger) -> list[dict]:
    """读 CSV 并构造 Redis Stream payload 列表（按时间戳升序）。"""
    if not cfg.csv_path.exists():
        raise FileNotFoundError(f"transaction CSV 不存在: {cfg.csv_path}")

    df = pd.read_csv(cfg.csv_path, encoding=cfg.csv_encoding)
    df = df.rename(columns=CN_COLS)
    logger.info(f"[csv] 读入 {len(df)} 行，列={list(df.columns)}")

    if cfg.date_range:
        before = len(df)
        df = df[
            (df["trade_date"] >= cfg.date_range[0])
            & (df["trade_date"] <= cfg.date_range[1])
        ].copy()
        logger.info(
            f"[csv] date_range 过滤 [{cfg.date_range[0]}, {cfg.date_range[1]}]: "
            f"{before} -> {len(df)} 行"
        )

    # 资金推荐：单笔最大金额
    try:
        prices = pd.to_numeric(df["filled_price"], errors="coerce")
        qtys = df["filled_qty_str"].apply(_parse_qty).astype(float)
        max_value = float((prices * qtys).abs().max())
        logger.info(
            f"[csv] 单笔最大金额 = {max_value:,.0f}; "
            f"当前 initial_capital = {cfg.initial_capital:,.0f}; "
            f"建议 initial_capital >= {max_value * 1.1:,.0f}（避免 pct>1 被 clip）"
        )
    except Exception:
        pass

    today_str = datetime.now().strftime("%Y-%m-%d")
    if cfg.rebase_remark_to_today:
        logger.info(
            f"[csv] rebase_remark_to_today=True: 所有信号 remark 日期部分将改为 {today_str}（HH:MM:SS 保留）"
        )

    payloads: list[dict] = []
    skipped: dict[str, int] = {}

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for _idx, row in df.iterrows():
        side_cn = str(row.get("side_cn", "")).strip()
        if side_cn not in DIRECTION_MAP:
            _skip(f"未知方向 side_cn={side_cn!r}")
            continue

        instrument = _parse_instrument(row.get("instrument"))
        if not instrument:
            _skip(f"标的解析失败 raw={row.get('instrument')!r}")
            continue

        qty = _parse_qty(row.get("filled_qty_str"))
        if qty <= 0:
            _skip("数量为 0")
            continue

        try:
            price = float(row.get("filled_price"))
        except (TypeError, ValueError):
            _skip("价格非法")
            continue
        if price <= 0:
            _skip("价格<=0")
            continue

        trade_date = str(row.get("trade_date", "")).strip()
        order_time = str(row.get("order_time", "")).strip()
        if not trade_date or not order_time:
            _skip("时间字段缺失")
            continue
        # rebase 到今天：保留时分秒，仅替换日期。
        # mysql_signal_strategy.run_polling 默认按 datetime.now() 起点过滤"当天"，
        # 历史日期的信号会被永远跳过；rebase 是绕过该过滤的唯一办法。
        # 注意：这丢失了原始日期信息（对账时 sim_trades.datetime 也是今天，与
        # CSV 原始日期不可直接对齐 —— reconcile 按 (vt_symbol, direction)
        # 累计聚合而非日期级别精确对账，可绕过此问题）。
        if cfg.rebase_remark_to_today:
            display_date = today_str
            sort_date = trade_date  # 用原始日期排序保持时序
        else:
            display_date = trade_date
            sort_date = trade_date
        remark = f"{display_date} {order_time}"

        pct = round(qty * price / cfg.initial_capital, 6)
        if pct <= 0 or pct > 1.0:
            # mysql 策略 process_signal 会拒掉 pct>1，这里早期告警
            logger.warning(
                f"[csv] 异常 pct={pct:.6f} (qty={qty}, price={price}, capital={cfg.initial_capital})"
                f" instrument={instrument} remark={remark}"
            )
            if pct > 1.0:
                pct = 1.0  # clip; 仍写入便于复现策略侧拒单分支

        payload = {
            "code": instrument,
            "pct": f"{pct:.6f}",
            "type": DIRECTION_MAP[side_cn],
            "price": f"{price:.4f}",
            "stg": cfg.strategy_name,
            "remark": remark,
            "amt": str(qty),
        }
        # 用原始日期+时间作为排序键（rebase 后所有 remark 都以今天日期开头，
        # 直接按 remark 排序会丢失跨日时序）。
        payloads.append((f"{sort_date} {order_time}", payload))

    # 按原始时间序排序后取出 payload
    payloads.sort(key=lambda x: x[0])
    payloads = [p for _, p in payloads]

    logger.info(f"[csv] 解析得到 {len(payloads)} 条有效信号")
    if skipped:
        logger.info(f"[csv] 跳过统计: {skipped}")
    return payloads


def replay(
    cfg: ReplayConfig,
    payloads: list[dict],
    logger: logging.Logger,
) -> int:
    """瞬时全发到 Redis Stream，返回成功 xadd 条数。"""
    rds = redis.Redis(
        host=cfg.redis_host,
        port=cfg.redis_port,
        password=cfg.redis_password or None,
        db=cfg.redis_db,
        socket_keepalive=True,
    )
    rds.ping()
    logger.info(f"[redis] ping ok @ {cfg.redis_host}:{cfg.redis_port} db={cfg.redis_db}")

    if cfg.trim_before_replay:
        try:
            rds.xtrim(cfg.stream_key, maxlen=0, approximate=False)
            logger.info(f"[redis] XTRIM {cfg.stream_key} maxlen=0 (清空旧消息)")
        except redis.ResponseError as exc:
            logger.warning(f"[redis] XTRIM 失败（可能 stream 不存在，忽略）: {exc}")

    sent = 0
    for p in payloads:
        try:
            rds.xadd(cfg.stream_key, p, maxlen=10000, approximate=True)
            sent += 1
        except Exception as exc:
            logger.error(f"[redis] xadd 失败 {exc}; payload={p}")

    logger.info(
        f"[redis] xadd 完成 sent={sent}/{len(payloads)} stream={cfg.stream_key}"
    )
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 transaction.csv 注入 Redis Stream（瞬时全发）"
    )
    parser.add_argument(
        "--config",
        required=True,
        help="test_setting.json 路径",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析 CSV，不实际 xadd",
    )
    args = parser.parse_args()

    logger = _setup_logger()

    with open(args.config, "r", encoding="utf-8") as f:
        setting = json.load(f)
    cfg = ReplayConfig.from_test_setting(setting)
    logger.info(
        f"[cfg] strategy={cfg.strategy_name} stream={cfg.stream_key} "
        f"capital={cfg.initial_capital} csv={cfg.csv_path}"
    )

    payloads = load_signals(cfg, logger)

    if args.dry_run:
        logger.info("[dry-run] 不发 redis；前 3 条 payload 预览：")
        for p in payloads[:3]:
            logger.info(f"  {p}")
        return

    sent = replay(cfg, payloads, logger)
    logger.info(f"[done] replay 完成 {sent} 条信号已写入 Redis Stream")


if __name__ == "__main__":
    main()
