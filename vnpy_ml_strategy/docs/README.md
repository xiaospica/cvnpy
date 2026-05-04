# vnpy_ml_strategy 工程文档

vnpy_ml_strategy 是 vnpy 框架下的 **ML 策略引擎**, 与 qlib 滚动训练管道协作:
研究侧用 qlib 训练 → 导出 bundle → 实盘侧 vnpy 加载 bundle → 每日定时推理 +
A 股 T+1 撮合下单. 同时支持 **回放模式** (跑过去 N 天历史) 与 **实盘 / 模拟
双轨架构** (同进程混部, 信号同步, 撮合对照).

---

## 🗺️ 顶层全景图

```mermaid
flowchart LR
    classDef research fill:#8B5CF6,stroke:#5B21B6,color:#fff
    classDef vnpy fill:#3B82F6,stroke:#1E40AF,color:#fff
    classDef monitor fill:#10B981,stroke:#065F46,color:#fff
    classDef external fill:#F59E0B,stroke:#92400E,color:#fff

    subgraph 研究侧 [研究机]
        Train["qlib 滚动训练<br/>(rolling_train.py)"]:::research
        Bundle[("bundle/<br/>params.pkl + filter_config")]:::research
        Train --> Bundle
    end

    subgraph 数据 [数据源 (D:/vnpy_data/)]
        TS["tushare daily"]:::external
        QlibBin[("qlib_data_bin")]:::external
        Filter[("snapshots/filtered/")]:::external
        TS --> QlibBin
        TS --> Filter
    end

    subgraph 实盘 [实盘机 vnpy 主进程]
        Engine["MLEngine<br/>21:00 推理 + 09:26 rebalance"]:::vnpy
        Strats["策略 × N<br/>(实盘/模拟/影子)"]:::vnpy
        Gateways["Gateway × N<br/>QmtGateway / QmtSimGateway"]:::vnpy
        Engine --> Strats
        Strats --> Gateways
    end

    subgraph 监控 [mlearnweb (双 uvicorn + 前端)]
        BE["后端 8000/8100<br/>5 个 sync_loop"]:::monitor
        DB[("mlearnweb.db")]:::monitor
        FE["前端 :5173<br/>权益曲线/持仓/TopK"]:::monitor
        BE --> DB
        DB --> FE
    end

    Bundle -.->|"rsync"| 实盘
    QlibBin --> Engine
    Filter --> Engine
    Gateways -->|"vnpy_webtrader<br/>HTTP :8001"| BE

    Style 研究侧 fill:#F3E8FF
    Style 数据 fill:#FEF3C7
    Style 实盘 fill:#DBEAFE
    Style 监控 fill:#D1FAE5
```

> 详细架构: [architecture.md](architecture.md) · 双轨架构: [dual_track.md](dual_track.md) ⭐

---

## 📚 文档索引

按角色读最相关的:

| 角色 | 推荐文档 | 内容 |
|---|---|---|
| **策略研究员 / 学习者** | [architecture.md](architecture.md) → [dual_track.md](dual_track.md) | 整体架构, 双轨原理 |
| **运维工程师 / 部署者** | [deployment.md](deployment.md) → [operations.md](operations.md) | Windows server 部署, 故障排查, 日志, 监控 |
| **开发者 / 扩展者** | [developer.md](developer.md) → [../test/README.md](../test/README.md) | 自定义策略, Gateway 扩展, 测试体系 |
| **跨工程对接 (mlearnweb 前端)** | [architecture.md §数据流](architecture.md) | vnpy_webtrader endpoint, mlearnweb sync 链路 |

---

## 🧩 核心组件总览

```
vnpy_ml_strategy/
├── engine.py              MLEngine — vnpy 主进程引擎: 注册 cron / 调度推理 / 分发回报
├── template.py            MLStrategyTemplate — 策略模板基类: run_daily_pipeline /
│                          run_open_rebalance / 回放 / 信号同步
├── strategies/
│   └── qlib_ml_strategy.py  QlibMLStrategy — 默认实现, 复用 qlib TopkDropoutStrategy
├── predictors/
│   ├── qlib_predictor.py    spawn 子进程跑 qlib + lightgbm 推理 (隔离重型依赖)
│   └── model_registry.py    bundle 元数据校验 + filter_config.json 跨端契约
├── replay_history.py      P2-A1: 本地 SQLite 写回放权益 (替代直写 mlearnweb.db)
├── monitoring/
│   ├── cache.py             MetricsCache — 内存最近 N 日 ring buffer
│   └── publisher.py         原子写 latest.json + EVENT_ML_METRICS 事件
├── topk_dropout_decision.py  qlib TopkDropoutStrategy 算法纯函数版 (不依赖 qlib)
├── services/
│   └── ic_backfill.py       IC 回填服务 (历史 forward-window 满足后异步补 IC)
├── docs/                  ← 本目录
└── test/                  pytest + smoke + e2e 验证 (含 fakes/ 替身)
```

---

## ⚡ 5 分钟快速入门

**前置**: D:/vnpy_data/qlib_data_bin (vnpy 端日更管道产出) + 已训练好的 bundle.

```bash
# 1. 配置 .vntrader/vt_setting.json (datafeed.password = tushare token)
# 2. 改 run_ml_headless.py 中 STRATEGIES 的 bundle_dir 指向你的 bundle
# 3. 启动
F:/Program_Home/vnpy/python.exe F:/Quant/vnpy/vnpy_strategy_dev/run_ml_headless.py
```

