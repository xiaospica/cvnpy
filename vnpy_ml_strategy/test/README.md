# vnpy\_ml\_strategy/test/ — 测试与验证脚本统一目录

本目录承载 **vnpy\_ml\_strategy** 应用的所有测试 / 验证 / 诊断脚本。一切跨 `vnpy 实盘回放` 与 `qlib backtest` 的等价性证明都集中在此,目的是让后续 AI / 工程师能从单一入口理解端到端验证体系。

***

## 文件总览

### qlib 等价性 e2e (Phase 6 — 持仓/权重/曲线)

| 类型     | 文件                                | 一句话说明                                                                                                 |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------- |
| pytest | `test_topk_e2e_d_drive.py`        | **Phase 6.4a** — vnpy 回放 vs qlib backtest 持仓 + weight 严格等价                                            |
| pytest | `test_topk_e2e_equity_curve.py`   | vnpy 回放 vs qlib backtest 权益曲线 / 日收益率严格等价                                                              |
| pytest | `test_topk_e2e_algorithm.py`      | `topk_dropout_decision()` 与 qlib `TopkDropoutStrategy` 算法层 bit-equal                                  |
| pytest | `test_topk_dropout_decision.py`   | `topk_dropout_decision()` 纯函数单元测试(6 分支)                                                               |
| pytest | `test_ml_strategy_replay.py`      | replay 控制器 + as\_of\_date 透传 + sim 守门等单测                                                              |

### A1/B2 解耦 — vnpy ↔ mlearnweb.db 跨工程紧耦合根除

| 类型     | 文件                                  | 一句话说明                                                                                                 |
| ------ | ----------------------------------- | ----------------------------------------------------------------------------------------------------- |
| pytest | `test_replay_history.py`            | 本地 `replay_history.db` UPSERT / since 增量 / 路径解析三优先级 (11 用例)                                          |
| pytest | `test_template_replay_persist.py`   | `_persist_replay_equity_snapshot` 写本地 db 单测 (4 用例: cash+持仓市值 / 空仓 / 同 day 幂等 / 写失败不 raise)            |
| 工具脚本   | `fake_webtrader_for_a1_e2e.py`      | 极简 FastAPI 服务模拟 vnpy 节点对外 HTTP API,绕过 RPC 直读 `replay_history.db` — 给 mlearnweb sync_loop 端到端冒烟          |

### P2-1 双轨架构 — 实盘+模拟混部 + 信号同步

| 类型     | 文件                                       | 一句话说明                                                                                                 |
| ------ | ---------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| pytest | `test_signal_source_strategy.py`         | 影子策略 `_link_selections_from_upstream` 单测 (5 用例: 4 产物全 link / hardlink 同 inode / 部分缺失 / 上游不存在 / 重复幂等) |
| pytest | `test_dual_gateway_routing.py`           | **V1** 双 sim gateway 路由验证 (5 用例: DB 隔离 / send_order 路由 / 命名 validator / EventEngine 隔离 / settle 隔离)   |
| pytest | `test_dual_track_with_fake_live.py`      | **V2** 实盘+模拟双轨 + 信号同步 (9 用例: 命名校验双轨 / startup 校验 / 一致性 4 反例 / 字节级信号同步 / FakeQmt drop-in)             |
| fixture | `fakes/fake_qmt_gateway.py`              | `FakeQmtGateway` 替身: `default_name="QMT"` 命名走 live, 内核继承 `QmtSimGateway` 撮合 — 仅 V2 用,部署机不安装          |
| fixture | `fakes/__init__.py`                      | `vnpy_ml_strategy.test.fakes` 包标识 (让 `run_ml_headless.py` 的 `fake_live` kind 分支能 import)             |

> **V3** 真券商仿真账户 — TODO 待下一交易日盘中,详见 [`docs/deployment_a1_p21_plan.md`](../../docs/deployment_a1_p21_plan.md) §三.2.

### 工具脚本 (生成 ground truth + 诊断 + 出图)

