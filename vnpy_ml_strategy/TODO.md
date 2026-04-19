# vnpy_ml_strategy 待补事项

Phase 2 (2026-04-19) 落地时暂留的占位。这几点都需要对接 vnpy 实时
行情或真实 bundle 数据才能验证,待模拟/实盘接入后再修复。

## 1. 按日磁盘扫描查询预测 summary

**位置**: [`vnpy_webtrader/routes_ml.py`](../vnpy_webtrader/routes_ml.py)
`GET /api/v1/ml/strategies/{name}/prediction/{yyyymmdd}`

**当前行为**: 直接返回 HTTP 501。

**应实现**: 从 `{output_root}/{strategy_name}/{yyyymmdd}/metrics.json` +
`predictions.parquet` 读数据。已有目录契约(`ResultStore.day_dir` 的布局)
和 schema(同 `prediction/latest/summary`),只需在 WebEngine 新增一个 RPC
方法 `get_ml_prediction_by_date(name, yyyymmdd)` 扫磁盘组装并返回。

**阻塞**: 需要真实跑过的推理输出目录(live 跑一轮 qlib_ml_strategy 后才能
有测试数据)。

## 2. `_is_limit_up` 对接 vnpy 实时 tick

**位置**: [`strategies/qlib_ml_strategy.py`](./strategies/qlib_ml_strategy.py)
`QlibMLStrategy._is_limit_up(vt_symbol: str) -> bool`

**当前行为**: 永远返回 `False`(不过滤涨停)。

**应实现**:
```python
tick = self.main_engine.get_tick(vt_symbol)
if tick is None or tick.limit_up is None or tick.last_price is None:
    return False
# 一字板: 最新价 == 涨停价 且 bid_volume_1 > 0 (意味市场在涨停价挂买单)
return abs(tick.last_price - tick.limit_up) < 1e-4 and (tick.bid_volume_1 or 0) > 0
```

**阻塞**: 需要 QmtGateway(或 QmtSimGateway)接入 + 订阅合约后,才有 tick
可读。在模拟盘冒烟时补齐。

## 3. `_compute_volume` 按 last_price 精确计算手数

**位置**: [`strategies/qlib_ml_strategy.py`](./strategies/qlib_ml_strategy.py)
`QlibMLStrategy._compute_volume(row: pd.Series) -> int`

**当前行为**: 固定返回 100 股。

**应实现**:
```python
tick = self.main_engine.get_tick(vt_symbol)  # 取最新 tick
ref_price = tick.last_price if tick else None
if not ref_price:
    return 100  # 无法算,默认一手兜底
# A 股买入 100 股整手为单位
lots = int((self.cash_per_order / ref_price) / 100) * 100
return max(lots, 100)
```

**阻塞**: 同第 2 项,需要 gateway + 订阅。

## 4. 可选增强(不紧急)

- `QlibMLStrategy` 目前没有实现 `on_tick` / `on_bar`,日频策略本身不依赖,
  但若要接入分钟级回测需要补齐。
- `MLStrategyAdapter.add_strategy` 只是薄包装,完整 CRUD(init/start/stop
  的 live 语义)待有真实使用场景再完善。
- UI widget `MLStrategyManager` 现在只有只读表格。可加"立即触发一次
  pipeline"按钮,手动调 `engine.scheduler.run_job_now(strategy_name)`。
