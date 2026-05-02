# Issue: vnpy 回放与 qlib backtest holdings 自 2026-02-13 起 diverge — **已严格定位 + 修复**

## 严格诊断结果（不是猜测）

通过 `scripts/diagnose_holdings_diverge.py` 逐日 dump 两边状态：

**First divergence day**: 2026-02-25 (在表面)

**真正分歧起点**: **2026-02-16 ~ 02-23 (中国春节假期)**

```
2026-02-13 (Friday)   v=7 q=7 OK    [最后一致日]
2026-02-16 (Monday)   v=7 q=0 DIFF  ← vnpy 跑了 rebalance, qlib 没有
2026-02-17 (Tuesday)  v=7 q=0 DIFF  ← vnpy 跑了 rebalance, qlib 没有
2026-02-18 (Wednesday) v=7 q=0 DIFF
2026-02-19 (Thursday) v=7 q=0 DIFF
2026-02-23 (Monday)   v=7 q=0 DIFF
2026-02-24 (Tuesday)  v=7 q=7      [qlib 春节后第一个交易日]
2026-02-25 (Wednesday) v=7 q=7 DIFF (持仓集合不同 — vnpy 经历 5 天无效 rebalance 累积偏差)
```

## 严格根因（代码层定位）

`vnpy_ml_strategy/template.py::_run_replay_loop` 调 `_is_trade_day` 判断
跳过非交易日。`_is_trade_day` 走 `signal_engine.is_trade_day` →
`engine.py:315`：

```python
def is_trade_day(self, d: date) -> bool:
    if self._trade_calendar is None:
        # 未注入时默认周一至周五
        return d.weekday() < 5
    return self._trade_calendar.is_trade_day(d)
```

**`_trade_calendar` 在 `_run_replay_loop` 启动前从未被 `ensure_trade_calendar`
初始化** —— grep `ensure_trade_calendar` / `set_trade_calendar` 在
`template.py` / `run_ml_headless.py` 全部为空。

```python
class WeekdayFallbackCalendar:
    def is_trade_day(self, d: date) -> bool:
        return d.weekday() < 5  # 不识别中国节假日
```

→ 春节假期 (Mon~Fri 共 5 天) 被误判为交易日。
→ vnpy 回放在春节 5 天**全部跑了 rebalance** (sell + buy)。
→ 累积 5 笔无效交易 → 持仓与 qlib 偏离 → 后续天 sell/buy 选择不同 → 看似 02-25 才 diverge。

## 影响范围

不仅 2026 春节，还包括所有中国节假日 (国庆 / 五一 / 清明 / 端午 / 中秋 /
元旦)。任何回放 / 实盘运行跨节假日都会产生无效 rebalance。

## 修复 (commit 待提交)

`template.py:_run_replay_loop` 启动时立即调:

```python
ensure_cal = getattr(self.signal_engine, "ensure_trade_calendar", None)
if callable(ensure_cal):
    ensure_cal(self.provider_uri)
```

`ensure_trade_calendar(provider_uri)` 从 D:/vnpy_data/qlib_data_bin/calendars/day.txt
加载真实交易日历 → QlibCalendar.is_trade_day() 准确识别春节等节假日。

## 验证

```python
cal = make_calendar(r'D:/vnpy_data/qlib_data_bin')  # QlibCalendar
cal.is_trade_day(date(2026,2,16))  # False ✓ (春节)
cal.is_trade_day(date(2026,2,23))  # False ✓ (春节)
cal.is_trade_day(date(2026,2,24))  # True  ✓ (节后第一个交易日)
```

## 优先级

**高** — 这是数据正确性问题，影响所有跨节假日的回放/实盘行为。修复后应让
`test_holdings_set_strict_equal` PASS。
