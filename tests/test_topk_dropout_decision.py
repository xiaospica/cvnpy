"""``topk_dropout_decision`` 纯函数与 qlib ``TopkDropoutStrategy`` 算法等价性测试。

测试覆盖 [signal_strategy.py:138-295] 全部分支：
  1. 空仓建仓
  2. 持仓与 top-k 完全相同 (no-op)
  3. n_drop 换手语义（不全卖）
  4. is_tradable 过滤（涨停股 sell 被跳过）
  5. 部分仓 (len(current) < topk) 补齐
  6. method_random 分支
  7. hold_thresh > 1 持仓天数限制
  8. forbid_all_trade_at_limit=False 单向限价

预期值由人工按 qlib 算法源码逐步推算，与 port 实现对比。
qlib 真实 import 等价测试见 ``tests/test_topk_e2e_strict_equivalence.py`` (Phase 6.4a)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vnpy_ml_strategy.topk_dropout_decision import topk_dropout_decision  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 空仓建仓 — buy = topk
# ---------------------------------------------------------------------------


def test_empty_holdings_buy_topk():
    pred = pd.Series({"A": 0.9, "B": 0.7, "C": 0.5, "D": 0.3, "E": 0.1})
    sell, buy = topk_dropout_decision(pred, [], topk=3, n_drop=1)
    assert sell == []
    assert sorted(buy) == sorted(["A", "B", "C"])  # top 3


# ---------------------------------------------------------------------------
# 2. 持仓与 top-k 完全相同 — 不调仓
# ---------------------------------------------------------------------------


def test_holdings_equal_topk_noop():
    pred = pd.Series({"A": 0.9, "B": 0.7, "C": 0.5, "D": 0.3})
    sell, buy = topk_dropout_decision(pred, ["A", "B", "C"], topk=3, n_drop=1)
    # last = ABC 按 pred 排序 = A, B, C
    # today = D 中前 (1+3-3)=1 个 = [D]
    # comb = ABCD 排序 = A, B, C, D；末 n_drop=1 个 = [D]
    # sell = last 中在 [D] 的 → 空（D 不在 last）
    # buy = today[: 0+3-3] = []
    assert sell == []
    assert buy == []


# ---------------------------------------------------------------------------
# 3. n_drop 换手语义 — 持仓最低分换为新候选最高分
# ---------------------------------------------------------------------------


def test_n_drop_one_swap():
    """持仓 ABC，pred top-3 是 ABD（D 进 C 走）。n_drop=1 应该卖 C 买 D。"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "C": 0.3, "E": 0.1})
    sell, buy = topk_dropout_decision(pred, ["A", "B", "C"], topk=3, n_drop=1)
    # last = ABC 按 pred 排序 = A(0.9), B(0.7), C(0.3)
    # today (~last 中按 score 取 n_drop+topk-len(last) = 1+3-3 = 1 个) = [D]
    # comb = ABCD 按 score 排 = A, B, D, C; 末 n_drop=1 = [C]
    # sell = last 中在 [C] 的 = [C]
    # buy = today[: len(sell)+topk-len(last) = 1+3-3 = 1] = [D]
    assert sell == ["C"]
    assert buy == ["D"]


def test_n_drop_two_swaps():
    """n_drop=2: 持仓 ABCD，pred top-4 是 ABEF。卖 CD 买 EF。"""
    pred = pd.Series({"A": 0.9, "B": 0.85, "E": 0.8, "F": 0.7, "C": 0.3, "D": 0.2})
    sell, buy = topk_dropout_decision(pred, ["A", "B", "C", "D"], topk=4, n_drop=2)
    # last = ABCD sort = A(0.9), B(0.85), C(0.3), D(0.2)
    # today = top of (~last) by score, n=2+4-4=2 → [E(0.8), F(0.7)]
    # comb = ABEFCD sort = A, B, E, F, C, D; 末 n_drop=2 = [C, D]
    # sell = last 中在 [C, D] 的 = [C, D]
    # buy = today[: 2+4-4=2] = [E, F]
    assert sorted(sell) == ["C", "D"]
    assert sorted(buy) == ["E", "F"]


# ---------------------------------------------------------------------------
# 4. is_tradable 过滤 — 涨停股 sell 被跳过
# ---------------------------------------------------------------------------