| 类型     | 文件                                | 一句话说明                                                                                                 |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------- |
| 脚本     | `generate_qlib_ground_truth.py`   | **生成 qlib ground truth** 按 `--strategy-name` 隔离子目录: `{OUT_DIR_BASE}/{name}/{positions,report,pred}.pkl` |
| 脚本     | `diagnose_holdings_diverge.py`    | 持仓集合 diverge 诊断 — 逐日 dump 找 first divergence day                                                      |
| 脚本     | `diagnose_weight_offset.py`       | weight 残余偏差归因(整百取整 + 撮合价分母 vs settle 浮点累积)                                                            |
| 脚本     | `plot_equity_curve_comparison.py` | 出对比图 `result/equity_curve_comparison_{strategy}.png` (按 strategy_name 隔离)                              |

### smoke 测试 (端到端冒烟)

| 类型     | 文件                                | 一句话说明                                                                                                 |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------- |
| smoke  | `smoke_subprocess.py`             | 单进程 → QlibPredictor → subprocess 推理(最小链路)                                                             |
| smoke  | `smoke_engine_rpc.py`             | MLEngine + WebTrader RPC + 派生 webtrader uvicorn                                                       |
| smoke  | `smoke_full_pipeline.py`          | 全栈一键(含 mlearnweb live\_main 子进程 + ml\_snapshot\_loop tick)                                            |

### 输出 / 子目录

| 路径 | 内容 |
|---|---|
| `result/` | `plot_equity_curve_comparison.py` 输出的对比图 (`equity_curve_comparison_{strategy}.png` × N 策略) |
| `fakes/` | 仅测试 / 开发用 gateway 替身 (P2-1.3 V2 验证), 部署机不安装 |

***

## 数据流 / 文件依赖图

```
                          ┌──────────────────────────────────────┐
                          │  D:/vnpy_data/qlib_data_bin (统一源) │
                          │  + D:/vnpy_data/snapshots/filtered/ │
                          └─────────────┬────────────────────────┘
                                        │
                ┌───────────────────────┼────────────────────────┐
                │                       │                        │
                ▼                       ▼                        ▼
   ┌──────────────────────┐  ┌──────────────────────┐  ┌────────────────────┐
   │ generate_qlib_ground │  │ run_ml_headless.py   │  │ vnpy_qmt_sim       │
   │ _truth.py            │  │ (vnpy 实盘/回放)     │  │ 撮合 + settle      │
   │ qlib backtest        │  │ → 推理 + rebalance   │  │ + persist (sqlite) │
   └──────────┬───────────┘  └──────────┬───────────┘  └─────────┬──────────┘
              │                         │                        │
              ▼                         ▼                        ▼
   C:/Users/richard/AppData/  D:/ml_output/{strategy}/   F:/.../vnpy_qmt_sim/
   Local/Temp/qlib_d_         {YYYYMMDD}/                .trading_state/
   backtest/                  predictions.parquet        sim_QMT_SIM_csi300.db
   ├ pred.pkl                 metrics.json               (sim_trades 表)
   ├ positions_normal_1day.pkl                                    │
   └ report_normal_1day.pkl                                       │
              │                                                   │
              └─────────────┬─────────────────────────────────────┘
                            │
                            ▼
              ┌──────────────────────────────────────┐
              │  pytest e2e tests (本目录)           │
              │  test_topk_e2e_d_drive.py            │  → 持仓 / weight
              │  test_topk_e2e_equity_curve.py       │  → 权益曲线
              │  diagnose_*.py / plot_*.py           │  → 诊断 / 出图
              └──────────────────────────────────────┘
```

***

## 关键约定

### Python 解释器

