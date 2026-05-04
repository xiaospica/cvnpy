# 开发者扩展手册

写新策略 / 接入新 Gateway / 加监控指标 / 改回放逻辑 — 改 vnpy_ml_strategy 时
应该看哪里 + 怎么改 + 测试体系怎么用. 本文档假定读者熟悉 vnpy 主框架与 qlib
基础.

---

## 1. 代码结构再讲

```
vnpy_ml_strategy/
├── base.py                  常量 / 枚举: Stage, InferenceStatus, EVENT_ML_*
├── engine.py                MLEngine — vnpy 主进程引擎 (BaseEngine 子类)
│                              ├─ scheduler            APScheduler 后台线程
│                              ├─ strategies           策略实例注册表 {name → MLStrategyTemplate}
│                              ├─ strategy_classes     策略类注册表 {class_name → type}
│                              ├─ _model_registry      bundle 元数据校验 (filter_config)
│                              ├─ _metrics_cache       MetricsCache (内存最近 30 日)
│                              ├─ _ic_backfill_services IC 回填 service (每策略一个)
│                              └─ _orderid_to_strategy 订单回报路由表
├── template.py              MLStrategyTemplate — 策略模板基类 ⭐
│                              ├─ on_init / on_start / on_stop  生命周期
│                              ├─ run_daily_pipeline            21:00 cron 入口 (推理 + persist)
│                              ├─ run_open_rebalance            09:26 cron 入口 (rebalance + send_order)
│                              ├─ select_topk                   topk 选股 (子类可 override)
│                              ├─ persist_selections            写 selections.parquet
│                              ├─ rebalance_to_target           generate_orders 主体
│                              ├─ _calculate_buy_amount         qlib 等权 risk_degree 公式
│                              ├─ _run_replay_loop              回放后台线程入口
│                              ├─ _replay_loop_body             回放主循环 (batch + 逐日 apply)
│                              ├─ _link_selections_from_upstream P2-1 影子策略
│                              └─ _persist_replay_equity_snapshot A1/B2 写本地 db
├── strategies/
│   └── qlib_ml_strategy.py  QlibMLStrategy — 默认实现 (空 override, 全部用模板默认)
├── predictors/
│   ├── base.py
│   ├── qlib_predictor.py    QlibPredictor — spawn 子进程接口
│   └── model_registry.py    ModelRegistry — bundle/filter_config 校验缓存
├── replay_history.py        write_snapshot / list_snapshots — A1/B2 本地 SQLite
├── topk_dropout_decision.py qlib TopkDropoutStrategy 算法纯函数版 ⭐
├── monitoring/
│   ├── cache.py             MetricsCache — thread-safe ring buffer
│   └── publisher.py         publish_metrics — 原子写 latest.json + 发事件
├── services/
│   └── ic_backfill.py       IC 回填后台 service
├── persistence/
│   └── selections_writer.py persist_selections 内部实现
└── utils/
    └── trade_calendar.py    QlibCalendar — 加载真实交易日历 (避免春节误判)
```

---

## 2. 写一个新策略

### 2.1 最简继承 (复用全部默认行为)

```python
# F:/Quant/vnpy/vnpy_strategy_dev/vnpy_ml_strategy/strategies/my_new_strategy.py
from vnpy_ml_strategy.template import MLStrategyTemplate


class MyNewStrategy(MLStrategyTemplate):
    """复用 qlib TopkDropoutStrategy 信号 + vnpy_qmt_sim/真 QMT 撮合.

    本类不 override 任何方法 → 100% 走父类默认逻辑:
      - 21:00 cron 推理 + persist selections.parquet
      - 09:26 cron rebalance + send_order
      - 回放 (sim 模式 enable_replay=True)
      - 信号同步 (signal_source_strategy 非空时影子模式)
    """
    pass
```

注册到 MLEngine:
```python
# vnpy_ml_strategy/engine.py 的 _autoload_strategy_classes 里加:
def _autoload_strategy_classes(self) -> None:
    from .strategies.qlib_ml_strategy import QlibMLStrategy
    self.register_strategy_class(QlibMLStrategy)
    from .strategies.my_new_strategy import MyNewStrategy   # ← 新加
    self.register_strategy_class(MyNewStrategy)
```

`run_ml_headless.py` STRATEGIES 中用 `"strategy_class": "MyNewStrategy"`.

### 2.2 自定义 select_topk (改信号选股逻辑)

```python
class MyHybridStrategy(MLStrategyTemplate):
    def select_topk(self, pred_df: pd.DataFrame) -> pd.DataFrame:
        """除 top-K 外, 加自定义过滤 (e.g. 行业分散)."""
        candidates = pred_df.sort_values("score", ascending=False).head(self.topk * 3)
        # ... 应用行业过滤 / 风格中性 ...
        return candidates.head(self.topk)
```