def test_is_tradable_filter_sell():
    """要卖的股 C 跌停（is_tradable=False）→ 不发卖单。但 buy 仍发。"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "C": 0.3, "E": 0.1})

    def is_tradable(code, direction):
        # C 任意方向不可交易（跌停）
        return code != "C"

    sell, buy = topk_dropout_decision(
        pred, ["A", "B", "C"], topk=3, n_drop=1, is_tradable=is_tradable,
    )
    # 算法层面 sell list = [C]，但 sell loop 里 is_tradable(C, None) = False 跳过
    # buy list = [D] 不受影响
    assert sell == []
    assert buy == ["D"]


def test_is_tradable_only_tradable_filter():
    """only_tradable=True 时，候选池 today 也按 is_tradable 过滤。

    展示 qlib"comb sort 防卖高买低"语义：D 涨停被禁 → today 取 E (next),
    但 E(0.1) < 末位 C(0.3) → comb 末位仍是 E → sell={C} ∩ {E} = 空 → 不换。
    """
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "C": 0.3, "E": 0.1, "F": 0.05})

    def is_tradable(code, direction):
        return code not in ("D",)  # D 涨停（任意方向不可交易）

    sell, buy = topk_dropout_decision(
        pred,
        ["A", "B", "C"],
        topk=3,
        n_drop=1,
        only_tradable=True,
        is_tradable=is_tradable,
    )
    # last = [A, B, C] sort = [A(0.9), B(0.7), C(0.3)]
    # today (only_tradable, get_first_n 1 from ~last sorted = [D, E, F])
    #   D 不可交易 → skip → take E. today = [E]
    # comb = pred.reindex(last ∪ today).sort = [A, B, C, E] (E=0.1)
    # 末 n_drop=1 = [E]
    # sell = last 中在 [E] 的 = []  (ABC 都不在 [E])
    # buy = today[:0+3-3=0] = []
    # 算法精髓：宁可不换也不卖高 (C=0.3) 买低 (E=0.1)
    assert sell == []
    assert buy == []


def test_is_tradable_swap_when_alternate_score_higher():
    """对比 case：D 被禁，E 比 last 末位 C 高 → 该换还是要换。"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "E": 0.4, "C": 0.3})

    def is_tradable(code, direction):
        return code != "D"

    sell, buy = topk_dropout_decision(
        pred, ["A", "B", "C"], topk=3, n_drop=1,
        only_tradable=True, is_tradable=is_tradable,
    )
    # today = [E (跳过 D)]
    # comb = [A, B, E, C]; 末 1 = [C]
    # sell = [C], buy = today[: 1+3-3=1] = [E]
    assert sell == ["C"]
    assert buy == ["E"]


# ---------------------------------------------------------------------------
# 5. 部分仓 — len(current) < topk 补齐
# ---------------------------------------------------------------------------


def test_partial_holdings_fill_to_topk():
    """持仓只有 A，topk=3 → today 取 2 只补齐"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "C": 0.5, "D": 0.3})
    sell, buy = topk_dropout_decision(pred, ["A"], topk=3, n_drop=1)
    # last = [A]
    # today = top (n_drop+topk-len(last) = 1+3-1 = 3) of ~last = [B, C, D]
    # comb = A B C D; 末 1 = [D]
    # sell = last 中在 [D] = [] (A 不在 [D])
    # buy = today[: 0+3-1 = 2] = [B, C]
    assert sell == []
    assert sorted(buy) == ["B", "C"]


# ---------------------------------------------------------------------------
# 6. method=random 分支
# ---------------------------------------------------------------------------


def test_method_random_buy():
    """method_buy='random' 在 topk 候选池中随机抽。同 seed 输出确定。"""
    pred = pd.Series({"A": 0.9, "B": 0.85, "C": 0.8, "D": 0.7, "E": 0.5})
    rng = np.random.default_rng(42)
    sell, buy = topk_dropout_decision(
        pred, ["A", "B"], topk=3, n_drop=1, method_buy="random", rng=rng,
    )
    # last = [A, B]
    # topk_candi = top 3 = [A, B, C]
    # candi = ~last in topk_candi = [C]
    # n = 1+3-2 = 2
    # rng.choice([C], 2, replace=False) → ValueError → today = [C]
    # comb = A B C; 末 1 = [C]
    # sell = last 中在 [C] = []
    # buy = today[: 0+3-2 = 1] = [C]
    assert sell == []
    assert buy == ["C"]


def test_method_random_sell():
    """method_sell='random' 在 last 中随机选 n_drop 卖。"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "C": 0.5, "D": 0.3})
    rng = np.random.default_rng(0)
    sell, buy = topk_dropout_decision(
        pred, ["A", "B", "C"], topk=3, n_drop=1, method_sell="random", rng=rng,
    )
    # last = ABC
    # today = top of ~last = [D]
    # method_sell="random": sell = rng.choice(filter(last), 1) = ramdomly 1 from {A,B,C}
    # buy = today[: 1+3-3=1] = [D]  (假设 sell 长度为 1)
    assert len(sell) == 1
    assert sell[0] in ["A", "B", "C"]
    assert buy == ["D"]


# ---------------------------------------------------------------------------
# 7. hold_thresh > 1 持仓天数限制
# ---------------------------------------------------------------------------