| 用途                                             | 解释器                                                            | 原因                                                                                                                                   |
| ---------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **pytest 运行(本目录所有 test\_\*)**                  | `F:/Program_Home/vnpy/python.exe`                              | 既能 `import vnpy.*`(vnpy 主栈) 又能 `import qlib`(unpickle qlib `Position` 对象) — `conftest.py` 已把 `vendor/qlib_strategy_core` 加到 sys.path |
| **`generate_qlib_ground_truth.py`(脚本直跑)**      | `E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe` | 需要 qlib 重型依赖(lightgbm / pyarrow / scikit-learn / mlflow),vnpy 主 python 不装这些                                                          |
| **smoke\_\*.py / 实盘** **`run_ml_headless.py`** | `F:/Program_Home/vnpy/python.exe`                              | vnpy 主栈,推理时再 spawn inference\_python 子进程                                                                                             |

### 数据源统一

**所有验证脚本两端的数据驱动都是** **`D:/vnpy_data/qlib_data_bin`** **+** **`D:/vnpy_data/snapshots/filtered/csi300_filtered_*.parquet`** — 这是 vnpy 端日更管道直接 dump 的产物。不允许拿 mlflow 历史 artifacts 当 ground truth(无法保证两端从同一份数据生成)。

### qlib backtest 撮合层数学等价(仅 e2e 验证用)

`generate_qlib_ground_truth.py` 把 qlib 的 `deal_price` 设置为 `"$open"`(hfq open),这样:

```
qlib amount = floor(value × adj / hfq_open / 100) × 100
           = floor(value / raw_open / 100) × 100
           = vnpy amount         (vnpy 用 raw open 撮合)
```

两端撮合层**严格等价**(只有整百取整误差,即每股 ≤ 1 手)。

> **此修改仅在** **`generate_qlib_ground_truth.py`** **内**,不污染训练路径(`tushare_hs300_rolling_train.py` 与 `multi_segment_records.py` 的默认 `deal_price="close"` 完全不变)。

***

## 详细脚本说明

### 1. `test_topk_e2e_d_drive.py`(Phase 6.4a 严格等价测试)

**目的**: 证明 vnpy 实盘回放的每日 sell / buy / 持仓 / 仓位权重 与 qlib backtest 在数学上严格一致(达成可信的 paper-trading)。

**验收条件**:

- `test_holdings_set_strict_equal`: 重叠期内每日**持仓 ts\_code 集合严格相等**(`==`)
- `test_weight_deviation_per_stock`: 每只股 `weight = volume × price / total_equity` 偏差 **< 5%** (max),持仓期内
- 失败时抛 AssertionError 列出 first divergence day + 双边 only\_set

**测试方法**:

```bash
# Step 1: 生成 ground truth (耗时 ~30s)
PYTHONPATH="F:/Quant/code/qlib_strategy_dev/vendor/qlib_strategy_core;F:/Quant/code/qlib_strategy_dev" \
  E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe \
  vnpy_ml_strategy/test/generate_qlib_ground_truth.py

# Step 2: 跑 vnpy 回放(产出 sim_trades + strategy_equity_snapshots)
F:/Program_Home/vnpy/python.exe run_ml_headless.py
# 等回放完成(看日志 [replay] day N/N done)

# Step 3: 跑 e2e 等价测试
F:/Program_Home/vnpy/python.exe -m pytest vnpy_ml_strategy/test/test_topk_e2e_d_drive.py -v
```

**原理**:

1. 加载 `positions_normal_1day.pkl` → qlib 每日 Position 对象
2. 重放 `sim_trades` → vnpy 每日(LONG 累计 / SHORT 减去)→ 当日 EOD 持仓
3. 逐日 set 比较 + weight 比较

### 2. `test_topk_e2e_equity_curve.py`(权益曲线对齐)

**目的**: 在持仓 / 权重等价基础上,进一步验证**复利累积收益率曲线**两端严格一致。

**验收条件**:

- `test_daily_return_strict_equal`: 每日日收益率偏差 max < 2%, 平均 < 0.5%
- `test_cumret_baseline_offset_documentation`: 文档化首日 cumret 基线偏移(信息性 assert)
- `test_daily_return_correlation`: 日收益率 pearson 相关系数 > 0.90

**数据源**:

- vnpy: `mlearnweb.db.strategy_equity_snapshots WHERE source_label='replay_settle'`(`template.py::_persist_replay_equity_snapshot` 每日 EOD 写入)
- qlib: `report_normal_1day.pkl 的 'account' 列`

**原理**: `cumret(T) = total[T] / total[0] - 1`, 两端 `total[0]` 都是 1,000,000(`init_cash`),复利累积天然嵌入(含累积盈亏 + 复投)。

### 3. `test_topk_e2e_algorithm.py`(算法层 bit-equal)

**目的**: 隔离算法决策环节,证明 `topk_dropout_decision()` 纯函数与 qlib 原版 `TopkDropoutStrategy.generate_trade_decision` 在受控输入下输出 sell\_list / buy\_list **完全一致**。

**验收条件**: ≥ 6 个 case 全部双边 set 严格相等。

**原理**: 算法等价是必要条件 — 撮合 / 资金管理在其他测试覆盖,本测试聚焦"信号决策同源"。

### 4. `test_topk_dropout_decision.py`(纯函数单元测试)

**目的**: 验证 `topk_dropout_decision()` 自身分支(空仓首日 / 持仓 = topk 不调仓 / n\_drop 卖低买高 / `is_tradable=False` 跳过 / 部分仓补满 / `method_buy=random`)。

**验收条件**: 6 个 case 全绿。

### 5. `test_ml_strategy_replay.py`(回放控制器单元测试)

**目的**: 验证 `_run_replay_loop` / `as_of_date` 透传 / sim 模式守门 / 续跑幂等等行为。

**验收条件**:

- `as_of_date` 注入 → 子进程 `--live-end` 正确
- 实盘 gateway(`QMT`)→ replay\_status="skipped\_live"
- 显式 `replay_start_date < bundle.test_start` → ValueError
- gateway `enable_auto_settle(False)` 后跨真实自然日不调 settle

### 6. `generate_qlib_ground_truth.py`(ground truth 生成)

**目的**: 用 vnpy 同源数据(`D:/vnpy_data/qlib_data_bin`)跑 qlib backtest, 输出三个 pickle 给 e2e 测试 / 诊断脚本消费。

**输入**:

- `BUNDLE_DIR = qs_exports/rolling_exp/<run_id>`(MLflow 训练产出)
- `PROVIDER_URI = D:/vnpy_data/qlib_data_bin`(vnpy 端日更产物,统一数据源)
- `filter_parquet`: `D:/vnpy_data/snapshots/filtered/csi300_filtered_<YYYYMMDD>.parquet`(覆盖 task.json 训练时固化的 filter)

**输出**(写到 `C:/Users/richard/AppData/Local/Temp/qlib_d_backtest/`):

- `pred.pkl`: 推理 pred\_df(qlib 端,同 vnpy 推理 bit-equal)
- `positions_normal_1day.pkl`: 每日 Position 对象(`amount` / `price` / `weight`)
- `report_normal_1day.pkl`: 每日 account / return / cash / turnover

**关键决策**:

- `deal_price="$open"` (hfq open) — 让 qlib 撮合层与 vnpy raw\_open 撮合数学等价(见上文"撮合层数学等价"段)
- `benchmark="600519.SH"` — provider\_uri 不含指数代码,benchmark 仅用于 excess\_return,我们对比的是 positions/sells/buys 不影响

**运行**:

```bash
PYTHONPATH="F:/Quant/code/qlib_strategy_dev/vendor/qlib_strategy_core;F:/Quant/code/qlib_strategy_dev" \
  E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe \
  f:/Quant/vnpy/vnpy_strategy_dev/vnpy_ml_strategy/test/generate_qlib_ground_truth.py
```

> ⚠️ **仅 E2E 验证用,不在训练路径上**:不影响 `tushare_hs300_rolling_train.py` / `multi_segment_records.py`。

### 7. `diagnose_holdings_diverge.py`(持仓 diverge 严格诊断)

