"""统一日推进 / 心跳轮询 / 多策略多日断言 helper.

被 ``run_ml_headless_smoke.py`` 与 ``smoke_full_pipeline.py`` 共用. 抽取自
后者 (line 266-355 等), 但调用 ``ml_engine.run_pipeline_now(name, as_of_date=day)``
显式注入历史日期, 不再 monkey-patch ``vnpy_ml_strategy.template.date.today()``.

为何不 monkey-patch
-------------------
``run_daily_pipeline(as_of_date=...)`` 已 fully 支持显式日期注入
(template.py:345 ``today = as_of_date if as_of_date is not None else date.today()``),
所有下游 (run_inference / persist_selections / pred_score 取 pred_df.max date)
都从 ``today`` 派生. 显式传 ``as_of_date`` 时 ``date.today()`` 不会被调用,
也就不需要 monkey-patch.

好处:
  1. 不污染全局 ``date`` 类, 避免影响 sim gateway 撮合 / log 时间戳等其它消费者
  2. 测试代码与生产代码路径完全一致 — 生产 cron 也可以传 as_of_date 做手动回放
  3. 更易写单测 (helper 函数纯参数化, 不带全局副作用)

历史 ``smoke_full_pipeline.py`` 用 monkey-patch 是因为它 ``run_pipeline_now(name)``
没传 as_of_date, 必须靠 patch 让 ``run_daily_pipeline`` 内的 ``date.today()`` 返回模拟日.
新 helper 直接传 as_of_date, 干净 + 无副作用.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# =====================================================================
# 日历 / 交易日序列
# =====================================================================


def build_trade_days(
    start: date,
    end: date,
    is_trade_date_fn: Callable[[date], bool],
) -> List[date]:
    """返回 [start, end] (含两端) 内所有交易日.

    Parameters
    ----------
    start, end : date
        闭区间端点. ``start > end`` 时返回空列表.
    is_trade_date_fn : Callable[[date], bool]
        交易日判断函数. 通常用 ``lambda d: tushare_engine._get_tushare_datafeed()
        .downloader.is_trade_date(d.strftime('%Y%m%d'))``.
    """
    if start > end:
        return []
    days: List[date] = []
    d = start
    while d <= end:
        try:
            if is_trade_date_fn(d):
                days.append(d)
        except Exception:  # noqa: BLE001 — 查失败保守保留
            days.append(d)
        d += timedelta(days=1)
    return days


def build_next_n_trade_days(
    start: date,
    n: int,
    is_trade_date_fn: Callable[[date], bool],
    *,
    max_lookahead: int = 60,
) -> List[date]:
    """从 ``start`` (含) 开始往后取 n 个交易日."""
    if n <= 0:
        return []
    days: List[date] = []
    d = start
    scanned = 0
    while len(days) < n and scanned < max_lookahead:
        try:
            if is_trade_date_fn(d):
                days.append(d)
        except Exception:  # noqa: BLE001
            days.append(d)
        d += timedelta(days=1)
        scanned += 1
    return days


# =====================================================================
# Ingest 后台线程 + 心跳
# =====================================================================


def run_ingest_with_heartbeat(
    tushare_engine: Any,
    day_str: str,
    *,
    heartbeat_s: float = 10.0,
    log_fn: Callable[[str], None] = print,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """后台线程跑 ``tushare_engine.run_daily_ingest_now(day_str)`` + 心跳轮询.

    主线程同步跑会让 webtrader RPC 30s 超时 (DumpDataAll IO ~100s+),
    放后台线程 + 主线程心跳保持 RPC 事件循环活跃.

    Returns
    -------
    (result_dict, elapsed_s):
        result_dict 是 ``run_daily_ingest_now`` 返回值 (dict 或 None);
        若线程内抛异常会重 raise.
    """
    state: Dict[str, Any] = {"result": None, "error": None, "done": False}

    def _worker() -> None:
        try:
            state["result"] = tushare_engine.run_daily_ingest_now(day_str)
        except Exception as exc:  # noqa: BLE001
            state["error"] = exc
        finally:
            state["done"] = True

    th = threading.Thread(target=_worker, daemon=False, name=f"ingest-{day_str}")
    t0 = time.time()
    th.start()
    last_hb = t0
    while not state["done"]:
        time.sleep(0.5)
        now = time.time()
        if now - last_hb >= heartbeat_s:
            log_fn(f"  ... ingest[{day_str}] running ({now - t0:.0f}s elapsed)")
            last_hb = now
    th.join(timeout=5)
    if state["error"] is not None:
        raise state["error"]
    return state["result"], time.time() - t0


# =====================================================================
# Pipeline 触发 + 等待 diagnostics 落盘
# =====================================================================


def wait_pipeline_with_heartbeat(
    ml_engine: Any,
    strategy_name: str,
    day: date,
    output_root: str,
    *,
    timeout_s: int = 600,
    heartbeat_s: float = 30.0,
    log_fn: Callable[[str], None] = print,
) -> Tuple[bool, float]:
    """触发 ``run_pipeline_now(strategy_name, as_of_date=day)`` + 轮询 diagnostics.json mtime.

    用 ``mtime > initial_mtime`` 判断 "本次运行新产出", 否则旧 diagnostics 会立刻命中.

    ``run_pipeline_now`` 同步阻塞直到 subprocess 完成 (~60-90s),
    所以理论上调返回时 diagnostics 应已落盘. 但 IO 落盘有微秒延迟,
    保留轮询窗口做兜底.

    Returns
    -------
    (ok, elapsed_s): ok=True 表示 diagnostics 在 timeout 内被本次运行重写
    """
    day_str = day.strftime("%Y%m%d")
    out_day_dir = Path(output_root) / strategy_name / day_str
    diag_path = out_day_dir / "diagnostics.json"
    initial_mtime = diag_path.stat().st_mtime if diag_path.exists() else 0.0

    t0 = time.time()
    if not ml_engine.run_pipeline_now(strategy_name, as_of_date=day):
        raise RuntimeError(
            f"run_pipeline_now({strategy_name!r}, as_of_date={day}) returned False"
        )

    last_hb = t0
    while time.time() - t0 < timeout_s:
        if diag_path.exists() and diag_path.stat().st_mtime > initial_mtime:
            return True, time.time() - t0
        time.sleep(2)
        now = time.time()
        if now - last_hb >= heartbeat_s:
            log_fn(
                f"  ... pipeline[{strategy_name}/{day_str}] running ({now - t0:.0f}s elapsed, "
                f"waiting for {diag_path.name})"
            )
            last_hb = now
    return False, time.time() - t0


# =====================================================================
# 单策略单日产物断言
# =====================================================================


def assert_strategy_day_outputs(
    strategy_name: str,
    output_root: str,
    day: date,
    *,
    expected_topk: int = 7,
    require_status_ok: bool = True,
) -> List[str]:
    """检查 ``{output_root}/{strategy_name}/{YYYYMMDD}/`` 下的产物是否齐全且语义正确.

    返回错误列表 (空列表 = 全通). 检查项:
      - diagnostics.json 存在, status 不是 failed
      - metrics.json 存在, n_predictions > 0
      - selections.parquet 存在, 行数 == expected_topk

    require_status_ok=True (默认) 时, status 必须 == "ok";
    False 时允许 "empty" (非交易日 / 当日无数据可推理), 但不允许 "failed".
    """
    import pandas as pd

    errors: List[str] = []
    day_str = day.strftime("%Y%m%d")
    out_day = Path(output_root) / strategy_name / day_str

    if not out_day.exists():
        return [f"[{strategy_name}/{day_str}] 目录不存在: {out_day}"]

    diag_p = out_day / "diagnostics.json"
    if not diag_p.exists():
        errors.append(f"[{strategy_name}/{day_str}] diagnostics.json 缺失")
        return errors  # 后续断言基于 diagnostics, 缺则跳过

    diag = json.loads(diag_p.read_text(encoding="utf-8"))
    status = diag.get("status")
    if status == "failed":
        errors.append(f"[{strategy_name}/{day_str}] diagnostics status=failed: {diag.get('error_message', '?')}")
        return errors  # failed 后续断言无意义
    if require_status_ok and status != "ok":
        errors.append(f"[{strategy_name}/{day_str}] diagnostics status={status} (期望 ok)")
    if status == "ok" and diag.get("rows", 0) <= 0:
        errors.append(f"[{strategy_name}/{day_str}] diagnostics rows=0 但 status=ok")

    # status=empty 时 metrics/selections 不会写, 跳过
    if status not in ("ok",):
        return errors

    m_p = out_day / "metrics.json"
    if not m_p.exists():
        errors.append(f"[{strategy_name}/{day_str}] metrics.json 缺失")
    else:
        m = json.loads(m_p.read_text(encoding="utf-8"))
        if m.get("n_predictions", 0) <= 0:
            errors.append(f"[{strategy_name}/{day_str}] metrics n_predictions={m.get('n_predictions')}")

    sel_p = out_day / "selections.parquet"
    if not sel_p.exists():
        errors.append(f"[{strategy_name}/{day_str}] selections.parquet 缺失")
    else:
        sel = pd.read_parquet(sel_p)
        if len(sel) != expected_topk:
            errors.append(f"[{strategy_name}/{day_str}] selections rows={len(sel)} (期望 {expected_topk})")

    return errors


# =====================================================================
# Sim gateway 撮合断言 (验证下单链路真实进了 gateway)
# =====================================================================


def assert_orders_for_gateway(
    gateway_name: str,
    strategy_name: str,
    days: List[date],
    *,
    sim_db_dir: str,
    min_trades_per_day: int = 1,
) -> List[str]:
    """查 ``sim_<gateway_name>.db`` 的 sim_trades 表, 校验每日有撮合记录.

    sim_trades 关键列: ``account_id``, ``reference`` (格式 ``"{strategy_name}:{i}"``),
    ``datetime`` (ISO 格式 ``"2026-01-28T09:30:00"``, 撮合时间 = 次日开盘).

    ``days`` 是发单日列表; 撮合发生在 day+1 的 09:30, 所以查 trade datetime
    LIKE ``{day+1}T%`` 才能匹配.

    Returns
    -------
    错误列表; 空 = 全通.
    """
    db_path = Path(sim_db_dir) / f"sim_{gateway_name}.db"
    if not db_path.exists():
        return [f"[{gateway_name}] sim DB 不存在: {db_path}"]

    errors: List[str] = []
    conn = sqlite3.connect(str(db_path))
    try:
        for day in days:
            settle_day = day + timedelta(days=1)
            # 撮合日可能跳到下一交易日 (周末/节假日), 用 LIKE 前缀宽松匹配
            # day+1 ~ day+5 区间内 reference 以 strategy_name 开头的 trades
            n = conn.execute(
                "SELECT COUNT(*) FROM sim_trades "
                "WHERE reference LIKE ? AND datetime >= ? AND datetime < ?",
                (
                    f"{strategy_name}:%",
                    settle_day.strftime("%Y-%m-%dT00:00:00"),
                    (settle_day + timedelta(days=7)).strftime("%Y-%m-%dT00:00:00"),
                ),
            ).fetchone()[0]
            if n < min_trades_per_day:
                errors.append(
                    f"[{gateway_name}/{strategy_name}/{day}] sim_trades count={n} "
                    f"(期望 >= {min_trades_per_day}, 撮合窗口 {settle_day} ~ +7d)"
                )
    finally:
        conn.close()
    return errors


# =====================================================================
# Live date 解析 — 当前是否可推理
# =====================================================================


def resolve_live_date(
    is_trade_date_fn: Callable[[date], bool],
    *,
    tushare_ready_hour: int = 20,
    fallback_lookback_days: int = 10,
) -> date:
    """返回最近 "已完整收盘" 的交易日.

    策略:
      1. today 是交易日 且 当前 ``hour >= tushare_ready_hour`` → today
         (tushare 当日 daily bar 通常 20:00 后落盘)
      2. 否则从 today-1 往前找 ``fallback_lookback_days`` 天里最近的交易日
    """
    from datetime import datetime as _dt

    today = date.today()
    if _dt.now().hour >= tushare_ready_hour:
        try:
            if is_trade_date_fn(today):
                return today
        except Exception:  # noqa: BLE001
            pass

    candidate = today - timedelta(days=1)
    for _ in range(fallback_lookback_days):
        try:
            if is_trade_date_fn(candidate):
                return candidate
        except Exception:  # noqa: BLE001
            return candidate  # 查失败保守取
        candidate -= timedelta(days=1)
    return candidate