### 2.3 自定义 rebalance_to_target (改算法)

不推荐 — 你会失去 qlib bit-equal 保证. 但如果一定要做 (比如改成 mean-variance
optimization 而非 topk):

```python
class MyMVStrategy(MLStrategyTemplate):
    def rebalance_to_target(self, pred_score, on_day=None) -> dict:
        # 不调 topk_dropout_decision, 自己算 sells/buys
        ...
        # 但要保留 _calculate_buy_amount + send_order 路径, 撮合不变
        for vt in buys:
            volume = self._calculate_buy_amount(ref_price, current_cash, len(buys))
            self.send_order(vt, Direction.LONG, ..., volume=volume)
```

⚠️ 这种 override 会让 e2e (test_topk_e2e_*) 测试不再适用 — 必须自己写新 e2e.

### 2.4 自定义 metrics

子类在 `run_daily_pipeline` 后调 `_publish_metrics(extra_metrics)`:

```python
class MyStrategy(MLStrategyTemplate):
    def run_daily_pipeline(self, as_of_date=None):
        super().run_daily_pipeline(as_of_date)
        # 算自己的指标
        my_metric = self._compute_custom_metric(...)
        # publish_metrics 会进入 MetricsCache + 发 EVENT_ML_METRICS
        self._publish_metrics({"my_custom_indicator": my_metric})
```

mlearnweb 端拉取已经走 `MetricsCache.get_latest`, 自动包含新字段 (前端要展示
则需要在 mlearnweb frontend types 加新字段, 详见 mlearnweb 文档).

---

## 3. 接入新 Gateway

### 3.1 新增一个 sim 类 gateway (e.g. 期货 sim)

```python
# F:/Quant/vnpy/vnpy_strategy_dev/vnpy_qmt_sim_futures/gateway.py
from vnpy_qmt_sim.gateway import QmtSimGateway

class QmtSimFuturesGateway(QmtSimGateway):
    default_name = "QMT_SIM_FT"   # 期货 sim 命名约定
    # 重写撮合规则 (T+0 / 多空双向 / 保证金 / ...)
```

更新 `vnpy_common/naming.py` 加新前缀:
```python
def classify_gateway(name: str) -> Literal['live', 'sim']:
    if name.startswith("QMT_SIM_FT"): return "sim"   # 新增
    if name.startswith("QMT_SIM"):    return "sim"
    if name == "QMT":                 return "live"
    raise ValueError(...)
```

`run_ml_headless._load_gateway_class` 加分支:
```python
def _load_gateway_class(kind: str):
    if kind == "sim_futures":
        from vnpy_qmt_sim_futures import QmtSimFuturesGateway
        return QmtSimFuturesGateway
    ...
```

### 3.2 新增一个 live 类 gateway (e.g. 港股券商)

类似 §3.1, 但需要:
- 实现完整的 connect / send_order / on_order / on_trade 接口
- 与真实券商 SDK 对接 (RPC / FIX / API)
- **更新 `_validate_startup_config` 中 `n_live > 1` 检查**, 因为可能多个 live
  gateway 是不同类的 (港股 + A 股) 不冲突? 这是设计选择.

详见 [`vnpy_qmt_sim/gateway.py`](../../vnpy_qmt_sim/gateway.py) 作参考实现.

### 3.3 测试新 gateway

参考 [`vnpy_ml_strategy/test/test_dual_gateway_routing.py`](../test/test_dual_gateway_routing.py):
- 实例化 + connect
- send_order / on_trade 路由
- 持久化 / settle
- 命名 validator

---

## 4. 加新监控指标

```python
# vnpy_ml_strategy/monitoring/cache.py 已是 thread-safe ring buffer, 直接加新字段:

# vnpy_ml_strategy/template.py:_publish_metrics
def _publish_metrics(self, metrics: dict, as_of_date=None) -> None:
    # 现有: ic / rank_ic / psi_mean / psi_max / pred_mean / pred_std / n_predictions
    # 新增: 不需要改 cache, 自由加新 key

    metrics["my_new_metric"] = ...
    self.signal_engine.publish_metrics(...)
```

**前端展示**: mlearnweb backend `app/services/vnpy/ml_monitoring_service.py`
默认透传所有 metrics 字段; 前端要展示新字段, 改:
- `mlearnweb/frontend/src/types/liveTrading.ts` (加字段)
- `mlearnweb/frontend/src/pages/live-trading/...` (加 UI)

---

## 5. 改回放逻辑