**目的**: 当 `test_topk_e2e_d_drive.py` 失败时,精确定位**第一个 diverge 的日期 + 哪个字段先发散**。不接受猜测 — 逐日 dump qlib state vs vnpy state 强制证据驱动。

**输出**:

1. First divergence day
2. T-1 EOD 持仓集合对比(确保起点同源)
3. T 日 sell / buy 集合对比
4. T-1 pred\_score top10 + 关注股 rank
5. T-1 持仓每股 amount × price 对比(qlib hfq vs vnpy raw)

**运行**:

```bash
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/diagnose_holdings_diverge.py
```

**已用此脚本定位的根因**(见 `docs/known_issues/holdings_diverge_after_2026_02_13.md`):

- 春节假期 vnpy 误判为交易日 → 5 天无效 rebalance → 累积偏差
- 修复: `template.py::_run_replay_loop` 启动调 `ensure_trade_calendar(provider_uri)` 加载真实 qlib 交易日历

### 8. `diagnose_weight_offset.py`(weight 残余偏差归因)

**目的**: 当 weight 残余 \~2% 偏差时,验证它是**整百取整 + 撮合价分母不同**(H1)还是 **settle 浮点累积**(H2)。

**实证方法**: 取一只两端连续持有多日的股,逐日 dump:

- vnpy: `volume`(整数)+ `cost`(`pct_chg` 累乘 mark-to-market 后)
- qlib: `amount`(× `adj`)+ `price`(hfq close)
- 比较每日 `vnpy_mv / qlib_mv` 比例

**判定**:

- H1 成立: 比例**稳定不漂移**(< 0.0002%)
- H2 成立: 比例**逐日漂移**

**已验证结论**: H1 成立(单只股持仓期 ratio drift < 0.0002%),即偏差完全来自买入瞬间的撮合价分母 + 整百取整,settle 浮点累积无影响。

### 9. `plot_equity_curve_comparison.py`(出对比图)

**目的**: 把 vnpy / qlib 权益曲线渲染成 3-panel matplotlib chart(累积收益率叠加 + diff + 日收益率 diff),方便人眼校验。

**输出**: `vnpy_ml_strategy/test/result/equity_curve_comparison_{strategy_name}.png` — **每个策略独立子图**(2026-05 修: 之前共享单一 pkl 导致两策略 qlib 曲线虚假对比, 现按 strategy_name 隔离)

**运行**:

```bash
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/plot_equity_curve_comparison.py csi300_lgb_headless
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/plot_equity_curve_comparison.py csi300_lgb_headless_2
```

***

## A1/B2 解耦相关测试与工具

A1/B2 = 让 vnpy 不再越界写 mlearnweb.db, 改为本地 `replay_history.db` + vnpy_webtrader endpoint 暴露 + mlearnweb sync_loop 拉. 详见 [`docs/deployment_a1_p21_plan.md`](../../docs/deployment_a1_p21_plan.md) §一.

### 10. `test_replay_history.py`(本地 SQLite 单测, 11 用例)

**测试目标**: `vnpy_ml_strategy.replay_history` 模块 — write_snapshot / list_snapshots / count_snapshots / 路径解析三优先级.

**关键覆盖**: UPSERT 语义(同 strategy+ts 重写) / 按 strategy_name 过滤 / ts ASC 排序 / since_iso 增量 (datetime 函数兼容 'T' vs 空格分隔符) / db 不存在返空列表.

```bash
F:/Program_Home/vnpy/python.exe -m pytest vnpy_ml_strategy/test/test_replay_history.py -v
```

### 11. `test_template_replay_persist.py`(主代码 E2E 单测, 4 用例)

**测试目标**: `MLStrategyTemplate._persist_replay_equity_snapshot` 真实写本地 db 闭环.

**关键覆盖**: equity = cash + 持仓市值 / 空仓 / 同 day 重写 UPSERT 仅保 1 行 / 写失败仅 log warn 不 raise (保护回放主循环).