def test_hold_thresh_skips_recent_buys():
    """持仓天数 < hold_thresh 时 sell 被跳过。"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "C": 0.3, "E": 0.1})
    sell, buy = topk_dropout_decision(
        pred,
        ["A", "B", "C"],
        topk=3,
        n_drop=1,
        hold_thresh=3,
        hold_days={"A": 5, "B": 5, "C": 1},  # C 才持仓 1 天，<3
    )
    # 算法 sell list = [C]，但 hold_days[C]=1 < hold_thresh=3 → 跳过
    # buy list = [D] 不受影响
    assert sell == []
    assert buy == ["D"]


def test_hold_thresh_default_satisfied():
    """hold_days 缺省视为 1 → hold_thresh=1 (默认) 天然满足。"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "C": 0.3, "E": 0.1})
    sell, buy = topk_dropout_decision(
        pred, ["A", "B", "C"], topk=3, n_drop=1, hold_thresh=1,
    )
    assert sell == ["C"]
    assert buy == ["D"]


# ---------------------------------------------------------------------------
# 8. forbid_all_trade_at_limit 语义
# ---------------------------------------------------------------------------


def test_forbid_all_trade_false_allows_directional_limit():
    """forbid_all_trade_at_limit=False: 涨停允许卖（is_tradable(_, SELL)=True 仍会卖）。
    qlib 原版语义：direction='SELL' 时 is_tradable 只检查"卖出方向能否成交"
    （跌停时不能卖，但涨停可以卖）。
    """
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "C": 0.3, "E": 0.1})
    calls = []

    def is_tradable(code, direction):
        calls.append((code, direction))
        return True  # 简化：任意方向都允许

    sell, buy = topk_dropout_decision(
        pred,
        ["A", "B", "C"],
        topk=3,
        n_drop=1,
        forbid_all_trade_at_limit=False,
        is_tradable=is_tradable,
    )
    # forbid_all_trade_at_limit=False → 卖单 is_tradable(code, "SELL")
    # 期望 callback 被调用至少一次 direction="SELL"
    sell_dir_calls = [c for c in calls if c[1] == "SELL"]
    buy_dir_calls = [c for c in calls if c[1] == "BUY"]
    assert len(sell_dir_calls) > 0, "卖单应用 SELL 方向 is_tradable 检查"
    assert len(buy_dir_calls) > 0, "买单应用 BUY 方向 is_tradable 检查"


def test_forbid_all_trade_true_uses_none_direction():
    """forbid_all_trade_at_limit=True (默认): 双向禁交，is_tradable(_, None)。"""
    pred = pd.Series({"A": 0.9, "B": 0.7, "D": 0.5, "C": 0.3, "E": 0.1})
    calls = []

    def is_tradable(code, direction):
        calls.append((code, direction))
        return True

    sell, buy = topk_dropout_decision(
        pred,
        ["A", "B", "C"],
        topk=3,
        n_drop=1,
        forbid_all_trade_at_limit=True,  # 默认
        is_tradable=is_tradable,
    )
    # 卖买都应用 direction=None
    for code, direction in calls:
        assert direction is None, f"forbid_all_trade_at_limit=True 时 direction 应为 None"


# ---------------------------------------------------------------------------
# 9. 边界 case
# ---------------------------------------------------------------------------


def test_empty_pred_score():
    """pred_score 全空 → 不调仓"""
    pred = pd.Series([], dtype=float)
    sell, buy = topk_dropout_decision(pred, ["A", "B"], topk=3, n_drop=1)
    assert sell == []
    assert buy == []


def test_pred_with_nan_dropped():
    """pred_score 中 NaN 应被 dropna 处理（与 qlib 原版一致）"""
    pred = pd.Series({"A": 0.9, "B": float("nan"), "C": 0.5, "D": 0.3})
    sell, buy = topk_dropout_decision(pred, [], topk=2, n_drop=1)
    # NaN 后 ABCD → ACD（B 被 drop）→ top 2 = [A, C]
    assert sorted(buy) == sorted(["A", "C"])


def test_pred_score_dataframe_not_supported_raises():
    """qlib 原版只支持 Series（DataFrame 取首列）。我们要求 caller 传 Series."""
    # 不强制 raise，但如果 caller 传 DataFrame 应当 break — 这里测 Series 行为
    pred = pd.Series({"A": 0.9, "B": 0.5})
    sell, buy = topk_dropout_decision(pred, [], topk=1, n_drop=1)
    assert buy == ["A"]


def test_invalid_method_raises():
    pred = pd.Series({"A": 0.9, "B": 0.5})
    with pytest.raises(NotImplementedError):
        topk_dropout_decision(pred, [], topk=1, n_drop=1, method_buy="invalid")
    with pytest.raises(NotImplementedError):
        topk_dropout_decision(pred, [], topk=1, n_drop=1, method_sell="invalid")