回放主流程在 `template._run_replay_loop` → `_replay_loop_body` → `_replay_loop_iter`.

### 5.1 加新回放阶段 (e.g. 风险事件检测)

```python
def _replay_loop_iter(self, days, total, prev_day_pred_score, gateway):
    for i, day in enumerate(days):
        # ... 现有: 推理 / rebalance / settle / persist ...

        # 新增: 风险事件检测
        if self._detect_risk_event(day):
            self.write_log(f"[replay] day {day} risk event detected, halt")
            self._emit_risk_event(day)
            # 不下单本日
            continue
```

### 5.2 改 batch 推理参数

`_replay_loop_body` 中:
```python
stats = self.signal_engine.run_inference_range(
    bundle_dir=self.bundle_dir,
    range_start=start, range_end=end,
    lookback_days=self.lookback_days,
    timeout_s=max(3600, total * 30),   # ← 改这里
    ...
)
```

⚠️ 改了之后要更新 [`test_ml_strategy_replay.py`](../test/test_ml_strategy_replay.py)
的相关用例.

---

## 6. 测试体系深入

### 6.1 测试套分层

```
单元测试 (无依赖, < 1s):
  test_topk_dropout_decision.py        算法分支 6 用例
  test_replay_history.py               本地 SQLite 11 用例
  test_template_replay_persist.py      _persist_replay_equity_snapshot 4 用例
  test_signal_source_strategy.py       _link_selections_from_upstream 5 用例

集成测试 (需要 EventEngine, ~5s):
  test_dual_gateway_routing.py         双 sim gateway 路由 5 用例
  test_dual_track_with_fake_live.py    FakeQmt 双轨 9 用例
  test_qmt_sim_*.py                    sim 撮合 / 持久化 / 多 gateway 隔离

E2E 测试 (需要 vnpy 真跑过 + qlib backtest ground truth):
  test_topk_e2e_d_drive.py             持仓 + weight 严格等价
  test_topk_e2e_equity_curve.py        权益曲线严格等价
  test_topk_e2e_algorithm.py           算法层 bit-equal

smoke (跨进程):
  smoke_subprocess.py / smoke_engine_rpc.py / smoke_full_pipeline.py
```

### 6.2 跑单元 + 集成 (最常跑, 最快)

```bash
F:/Program_Home/vnpy/python.exe -m pytest \
  vnpy_ml_strategy/test/test_topk_dropout_decision.py \
  vnpy_ml_strategy/test/test_replay_history.py \
  vnpy_ml_strategy/test/test_template_replay_persist.py \
  vnpy_ml_strategy/test/test_signal_source_strategy.py \
  vnpy_ml_strategy/test/test_dual_gateway_routing.py \
  vnpy_ml_strategy/test/test_dual_track_with_fake_live.py \
  vnpy_ml_strategy/test/test_ml_strategy_replay.py \
  -v
# < 30s, 任何代码改动后必跑
```

### 6.3 跑 E2E 等价验证

需要先有 qlib ground truth + vnpy 真实回放数据. 详见 [`../test/README.md`](../test/README.md)
"端到端 reproduce 流程".

### 6.4 写新测试 — 风格指南

跟着已有测试套, 不发明新风格:

| 类型 | 命名 | 例 |
|---|---|---|
| 单元 | `test_<module>_<feature>.py` | `test_replay_history.py` |
| 集成 | `test_<scenario>.py` | `test_dual_gateway_routing.py` |
| E2E | `test_<comparison>_e2e.py` | `test_topk_e2e_d_drive.py` |
| 工具 | `<purpose>.py` (无 test_ 前缀, pytest 不收集) | `generate_qlib_ground_truth.py` |
| 替身 | `fakes/fake_<thing>.py` | `fake_qmt_gateway.py` |

每个 test 函数一句话 docstring 讲清楚 "覆盖什么 / 期望啥". 不写跨函数 fixture
除非真有共享.

### 6.5 测试 fixture 的位置

| 类型 | 位置 |
|---|---|
| 单测内 fixture (one-off) | 同文件 `@pytest.fixture` |
| 跨测试 fixture (e.g. mem db) | `conftest.py` (模块级 / 包级) |
| 替身类 (fake gateway / fake api) | `fakes/` 子目录 (run_ml_headless 也可 import) |
| 共用 helper 函数 | 文件顶部 `_helper(...)` (不暴露给生产) |

---

## 7. 调试技巧

### 7.1 仅跑某一天回放

```python
# 改 run_ml_headless.py 中策略 setting:
"setting_override": {
    "replay_start_date": "2026-04-28",
    "replay_end_date":   "2026-04-30",   # 只回放 3 天
}
```