### 12. `fake_webtrader_for_a1_e2e.py`(冒烟工具)

**目的**: 极简 FastAPI uvicorn 服务模拟 vnpy 节点对外 HTTP API, 直接 `import replay_history.list_snapshots` 绕过 vnpy MainEngine + RPC server. 给 mlearnweb 端 `replay_equity_sync_service` 做端到端冒烟.

⚠️ 仅 A1 E2E 验证用. 真实 vnpy_webtrader 实现见 [`vnpy_webtrader/web.py`](../../vnpy_webtrader/web.py).

**运行**(配 mlearnweb `vnpy_nodes.yaml` `base_url=http://127.0.0.1:8001`):

```bash
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/fake_webtrader_for_a1_e2e.py
```

***

## P2-1 双轨架构相关测试

P2-1 = 实盘+模拟混部 + 影子策略复用上游 selections.parquet. 详见 [`docs/deployment_a1_p21_plan.md`](../../docs/deployment_a1_p21_plan.md) §三.

### 13. `test_signal_source_strategy.py`(影子 link 单测, 5 用例)

**测试目标**: `MLStrategyTemplate._link_selections_from_upstream` — 影子策略复用上游 4 个产物.

**关键覆盖**: selections/predictions/diagnostics/metrics 全 link / NTFS hardlink 同 inode (上游覆盖 → 下游自动同步) / 部分缺失不 raise / 上游不存在 → `last_status='empty'` / 重复幂等.

### 14. `test_dual_gateway_routing.py`(V1, 5 用例)

**测试目标**: 双 sim gateway 验证多 Gateway 架构核心 — **路由逻辑与 gateway 类型无关**, sim+sim 跑通即证明 live+sim 也能跑通.

**关键覆盖**:
- R1 EventEngine 同时挂两 gateway 不串味 (gateway_name 各自正确)
- R2 send_order 路由正确 (`sim_orders` 表只在目标 gw 出现)
- R3 持仓+资金 SQLite 物理隔离 (各自 capital + `sim_<gw>.db` 文件)
- R4 命名 validator 双 sim 各自合规
- R5 settle_end_of_day 隔离

### 15. `test_dual_track_with_fake_live.py`(V2, 9 用例)

**测试目标**: 用 `FakeQmtGateway` 替身配合 sim gateway 验证 live+sim 双轨架构 — **不依赖真实盘环境**.

**关键覆盖**:
- 命名 validator 双轨 (live + sim 各自分支)
- 启动期 `_validate_startup_config` 接受 `[fake_live, sim]` 混部
- 双 `kind=live` → ValueError (miniqmt 单进程单账户约束)
- `_validate_signal_source_consistency` 通过对齐 + 4 反例:
  * bundle_dir 不一致 → raise
  * topk 不一致 → raise
  * 链式依赖 (影子的上游也是影子) → raise
- **R8 信号同步字节级等价**: 影子 link 后 selections.parquet md5 == 上游 md5
- FakeQmtGateway drop-in: gateway_name='QMT' / classify=live / md+td 完整 / `enable_auto_settle` 可调

### 16. `fakes/fake_qmt_gateway.py`(P2-1 V2 替身)

**作用**: 继承 `QmtSimGateway` 撮合内核, `default_name="QMT"` 让命名 validator 走 live 分支. 跳过 `validate_gateway_name(expected_class="sim")` 校验, 允许 `gateway_name='QMT'` 即合规.

⚠️ **部署机不应安装** `vnpy_ml_strategy/test/` 目录 — `deploy/install_services.ps1` 必须跳过. 真券商仿真账户 (V3) 验证时 把 `GATEWAYS` 中 `kind=fake_live` 改为 `kind=live` + 类换成真 `vnpy_qmt.QmtGateway` 即可.

***

## smoke 脚本(独立链路,与 e2e 验证解耦)

下面三个 `smoke_*.py` 不参与 qlib 等价验证,纯粹是 vnpy\_ml\_strategy app 自身的链路冒烟。

