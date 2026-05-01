"""qlib ``TopkDropoutStrategy.generate_trade_decision`` 的纯函数 port。

为什么 port 而不是直接 import qlib：
  1. vnpy 主进程跑 Python 3.13 (PyQt/vnpy 依赖)，qlib 0.9.7 仅兼容 3.11
  2. qlib.init() + 加载 instruments/calendars 启动 ~3s，每天调用昂贵
  3. qlib Exchange 用 qlib_data_bin (hfq) 回答 is_stock_tradable / get_deal_price，
     vnpy 撮合用原始价 daily_merged，决策与撮合用同一份数据更一致
  4. 训练-实盘双端共用算法的"真理来源"应该是**简单可读的纯函数**，不是
     一个 qlib 子进程黑盒

源代码参考: F:/Quant/code/qlib_strategy_dev/vendor/qlib_strategy_core/qlib/contrib/strategy/signal_strategy.py:138-295

**等价性测试**: tests/test_topk_dropout_decision.py 用同样 input 跑 qlib 原版 +
本 port，逐 case 验证 sell_list / buy_list 完全一致。

qlib 原版差异（**仅在不影响输出的实现细节上**）：
  - 不依赖 ``qlib.backtest.position.Position`` 对象，传入 List[str] 即可
  - 不依赖 ``qlib.backtest.exchange.Exchange``，is_tradable 用 callback
  - 不返回 ``Order`` / ``TradeDecisionWO`` 对象，只返回 (sell_codes, buy_codes)
  - amount 计算由调用方处理（vnpy 端用 cash × risk_degree / n_buys / price 公式）
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# 与 qlib OrderDir 对应（qlib OrderDir.SELL=0, BUY=1，但 callback 用 string 更通用）
DIR_SELL = "SELL"
DIR_BUY = "BUY"


def topk_dropout_decision(
    pred_score: pd.Series,
    current_holdings: List[str],
    *,
    topk: int,
    n_drop: int,
    method_buy: str = "top",
    method_sell: str = "bottom",
    only_tradable: bool = False,
    forbid_all_trade_at_limit: bool = True,
    hold_thresh: int = 1,
    hold_days: Optional[Dict[str, int]] = None,
    is_tradable: Optional[Callable[[str, str], bool]] = None,
    rng: Optional[np.random.Generator] = None,
) -> Tuple[List[str], List[str]]:
    """与 qlib ``TopkDropoutStrategy.generate_trade_decision`` 算法等价的纯函数。

    Parameters
    ----------
    pred_score : pd.Series
        index = instrument code (ts_code 格式), value = 模型预测分数。
    current_holdings : List[str]
        当前持仓的 instrument list（顺序无关）。
    topk : int
        目标持仓股票数。
    n_drop : int
        每日换手数（qlib 默认每日卖 n_drop 只换 n_drop 只）。
    method_buy : "top" | "random"
        买入候选选择方式：top = 按 score 取最高的；random = 在 topk 候选池中随机。
    method_sell : "bottom" | "random"
        卖出选择方式：bottom = 按 score 取最低的；random = last 中随机。
    only_tradable : bool
        True 时仅在可交易股票中做决策；is_tradable 必须提供。
    forbid_all_trade_at_limit : bool
        涨跌停时双向禁交（qlib 默认 True）。卖单方向用 None 等价禁交，
        否则只禁与方向"对冲"的（如 sell 时跌停就禁，但涨停允许卖）。
    hold_thresh : int
        最小持仓天数。卖出前检查 hold_days[code] >= hold_thresh。
    hold_days : Dict[str, int], optional
        instrument → 已持仓天数（含今日）。缺省视为 1（A 股 T+1 天然满足）。
    is_tradable : (code, direction) -> bool, optional
        可交易性 callback。direction in {"BUY", "SELL", None}（None=任意方向）。
        缺省视为全部可交易。
    rng : np.random.Generator, optional
        method_buy="random" 或 method_sell="random" 时使用，便于单测复现。

    Returns
    -------
    (sell_codes, buy_codes) : (List[str], List[str])
        sell_codes — 今日要卖出的 instrument list
        buy_codes  — 今日要买入的 instrument list
        amount 由调用方计算（vnpy 端：sell 全卖 yd_volume，buy 走等权 risk_degree）
    """
    # 算法本身不需要 hold_days/is_tradable 时给默认 callable
    if is_tradable is None:
        def _always_tradable(_code: str, _direction: Optional[str]) -> bool:
            return True
        is_tradable = _always_tradable
    if hold_days is None:
        hold_days = {}
    if rng is None:
        rng = np.random.default_rng()

    # qlib 原版：pred_score 中不能含 nan，否则 sort_values 行为不一致
    pred_score = pred_score.dropna()

    # 边界：pred_score 为空 → 不调仓
    if pred_score.empty:
        return [], []

    # =========================================================================
    # qlib 原版 [signal_strategy.py:150-187] only_tradable 闭包定义
    # =========================================================================
    if only_tradable:
        def get_first_n(li, n, reverse=False):
            """按 li 顺序取前 n 个**可交易**的（reverse=True 从尾部取）。"""
            cur_n = 0
            res = []
            for si in reversed(li) if reverse else li:
                if is_tradable(si, None):  # qlib 原版用 direction=None
                    res.append(si)
                    cur_n += 1
                    if cur_n >= n:
                        break
            return res[::-1] if reverse else res

        def get_last_n(li, n):
            return get_first_n(li, n, reverse=True)

        def filter_stock(li):
            return [si for si in li if is_tradable(si, None)]
    else:
        def get_first_n(li, n):
            return list(li)[:n]

        def get_last_n(li, n):
            return list(li)[-n:]

        def filter_stock(li):
            return list(li)

    # =========================================================================
    # 主算法 [signal_strategy.py:189-294]
    # =========================================================================
    # last position (sorted by score) — qlib reindex 是确保 last 与 pred_score 对齐
    last = pred_score.reindex(current_holdings).sort_values(ascending=False).dropna().index

    # The new stocks today want to buy **at most**
    if method_buy == "top":
        today = get_first_n(
            pred_score[~pred_score.index.isin(last)].sort_values(ascending=False).index,
            n_drop + topk - len(last),
        )
    elif method_buy == "random":
        topk_candi = get_first_n(pred_score.sort_values(ascending=False).index, topk)
        candi = list(filter(lambda x: x not in last, topk_candi))
        n = n_drop + topk - len(last)
        try:
            today = rng.choice(candi, n, replace=False).tolist()
        except ValueError:  # 候选数 < n
            today = candi
    else:
        raise NotImplementedError(f"method_buy={method_buy!r} not supported")

    # combine(new stocks + last stocks) — sort by score 再选末尾 n_drop 卖出
    # 防止"卖高分买低分"
    comb = pred_score.reindex(last.union(pd.Index(today))).sort_values(ascending=False).index

    # Get the stock list we really want to sell (After filtering "sell high buy low")
    if method_sell == "bottom":
        sell = last[last.isin(get_last_n(comb, n_drop))]
    elif method_sell == "random":
        candi = filter_stock(last)
        try:
            sell = pd.Index(rng.choice(candi, n_drop, replace=False) if len(last) else [])
        except ValueError:
            sell = pd.Index(candi)
    else:
        raise NotImplementedError(f"method_sell={method_sell!r} not supported")

    # Get the stock list we really want to buy
    buy = today[: len(sell) + topk - len(last)]

    # =========================================================================
    # 卖单生成 [signal_strategy.py:232-262]
    # =========================================================================
    sell_codes: List[str] = []
    for code in current_holdings:
        # qlib direction 语义：forbid_all_trade_at_limit=True → direction=None (双向禁交)
        # forbid_all_trade_at_limit=False → SELL 只禁卖出方向 (跌停)
        sell_check_dir = None if forbid_all_trade_at_limit else DIR_SELL
        if not is_tradable(code, sell_check_dir):
            continue
        if code in sell:
            # check hold limit (默认所有持仓 1 天，A 股 T+1 天然满足 hold_thresh=1)
            held = hold_days.get(code, 1)
            if held < hold_thresh:
                continue
            sell_codes.append(code)

    # =========================================================================
    # 买单生成 [signal_strategy.py:271-294]
    # =========================================================================
    buy_codes: List[str] = []
    for code in buy:
        buy_check_dir = None if forbid_all_trade_at_limit else DIR_BUY
        if not is_tradable(code, buy_check_dir):
            continue
        buy_codes.append(code)

    return sell_codes, buy_codes