### 7.2 单独触发 09:26 rebalance (不等 cron)

```python
# 在 vnpy 主进程的 Python REPL 或脚本里:
ml_engine.run_open_rebalance_now("csi300_lgb_headless", as_of_date=date(2026, 4, 30))
```

### 7.3 看推理子进程 stderr

```bash
# 子进程异常会写到 diagnostics.json error_message
cat D:/ml_output/csi300_lgb_headless/20260430/diagnostics.json | jq

# 子进程 stderr 实时观察 (debug 用):
# qlib_predictor.py:predict 改成不捕获 stderr:
# proc = subprocess.Popen(..., stderr=subprocess.PIPE)  → stderr=None
```

### 7.4 跑 MLEngine 手动触发 (绕过 cron)

```bash
F:/Program_Home/vnpy/python.exe -c "
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
ee = EventEngine()
ee.start()
me = MainEngine(ee)
# ... add_gateway / add_app ...
ml_engine = me.get_engine('MlStrategy')
ml_engine.init_engine()
# 直接调:
ml_engine.run_pipeline_now('csi300_lgb_headless', as_of_date=...)
ml_engine.run_open_rebalance_now('csi300_lgb_headless', as_of_date=...)
"
```

### 7.5 mlearnweb 端排错

```bash
# 后端日志
tail -f D:/vnpy_logs/mlearnweb_live.log
# 关注 [vnpy.client] node=... failed: ...

# 前端 console (浏览器 F12)
# Network 看 /api/live-trading 请求是否 200
```

---

## 8. 跨工程协作 (qlib_strategy_dev)

vnpy_ml_strategy 与 qlib_strategy_dev 通过 **bundle 文件契约** 协作, 不直接 import.
真要跨工程改:

| 文件 | 工程 | 协作点 |
|---|---|---|
| `qlib_strategy_dev/strategy_dev/config.py:UNIVERSE_REGISTRY` | 训练 | 增减 universe |
| `qlib_strategy_dev/vendor/qlib_strategy_core/scripts/export_bundle.py` | 训练 | bundle 5 文件契约 |
| `vnpy_ml_strategy/predictors/model_registry.py` | 实盘 | bundle 校验逻辑 |
| `vnpy_ml_strategy/predictors/qlib_predictor.py` | 实盘 | 推理子进程 CLI 接口 |

跨工程 schema 变更必须**两端同步改 + 跑一次回放 e2e** 确认无回归.

详见 [`docs/deployment_a1_p21_plan.md`](../../docs/deployment_a1_p21_plan.md) §一
(filter_chain_specs 跨端契约的演进过程, 是个反面案例).

---

## 9. 常见陷阱

### 9.1 `gateway.send_order` 路由

策略调 `self.send_order(vt_symbol, ..., gateway=...)`. 如果不传 `gateway` 参数,
走 `self.gateway` 字段 (来自 setting). 双轨场景下务必确认 `gateway` 字段值正确.

### 9.2 `on_order` / `on_trade` 路由

vnpy MainEngine 通过 `vt_orderid` 路由回调到正确策略. `vt_orderid =
"{gateway_name}.{orderid}"` 全局唯一. 策略发单时调
`signal_engine.track_order(vt_orderid, strategy_name)` 登记归属表.

不要直接用 `orderid` (per-gateway 序号, 不唯一).

### 9.3 thread-safety

vnpy EventEngine 是单线程 dispatch, 但 MLEngine.scheduler 是 APScheduler 后台
线程, 推理子进程是独立 Process. 共享状态 (sim_db / replay_history.db / mlearnweb.db)
都用 SQLite WAL 模式; 内存共享 (MetricsCache) 用 RLock.

写新 service 用后台线程时, 不要直接调 vnpy event_engine.put — 会重入死锁.
用 EventEngine.queue 或 publish_metrics 走标准路径.

### 9.4 测试中的 `importlib.reload(run_ml_headless)`

V2 测试用 `importlib.reload(r)` 让 STRATEGIES 改动生效. 但这会重置模块顶部的
副作用 (e.g. `os.environ.setdefault("QS_DATA_ROOT", ...)`). 写新测试时不要依赖
模块顶部的 env state.

---

## 10. 进一步阅读

- [architecture.md](architecture.md) — 整体架构
- [dual_track.md](dual_track.md) — 双轨架构
- [`../test/README.md`](../test/README.md) — 测试体系详细
- [`../template.py`](../template.py) — 策略模板源码 (~1500 行, 注释丰富)
- [`../engine.py`](../engine.py) — MLEngine 源码
- [`../topk_dropout_decision.py`](../topk_dropout_decision.py) — qlib 算法纯函数版