### `smoke_subprocess.py` — 最小链路

**测试链路**: vnpy main(3.13) → QlibPredictor → subprocess(3.11) → 3 文件契约

```bash
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_subprocess.py
```

**测**: subprocess 启动 / 3 文件落盘 / IC 计算 / 超时 / 失败语义。
**不测**: gateway 连接 / 下单 / 事件传播。

### `smoke_engine_rpc.py` — 全链路 + WebTrader

**测试链路**: 同上 + 起 webtrader uvicorn 子进程

```bash
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_engine_rpc.py
# 另开终端 curl http://127.0.0.1:8001/api/v1/ml/health
```

### `smoke_full_pipeline.py` — 全栈(含 mlearnweb)

**测试链路**: 上述 + 派生 mlearnweb `app.live_main` uvicorn(端口 8100), 等 ml\_snapshot\_loop tick(60s)后做 12 条端到端断言。

```bash
F:/Program_Home/vnpy/python.exe -u vnpy_ml_strategy/test/smoke_full_pipeline.py
```

**前置**: 端口 2014/4102/8001/8100 全空闲,`mlearnweb.db` 写权限。

***

## 端到端 reproduce 流程(从零到验证通过)

```bash
# 1. 同步统一数据源(每日 20:00 cron 自动跑;手动可)
F:/Program_Home/vnpy/python.exe -m vnpy_strategy_dev.vnpy_tushare_pro.daily_ingest

# 2. 训练 → 导出 bundle(在 qlib_strategy_dev 工程跑)
cd /f/Quant/code/qlib_strategy_dev
E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe strategy_dev/tushare_hs300_rolling_train.py
# 记下 mlflow run_id

# 3. 生成 qlib ground truth (按 strategy_name 隔离子目录, 双 bundle 各跑一次)
PYTHONPATH="F:/Quant/code/qlib_strategy_dev/vendor/qlib_strategy_core;F:/Quant/code/qlib_strategy_dev" \
  BUNDLE_DIR="f:/Quant/code/qlib_strategy_dev/qs_exports/rolling_exp/<run_id_1>" \
  E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe \
  f:/Quant/vnpy/vnpy_strategy_dev/vnpy_ml_strategy/test/generate_qlib_ground_truth.py \
  --strategy-name csi300_lgb_headless

PYTHONPATH=... BUNDLE_DIR="...<run_id_2>..." \
  E:/.../python.exe vnpy_ml_strategy/test/generate_qlib_ground_truth.py \
  --strategy-name csi300_lgb_headless_2

# 4. 清空 vnpy 模拟柜台 + ml_output + replay_history.db 触发干净回放
F:/Program_Home/vnpy/python.exe scripts/reset_sim_state.py --all  # 含 replay_history.db

# 5. 启动 vnpy 实盘回放 (sim mode 自动 enable_replay)
F:/Program_Home/vnpy/python.exe F:/Quant/vnpy/vnpy_strategy_dev/run_ml_headless.py
# 等回放完成 (batch 模式 ~1 min, 历史逐日模式 ~80 个交易日 × ~90s/天)

# 6. 跑 e2e 严格等价 + 权益曲线测试
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -m pytest \
  vnpy_ml_strategy/test/test_topk_e2e_d_drive.py \
  vnpy_ml_strategy/test/test_topk_e2e_equity_curve.py \
  vnpy_ml_strategy/test/test_topk_e2e_algorithm.py \
  vnpy_ml_strategy/test/test_topk_dropout_decision.py \
  -v
# 多策略切换: set E2E_STRATEGY_NAME=csi300_lgb_headless_2 + E2E_VNPY_SIM_DB=...

# 7. 出对比图 (按 strategy_name 隔离, 每策略一张)
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/plot_equity_curve_comparison.py csi300_lgb_headless
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/plot_equity_curve_comparison.py csi300_lgb_headless_2
# 浏览 vnpy_ml_strategy/test/result/equity_curve_comparison_csi300_lgb_headless.png 等

# 8. 任一测试 FAIL → 跑诊断脚本(均不需要 pytest)
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/diagnose_holdings_diverge.py
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/diagnose_weight_offset.py
```

