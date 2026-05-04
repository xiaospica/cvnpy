# vnpy\_ml\_strategy/test/ — 测试与验证脚本统一目录

本目录承载 **vnpy\_ml\_strategy** 应用的所有测试 / 验证 / 诊断脚本。一切跨 `vnpy 实盘回放` 与 `qlib backtest` 的等价性证明都集中在此,目的是让后续 AI / 工程师能从单一入口理解端到端验证体系。

***

## 文件总览

| 类型     | 文件                                | 一句话说明                                                                                                 |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------- |
| pytest | `test_topk_e2e_d_drive.py`        | **Phase 6.4a** — vnpy 回放 vs qlib backtest 持仓 + weight 严格等价                                            |
| pytest | `test_topk_e2e_equity_curve.py`   | vnpy 回放 vs qlib backtest 权益曲线 / 日收益率严格等价                                                              |
| pytest | `test_topk_e2e_algorithm.py`      | `topk_dropout_decision()` 与 qlib `TopkDropoutStrategy` 算法层 bit-equal                                  |
| pytest | `test_topk_dropout_decision.py`   | `topk_dropout_decision()` 纯函数单元测试(6 分支)                                                               |
| pytest | `test_ml_strategy_replay.py`      | replay 控制器 + as\_of\_date 透传 + sim 守门等单测                                                              |
| 脚本     | `generate_qlib_ground_truth.py`   | **生成 qlib ground truth**(positions\_normal\_1day.pkl + report\_normal\_1day.pkl + pred.pkl)给 e2e 测试消费 |
| 脚本     | `diagnose_holdings_diverge.py`    | 持仓集合 diverge 诊断 — 逐日 dump 找 first divergence day                                                      |
| 脚本     | `diagnose_weight_offset.py`       | weight 残余偏差归因(整百取整 + 撮合价分母 vs settle 浮点累积)                                                            |
| 脚本     | `plot_equity_curve_comparison.py` | 出对比图 `vnpy_ml_strategy/test/result/equity_curve_comparison.png`                                                               |
| smoke  | `smoke_subprocess.py`             | 单进程 → QlibPredictor → subprocess 推理(最小链路)                                                             |
| smoke  | `smoke_engine_rpc.py`             | MLEngine + WebTrader RPC + 派生 webtrader uvicorn                                                       |
| smoke  | `smoke_full_pipeline.py`          | 全栈一键(含 mlearnweb live\_main 子进程 + ml\_snapshot\_loop tick)                                            |

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

**输出**: `vnpy_ml_strategy/test/result/equity_curve_comparison.png`

**运行**:

```bash
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/plot_equity_curve_comparison.py
```

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

# 3. 生成 qlib ground truth
PYTHONPATH="F:/Quant/code/qlib_strategy_dev/vendor/qlib_strategy_core;F:/Quant/code/qlib_strategy_dev" \
  E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe \
  f:/Quant/vnpy/vnpy_strategy_dev/vnpy_ml_strategy/test/generate_qlib_ground_truth.py

# 4. 清空 vnpy 模拟柜台 + ml_output 触发干净回放
rm -rf F:/Quant/vnpy/vnpy_strategy_dev/vnpy_qmt_sim/.trading_state/sim_QMT_SIM_csi300.db
rm -rf D:/ml_output/csi300_lgb_headless

# 5. 启动 vnpy 实盘回放 (sim mode 自动 enable_replay)
F:/Program_Home/vnpy/python.exe F:/Quant/vnpy/vnpy_strategy_dev/run_ml_headless.py
# 等回放完成 (~80 个交易日 × ~90s/天)

# 6. 跑 e2e 严格等价 + 权益曲线测试
cd /f/Quant/vnpy/vnpy_strategy_dev
F:/Program_Home/vnpy/python.exe -m pytest \
  vnpy_ml_strategy/test/test_topk_e2e_d_drive.py \
  vnpy_ml_strategy/test/test_topk_e2e_equity_curve.py \
  vnpy_ml_strategy/test/test_topk_e2e_algorithm.py \
  vnpy_ml_strategy/test/test_topk_dropout_decision.py \
  -v

# 7. 出对比图
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/plot_equity_curve_comparison.py
# 浏览 vnpy_ml_strategy/test/result/equity_curve_comparison.png

# 8. 任一测试 FAIL → 跑诊断脚本(均不需要 pytest)
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/diagnose_holdings_diverge.py
F:/Program_Home/vnpy/python.exe vnpy_ml_strategy/test/diagnose_weight_offset.py
```

***

## 已知 issue / 历史教训

- **春节假期未识别 → 5 天无效 rebalance**: `docs/known_issues/holdings_diverge_after_2026_02_13.md` — 修复要点是 replay 启动调 `ensure_trade_calendar(provider_uri)`
- **不要把 e2e 验证用的** **`deal_price="$open"`** **改到训练代码**: 训练 / production code 必须保持 `deal_price="close"` 默认,e2e 改动只在 `generate_qlib_ground_truth.py` 内，原因是之前大量训练都是用的close
- **不要拿 mlflow 历史 artifacts 当 ground truth**: 跨系统验证两端必须都用 `D:/vnpy_data/qlib_data_bin` 重新驱动,确保数据同源