启动后:
- 策略 `on_init` 校验 bundle (manifest + filter_config) → 注册 21:00 + 09:26 双 cron
- 若 sim gateway 且 `enable_replay=True` → 后台线程跑历史回放 (batch 模式 ~1 min)
- 21:00 cron 每日触发推理 → 写 `selections.parquet`
- 09:26 cron 每日开盘前触发 rebalance → 09:30 撮合
- vnpy_webtrader HTTP 8001 暴露策略状态 / 权益 / 持仓 / 监控指标
- mlearnweb (启 `start_mlearnweb.bat`) 通过 8001 fanout 拉数据, 前端 5173 展示

**双轨示例脚本** (一键 V1/V2/V3 演示): 见 [`run_dual_track_demo.py`](../../run_dual_track_demo.py),
详见 [dual_track.md](dual_track.md) §使用指导.

---

## 🔗 跨工程依赖

| 工程 | 角色 | 链接 |
|---|---|---|
| `qlib_strategy_dev` | 训练侧, 产出 bundle (含 filter_config.json) | [`tushare_hs300_rolling_train.py`](../../../code/qlib_strategy_dev/strategy_dev/tushare_hs300_rolling_train.py) |
| `qlib_strategy_dev/vendor/qlib_strategy_core` | 推理 SDK (predict_from_recorder / predict_from_bundle) | submodule |
| `mlearnweb` | 监控前端 + 训练记录 + 实盘观察者 | [`mlearnweb/`](../../../code/qlib_strategy_dev/mlearnweb) |
| `vnpy_qmt` | 实盘 miniqmt 网关 | 第三方 |
| `vnpy_qmt_sim` | 模拟柜台 (本仓库) | [`../../vnpy_qmt_sim/`](../../vnpy_qmt_sim/) |
| `vnpy_webtrader` | HTTP RPC 节点 (mlearnweb 拉数据入口) | [`../../vnpy_webtrader/`](../../vnpy_webtrader/) |

---

## 📋 关键约定

### Python 解释器

| 用途 | 解释器 |
|---|---|
| vnpy 主进程 (`run_ml_headless.py`, 策略, 撮合) | `F:/Program_Home/vnpy/python.exe` |
| qlib 推理子进程 | `E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe` |
| mlearnweb backend (uvicorn 8000 / 8100) | 同 qlib 推理 |

vnpy 主 python 不装 qlib / lightgbm / mlflow (太重), 推理走 subprocess 隔离.

### 关键路径 (env 可配)

| Env | 默认 | 用途 |
|---|---|---|
| `QS_DATA_ROOT` | `D:/vnpy_data` | 数据根: qlib_bin / snapshots / state |
| `ML_OUTPUT_ROOT` | `D:/ml_output` | 策略每日产物: `{strategy}/{yyyymmdd}/selections.parquet` |
| `VNPY_MODEL_ROOT` | `D:/vnpy_data/models` | bundle 部署目录 (训练机 rsync 到这里) |
| `INFERENCE_PYTHON` | 上面的 3.11 路径 | 推理 subprocess 入口 |
| `REPLAY_HISTORY_DB` | `${QS_DATA_ROOT}/state/replay_history.db` | 回放权益本地 SQLite |
| `ML_DAILY_INGEST_ENABLED` | `0` | 设 `1` 启用 vnpy_tushare_pro 20:00 cron 拉数据 |

### filter 跨端契约 (Phase 2)

训练侧把 filter_chain 写入 bundle/filter_config.json → 实盘 ModelRegistry 强校验 →
DailyIngestPipeline 按 filter_id 产 `snapshots/filtered/{filter_id}_{T}.parquet` →
推理子进程通过 `--filter-parquet` 消费. 详见
[`strategy_dev/config.py UNIVERSE_REGISTRY`](../../../code/qlib_strategy_dev/strategy_dev/config.py).

### 双 cron 架构 (实盘 best practice)

- **21:00 trigger_time**: 推理 + persist `selections.parquet` (信号已落盘)
- **09:26 buy_sell_time**: 读上一交易日 selections + 当前开盘价 → rebalance + send_order

把推理与下单解耦 → 09:26 用真实开盘价校准 volume, 撮合精度高, 与 batch replay 语义一致.
详见 [strategy_lifecycle](architecture.md §策略生命周期).

---

## 🔬 测试体系

详见 [`../test/README.md`](../test/README.md). 关键测试套:

| 套件 | 用例数 | 目的 |
|---|---|---|
| Phase 6 e2e (test_topk_e2e_*) | ~10 | vnpy 回放 vs qlib backtest 持仓/权重/曲线严格等价 |
| A1 (test_replay_history + test_template_replay_persist) | 15 | 本地 SQLite + 模板写入闭环 |
| **P2-1 双轨** (test_signal_source + test_dual_gateway + test_dual_track) | 19 | V1 + V2 双轨架构 |

---

## 📜 版本历史与设计决策

详见 [docs/deployment_a1_p21_plan.md](../../docs/deployment_a1_p21_plan.md) (跨阶段实施计划).

主要设计决策:
- **Phase 4** 回放模式 (batch 推理 + 逐日 settle) 加速 ~10x
- **Phase 6** 算法层 bit-equal: `topk_dropout_decision()` 与 qlib 原版严格一致
- **Phase 2** filter_chain 跨端契约 (训练 → 实盘)
- **A1/B2** vnpy ↔ mlearnweb.db 解耦 (跨机部署可行)
- **P2-1** 实盘 / 模拟双轨架构 (信号同步, 撮合对照)