### A1/B2 + P2-1 单测套件(快速跑, 全程 < 10s)

```bash
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -m pytest \
  vnpy_ml_strategy/test/test_replay_history.py \
  vnpy_ml_strategy/test/test_template_replay_persist.py \
  vnpy_ml_strategy/test/test_signal_source_strategy.py \
  vnpy_ml_strategy/test/test_dual_gateway_routing.py \
  vnpy_ml_strategy/test/test_dual_track_with_fake_live.py \
  -v
# 期望: 34 passed (11 + 4 + 5 + 5 + 9)
```

### A1 端到端跨进程冒烟(模拟 vnpy 节点 → mlearnweb sync_loop)

```bash
# 1. 注入 fake 数据到 replay_history.db (或先跑过真实回放)
F:/Program_Home/vnpy/python.exe -c "
from vnpy_ml_strategy.replay_history import write_snapshot
from datetime import datetime
for i in range(5):
    write_snapshot(strategy_name='csi300_test',
                   ts=datetime(2026, 4, 26+i, 15, 0),
                   strategy_value=1_000_000.0+i*10_000,
                   account_equity=1_000_000.0+i*10_000)
"

# 2. 启 fake_webtrader 模拟 vnpy 节点 8001
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/fake_webtrader_for_a1_e2e.py &

# 3. 启 mlearnweb live_main 8100, 等 5 min 让 sync_loop 拉到
cd /f/Quant/code/qlib_strategy_dev/mlearnweb/backend
E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe -m uvicorn app.live_main:app --port 8100 &

# 4. 立即触发同步
E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe -c "
import asyncio
from app.services.vnpy.replay_equity_sync_service import sync_all
print(asyncio.run(sync_all()))
# 期望: {'scanned': N, 'upserted': 5, 'ok': True}
"
```

***

## 已知 issue / 历史教训

- **春节假期未识别 → 5 天无效 rebalance**: `docs/known_issues/holdings_diverge_after_2026_02_13.md` — 修复要点是 replay 启动调 `ensure_trade_calendar(provider_uri)`
- **不要把 e2e 验证用的** **`deal_price="$open"`** **改到训练代码**: 训练 / production code 必须保持 `deal_price="close"` 默认,e2e 改动只在 `generate_qlib_ground_truth.py` 内,原因是之前大量训练都是用的close
- **不要拿 mlflow 历史 artifacts 当 ground truth**: 跨系统验证两端必须都用 `D:/vnpy_data/qlib_data_bin` 重新驱动,确保数据同源
- **ground truth 必须按 strategy_name 隔离子目录** (2026-05 修): 之前 `generate_qlib_ground_truth.py` + `plot_equity_curve_comparison.py` 6 个脚本写死同一份 `qlib_d_backtest/report_normal_1day.pkl`,导致两个不同 bundle 的策略对比图共享同一份 qlib 曲线,**虚假等价**. 现按 `--strategy-name` / env `E2E_STRATEGY_NAME` / `output_root/{strategy}` 子目录严格隔离.
- **vnpy 主进程不应直接写 `mlearnweb.db`** (A1/B2): 跨工程紧耦合 + 跨机部署阻塞. 改成 vnpy 写本地 `replay_history.db` + `vnpy_webtrader` endpoint 暴露 + mlearnweb `replay_equity_sync_service` 5min 周期拉. 详见 [`docs/deployment_a1_p21_plan.md`](../../docs/deployment_a1_p21_plan.md) §一.
- **fakes/ 与生产 import 路径绑定**: `run_ml_headless.py` 的 `kind=fake_live` 分支 `from vnpy_ml_strategy.test.fakes.fake_qmt_gateway import FakeQmtGateway`. 调整 fakes/ 位置必须同步改 `_load_gateway_class` + V2 测试 import.

