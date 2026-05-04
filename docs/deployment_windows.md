# vnpy_strategy_dev — Windows 推理服务器部署评估

> **状态**：草稿 / 待讨论。本文档只列**问题 + 备选方案**，不是已决方案。
> 评审顺序参见末尾"待决议清单"。每条决议后再切到 P0/P1/P2 实施。

---

## 0. 部署目标与背景

- **目标机型**：Windows Server 2022 / Windows 11 Pro（专门跑实盘 + 模拟推理）
- **部署组件**：
  1. `run_ml_headless.py`（vnpy 主进程：MainEngine + EventEngine + 策略 cron）
  2. `vnpy_webtrader` HTTP RPC（uvicorn :8001 — mlearnweb 通过此端拉策略状态）
  3. **可选**：mlearnweb backend（research :8000 + live :8100） + frontend Vite (:5173)
- **数据流**：
  ```
  tushare ─→ DailyIngestPipeline (20:00 cron) ─→ qlib bin + filter snapshot
                                                            ↓
  21:00 cron ─→ run_inference subprocess (qlib + lgb) ─→ predictions.parquet
                                                            ↓
  09:26 cron ─→ rebalance + send_order ─→ QmtGateway / QmtSimGateway
                                                            ↓
                                                         broker / sim
  ```

---

## 1. 架构层面的部署阻碍（讨论前必须澄清的）

### A1. `vnpy_strategy_dev` 直接写 `mlearnweb.db` — **跨工程紧耦合**

**事实**：
- [vnpy_ml_strategy/mlearnweb_writer.py](../vnpy_ml_strategy/mlearnweb_writer.py) 在 vnpy 主进程里 `sqlite3.connect("F:/Quant/code/qlib_strategy_dev/mlearnweb/backend/mlearnweb.db")` 直接 INSERT 三张表（`strategy_equity_snapshots` / `ml_metric_snapshots` / `ml_prediction_daily`）。
- 默认路径写死 `F:/Quant/code/qlib_strategy_dev/...`，可由 env `MLEARNWEB_DB` 覆盖。
- 注释里明承认"表 schema 由 mlearnweb 端定义，如该表 DDL 漂移本模块需同步"。
- 调用点：[template.py:1417](../vnpy_ml_strategy/template.py#L1417) 回放期间每日 settle 后写权益、[template.py:1600](../vnpy_ml_strategy/template.py#L1600) 写 metric/prediction。

**为什么是问题**：
1. **跨机部署不可行**：推理服务器和 mlearnweb 服务器分开后，vnpy 看不到 mlearnweb 的本地文件。
2. **跨工程 schema 漂移风险**：mlearnweb backend 改了 `models/database.py` 的 `TrainingRecord` / `strategy_equity_snapshots` 字段，vnpy 端 INSERT 语法立刻 break — 但跨工程没有任何 contract test。
3. **违反单一职责**：vnpy 的写应该在自己的进程边界结束（写到本地状态、发事件），存档由订阅者负责。
4. **同机也有锁竞争**：mlearnweb 两个 uvicorn + vnpy 都连同一 db；WAL 模式抵消大部分压力但单文件 lock 在重启场景仍可能撞上（已观察到的现象之一）。

**为什么以前选这条路**（推断）：
- 回放产生几十/几百行 snapshot，逐行走 HTTP 来回成本高
- mlearnweb 写端口又有 ops 口令护守，自动化不便
- 同机开发期间文件路径直读最快上线

**备选方案**：

| 方案 | 改动量 | 跨机可行 | 长期合理度 | 主要风险 |
|---|---|---|---|---|
| **B1** 维持现状，仅把 `MLEARNWEB_DB` 强制 env 化 + 启动期断言可达 | XS | ❌ 仍同机 | 低 | 临时缓解，本质未解 |
| **B2** vnpy 端**只写本地 SQLite**（`D:\vnpy_data\replay_history.db`），mlearnweb 改成"按需 fanout 拉" via `vnpy_webtrader` 新 endpoint `/api/v1/replay/equity_snapshots` | M | ✅ | 高 | 需在 vnpy_webtrader 加 endpoint + mlearnweb client 加 fetcher；首次拉延迟 |
| **B3** vnpy 通过 mlearnweb 的 HTTP 写端点 POST（绕过 ops 口令的"机器对机器"专用 token） | M-L | ✅ | 中 | mlearnweb 要加批量写 endpoint + token 鉴权设计；网络故障重试 |
| **B4** 改用 PostgreSQL（mlearnweb + vnpy 共用 DB，权限分隔）| L | ✅ | 高 | 引入新依赖；Windows 上跑 PG 要装服务；备份恢复换栈 |

**讨论焦点**：
- 同机部署是否长期方案？若是，B1 够用；若打算多机，必须 B2/B3。
- 倾向 **B2**：契合"vnpy 是实盘节点 + mlearnweb 是观察者"的定位，与现有 fanout 架构一致（mlearnweb 已经按 vnpy_nodes.yaml 拉每节点的实时 strategy / position）。

> ⚠️ **本条决议直接影响 P0 多个其他项**：B1 → P0 路径外置就够；B2/B3 → 加新 API、改 mlearnweb 数据接入。先决此条。

### A2. `vnpy_qmt_sim` 状态 SQLite 与 mlearnweb 写入路径解耦得不彻底

[vnpy_qmt_sim/persistence.py](../vnpy_qmt_sim/persistence.py) 写 `sim_<gateway>.db`，文档明说"避免与 mlearnweb.db 互锁"。这条目前 OK — 文件分开。但 A1 解开后，整套 vnpy 状态文件都该统一到 `D:\vnpy_data\state\`：
- `D:\vnpy_data\state\sim_QMT_SIM_csi300.db`
- `D:\vnpy_data\state\replay_history.db`（B2 方案下新增）
- `D:\vnpy_data\state\strategy_inflight.db`（如未来需要订单 inflight 持久化）

避免散落在 `vnpy_qmt_sim/.trading_state/` / `mlearnweb/backend/` 各处。

---

## 2. P0 · 上线前必修

> 仅当 A1 决议为"维持同机部署 + B1"时才把 `mlearnweb.db` 路径列入 P0；否则 P0 由 B2/B3 落地决定。

### P0-1. 凭证与配置的明文外散

**事实**：
- [.vntrader/vt_setting.json](../.vntrader/vt_setting.json) 里 `datafeed.password` = tushare token 明文。
- `.vntrader` 已在 `.gitignore`，但部署机第一次启动需要这份文件 — 没有"复刻 / 注入"流程。
- [run_ml_headless.py:79](../run_ml_headless.py#L79) miniqmt `客户端路径` 硬编码 `E:\迅投极速交易终端 睿智融科版\userdata_mini`，资金账号留空。

**备选方案**：
- 最小：`C:\ProgramData\vnpy_secrets\vt_setting.json` 由 ACL 限定到运行账户，启动脚本 `os.environ["VNPY_SETTING"]` 指向。
- 更完整：用 Windows DPAPI（`win32crypt.CryptProtectData`）加密敏感字段；boot 时解密；仓库提供 `deploy/encrypt_secrets.ps1`。
- 最完整：HashiCorp Vault / Azure Key Vault — 适合多机 / 受审计场景。

### P0-2. 路径硬编码

[run_ml_headless.py](../run_ml_headless.py) 处处绝对路径：

| 行 | 路径 | 含义 |
|---|---|---|
| L45 | `F:\Quant\code\qlib_strategy_dev` | qlib 源码 |
| L64 | `D:\vnpy_data\snapshots\merged` | bar 行情快照 |
| L70 | `F:\Quant\vnpy\vnpy_strategy_dev\vnpy_qmt_sim\.trading_state` | 模拟柜台状态 |
| L108 | `D:/vnpy_data` | QS_DATA_ROOT |
| L115 | `E:/ssd_backup/Pycharm_project/python-3.11.0-amd64/python.exe` | 推理子进程 Python |
| L145/L161 | `F:/Quant/code/qlib_strategy_dev/qs_exports/...` | bundle_dir 写死 |

**备选方案**：
- `deploy/.env.production` + [`python-dotenv`](https://pypi.org/project/python-dotenv/)，所有路径走 `os.getenv` 缺即 raise。
- `STRATEGIES` 数组从 JSON 文件读：`D:\vnpy_data\config\strategies.json`，部署机自己维护。

### P0-3. ML_DAILY_INGEST_ENABLED 默认关闭 → 上线即 raise

[tushare_datafeed.py:206](../vnpy_tushare_pro/tushare_datafeed.py#L206) `ML_DAILY_INGEST_ENABLED` 默认 `"0"` → 不构 pipeline → run_ml_headless 启动期 `set_filter_chain_specs` 跳过；21:00 推理走 strict raise。

**备选方案**：
- 服务器一次性 `setx ML_DAILY_INGEST_ENABLED 1 /M`。
- run_ml_headless 启动期检测 `ts_pipeline is None` → 直接 abort（当前是 print warn）。

### P0-4. 服务化（开机自启 + 进程崩溃自恢复）

**事实**：仓库无 NSSM / WinSW / 任务计划程序模板。一次远程登出整套 stop。

**备选方案**：

| 工具 | 优点 | 缺点 |
|---|---|---|
| **NSSM** | 一行命令装；自动重启；console log 重定向；20 年成熟 | 配置散在 nssm CLI / 注册表 |
| **WinSW** | XML 配置可版本化；与 winget 配套好 | 文档相对少 |
| **任务计划程序** | 系统原生 | 无原生 watchdog；进程死了等下次 boot |
| **Windows Service via pywin32** | Python 原生 | 实现成本高，维护负担 |

倾向 **NSSM** + 一份 `deploy/install_services.ps1` 一键装：
- `vnpy_headless`（run_ml_headless.py）
- `vnpy_webtrader_http`（如未由 vnpy_headless 派生）
- `mlearnweb_research` / `mlearnweb_live`（若同机部署）

### P0-5. SQLite 锁文件的崩溃恢复

`sim_<gateway>.db` 用 `msvcrt.locking` 防同 account_id 多进程；进程崩溃→ OS 释放 OK。但若用 `flock` 自实现（plan 文档曾提）需检查 lockfile 残留处理。

**备选方案**：lockfile 改 PID-stamped + 启动期 `psutil.pid_exists(old_pid)` 决定是否覆盖。

---

## 3. P1 · 影响稳态运行

### P1-1. 多策略推理时间必须错峰（用户提出）

**事实**：当前 [run_ml_headless.py STRATEGY_BASE_SETTING](../run_ml_headless.py#L112) 里 `trigger_time = "21:00"` 是**全局**默认，多策略指向同一时刻。

**风险**：
- 单策略推理峰值内存 4-5 GB（qlib + lightgbm 加载 csi300 60 交易日 alpha158 全量特征）。
- 三策略并发 = 12-15 GB → 16 GB 机器直接 swap，48 GB 机器也压力大。
- CPU 用满 → 全策略一起拖慢。
- 推理进程互不感知，没有排队 / 限流。

**备选方案**：

| 方案 | 改动量 | 评论 |
|---|---|---|
| **C1** 在 `STRATEGIES` 每条策略的 `setting_override` 里**显式**给 `trigger_time` 错峰（例：21:00 / 21:10 / 21:20） | XS | 最直观；约定行内注释告警 |
| **C2** `vnpy_common/scheduler.py` 加全局信号量 / 队列：同一时刻最多 N 个推理 job 跑 | S | 自动化；但用户不可见 |
| **C3** APScheduler `BackgroundScheduler` 改 `max_workers=1`（已经是？需确认） | XS | 如果是单 worker 已经是序列化执行 |
| **C4** `run_ml_headless.py` 启动期遍历 STRATEGIES 看到同 trigger_time → 自动加 1 分钟错位 | S | 用户可能想要并发，破坏意图 |

**倾向 C1 + 强约定**：在 `run_ml_headless.py` 的 `STRATEGIES` 注释块加一段强约定，并在 `_validate_startup_config` 里 raise 同时刻冲突。

```python
# ⚠️ 多策略 trigger_time 必须错开（推荐间隔 ≥10 分钟）
# 单策略推理峰值 4-5 GB；同 trigger_time 多策略并发会触发 swap / OOM。
# 启动期校验若发现两个策略 trigger_time 冲突会直接 raise。
```

代码补一个校验函数：
```python
def _validate_trigger_time_unique() -> None:
    seen = {}
    for s in STRATEGIES:
        t = s["setting_override"].get("trigger_time") or STRATEGY_BASE_SETTING["trigger_time"]
        if t in seen:
            raise ValueError(
                f"策略 {s['strategy_name']!r} 与 {seen[t]!r} trigger_time={t!r} 冲突；"
                "推理峰值 4-5GB，必须错峰 ≥10 min。"
            )
        seen[t] = s["strategy_name"]
```

### P1-2. 日志滚动 + 集中

vnpy / loguru / mlearnweb 各一份，无 rotation。
- 解决：loguru 全局 `rotation="100 MB", retention="14 days", compression="zip"`；NSSM stdout/stderr → `D:\vnpy_logs\<service>\`。

### P1-3. 监控告警空白

20:00 ingest 失败 / 21:00 推理 raise / send_order 拒单全静默。
- 已有 `EVENT_DAILY_INGEST_FAILED` / `EVENT_ML_METRICS` 但无出口。
- 解决：mlearnweb 加 `/api/health`（检查当日 ingest status + selections.parquet 当日产出）+ 接入 Healthchecks.io / Uptime Kuma 心跳。

### P1-4. 时区 / cron 准点性

APScheduler 默认用 OS local time。Windows Server 默认 UTC → cron `21:00` 实际是北京 5:00 → 错档完全失效。
- 解决：scheduler 强制 `timezone="Asia/Shanghai"`；启动期断言 `tzlocal.get_localzone()` 一致。

### P1-5. 推理子进程 OOM / 超时

[qlib_predictor.py](../vnpy_ml_strategy/predictors/qlib_predictor.py) 用 `subprocess.run(timeout=180)`。
- csi300 OK；zz500/all_market 可能超时。
- 没有内存监控 / 限制。
- 解决：换 `Popen` + `psutil.Process.memory_info()` 监控；超阈值 terminate + 警告事件；timeout 改成 setting 字段。

### P1-6. 备份与恢复

| 数据 | 当前 | 风险 |
|---|---|---|
| `D:\vnpy_data\models\bundle_dir\` | 训练机 rsync 没固化 | 一次 rsync 失败可能跑老 bundle |
| `mlearnweb.db` | 无备份 | 损坏即丢失部署元数据 + 历史 snapshot |
| `sim_<gateway>.db` | 无备份 | 重启从零，权益曲线断档 |
| `.vntrader/database.db` | 无备份 | vnpy bar 数据库 |

- 解决：`deploy/daily_backup.ps1` 任务计划程序 02:00 触发，整套 7zip 到 NAS / S3，retention 30 天。

### P1-7. NTP 时钟同步

A 股 09:26 调仓有严格时间窗。Windows server 默认 NTP 在某些网段不可靠。
- 解决：`w32tm /config /manualpeerlist:"ntp.ntsc.ac.cn,0x9" /syncfromflags:manual /update`；`w32tm /query /status` 偏差 >1s 告警。

---

## 4. P2 · 锦上添花

### P2-1. 实盘 / 模拟双轨并行（用户提议提优先级）

**目标**：同一台机器、同一份代码、同一份信号产出，**同时**跑：
- 实盘策略（gateway = "QMT" → miniqmt → 真券商账户）
- 影子模拟策略（gateway = "QMT_SIM_*" → vnpy_qmt_sim 撮合）

把两条权益曲线并排比较，验证模拟柜台的真实度 + 给实盘做 A/B 信号验证。

**既有基础**（已支持的部分）：
- [run_ml_headless.py](../run_ml_headless.py) `GATEWAYS` 是 list、`STRATEGIES[*].gateway_name` 独立 → 架构上**已经支持**实盘 + 模拟混部。
- vnpy 主进程的 MainEngine 可同时挂多个 Gateway 实例。
- mlearnweb plan Phase 3 已规划 mode badge（实盘 / 模拟）区分。

**当前缺口**：
1. [run_ml_headless.py:51](../run_ml_headless.py#L51) `USE_GATEWAY_KIND = "QMT_SIM"` 是**单选**变量，分支决定 gateway 类（导入 QmtGateway 或 QmtSimGateway）。混部需要去掉这个开关，让 GATEWAYS 每条自己声明类型。
2. [run_ml_headless.py:230-237](../run_ml_headless.py#L230) `for gw in GATEWAYS: main_engine.add_gateway(_GatewayClass, ...)` 复用同一个 class，混部需按 `gw["kind"]` 各自挑类。
3. miniqmt **单进程单账户**约束：实盘 GATEWAYS 列表中 `QMT` 只能 1 条。混部场景下 `QMT` 1 条 + `QMT_SIM_*` 任意条。
4. `_validate_startup_config` 现在按 `expected_class = "sim" if USE_GATEWAY_KIND == "QMT_SIM" else "live"` 强约束所有 gateway 同类 → 必须放宽。

**方案 D · 双轨混部架构**：

```python
# run_ml_headless.py 新结构
GATEWAYS = [
    {"name": "QMT",                "kind": "live", "setting": QMT_SETTING},          # 实盘 (≤1 条)
    {"name": "QMT_SIM_csi300_live_shadow", "kind": "sim",  "setting": SIM_SETTING},  # 与实盘同信号的影子
    {"name": "QMT_SIM_csi300_paper",       "kind": "sim",  "setting": SIM_SETTING},  # 独立纸面策略
]
STRATEGIES = [
    {"strategy_name": "csi300_lgb_live",        "gateway_name": "QMT",                          ...},
    {"strategy_name": "csi300_lgb_live_shadow", "gateway_name": "QMT_SIM_csi300_live_shadow",   ...},  # 同 bundle 同信号 → 影子
    {"strategy_name": "csi300_lgb_paper",       "gateway_name": "QMT_SIM_csi300_paper",         ...},  # 独立 bundle
]
```

**关键改动**：
- 删 `USE_GATEWAY_KIND`，每个 GATEWAYS 元素自带 `kind`。
- `add_gateway` 循环按 `kind` 选类（`from vnpy_qmt import QmtGateway` / `from vnpy_qmt_sim import QmtSimGateway`）。
- `_validate_startup_config` 改为：每条 gateway 用自己的 `expected_class = gw["kind"]` 校验。
- 影子策略的 bundle_dir 与实盘策略**指向同一个 bundle**，但 `strategy_name` 不同 → mlearnweb 前端各自一条曲线。
- **风险点**：实盘 + 影子的 trigger_time 错峰（推理只跑 1 次没问题），但 09:26 rebalance 的 `send_order` 各走各的 gateway，不会冲突。
- **数据隔离**：mlearnweb 已天然按 `strategy_name` 分曲线，无需额外改动；vnpy_qmt_sim 按 gateway_name 隔 sim_<gateway>.db，与实盘账户完全独立。

**双轨同步 vs 异步**（新决策点）：
- **同步**：影子策略**复用**实盘策略的 prediction（同一份 selections.parquet），保证信号 100% 等价 → 唯一差异 = 撮合差异。这才是"模拟柜台真实度评估"的正确口径。
- **异步**：影子独立跑推理（同 bundle、同 lookback、同 live_end，理论上 deterministic 一致，但磁盘 IO + 执行时序可能引入微小差异）→ 浪费 5GB 内存 +  90s。

**倾向同步**：加一个 strategy 参数 `signal_source_strategy: Optional[str]`，若设置则跳过本策略推理，直接 symlink / hardcopy 上游策略的 selections.parquet 到本策略 output_root。改动量 S。

**实施次序建议**：
1. 解开 USE_GATEWAY_KIND（M, 半天）
2. 双轨混部跑通模拟（XS 验证测试）
3. signal_source_strategy 共享信号机制（S, 一天）
4. mlearnweb 前端 mode badge（plan Phase 3 已设计，跟上即可）

### P2-2. 容器化 / IaC

仓库加 `deploy/bootstrap.ps1` 一键复刻服务器：装 Python 3.11 + ta-lib wheel + miniqmt + 4 个 service + 1 次 ingest 验证。

### P2-3. webtrader HTTP 端口冲突 / 鉴权

`WEBTRADER_HTTP_PORT = 8001` 写死；多 headless 撞端口；防火墙策略未文档化。
- 解决：端口 setting 化；Windows Defender Firewall 限制 8001 仅 mlearnweb IP。

---

## 5. docs/ 整改建议（用户提出）

**当前 [docs/](.) 内容**：
- `frontend_requirements.md`（11 KB）/ `api.md`（11 KB）→ 与 `vnpy_webtrader/docs/` 重复或更陈旧
- `a_share_sim_logic.md` / `ui_pyqtads_*.md` → vnpy_strategy_dev 自身文档，保留
- `community/` `elite/` `_static/` `_templates/` `index.rst` → vnpy 框架原始 sphinx 文档

**整改方案**：

| 文件 | 处置 |
|---|---|
| `frontend_requirements.md` | 移除 → 改在 `docs/README.md` 写一行 "前端需求详见 [vnpy_webtrader/docs/frontend_requirements.md](../vnpy_webtrader/docs/frontend_requirements.md)" |
| `api.md` | 同上，指向 `vnpy_webtrader/docs/api.md` |
| `a_share_sim_logic.md` | 保留，是 vnpy_qmt_sim 的核心设计文档 |
| `ui_pyqtads_*.md` | 保留，UI 历史决策 |
| `index.rst` + sphinx 框架 | vnpy 上游遗留，部署相关无关，保留不动 |
| **新增** `docs/README.md` | 顶层索引 + 链接到子模块文档 |
| **新增** `docs/deployment_windows.md`（本文档）| 部署评审清单 |

新增 `docs/README.md` 模板：

```markdown
# vnpy_strategy_dev 文档索引

## 部署
- [Windows 推理服务器部署](deployment_windows.md) — 上线前评审清单

## 模拟柜台
- [A 股模拟撮合规则](a_share_sim_logic.md) — vnpy_qmt_sim 设计

## UI / 历史决策
- [PyQtAds 评估](ui_pyqtads_assessment.md)
- [PyQtAds 迁移](ui_pyqtads_migration.md)

## 子模块文档（详细 API / 前端需求）
- vnpy_webtrader：[docs/](../vnpy_webtrader/docs/)
- vnpy_ml_strategy：[test/README.md](../vnpy_ml_strategy/test/README.md)
- ml_data_build：[docs/](../vnpy_tushare_pro/ml_data_build/docs/)
```

---

## 6. 待决议清单

按用户讨论顺序勾选 → 决议后切到实施 PR。

- [ ] **A1** mlearnweb.db 跨工程耦合 → 选 B1 / B2 / B3 / B4？
- [ ] **A2** 状态文件统一到 `D:\vnpy_data\state\`？
- [ ] **P0-1** 凭证外置走哪条？(env / DPAPI / Vault)
- [ ] **P0-2** 路径外置走 dotenv 还是机器 env？`STRATEGIES` JSON 化？
- [ ] **P0-3** `ML_DAILY_INGEST_ENABLED` 是否改默认 `"1"`？
- [ ] **P0-4** NSSM / WinSW / 任务计划程序选哪个？
- [ ] **P0-5** lockfile 健壮性是否做？
- [ ] **P1-1** 多策略错峰 → C1 + 校验函数？
- [ ] **P1-2/3/4/5/6/7** 各项做不做？
- [ ] **P2-1** 实盘 / 模拟双轨 → 方案 D 同步信号 vs 异步？
- [ ] **docs/** 整改 → 删 `frontend_requirements.md` + `api.md`？

---

## 附录 · 当前部署相关文件清单

| 文件 | 角色 |
|---|---|
| [run_ml_headless.py](../run_ml_headless.py) | 主入口（headless） |
| [run_sim.py](../run_sim.py) | UI 启动（开发用，部署不用） |
| [install.bat](../install.bat) | 框架依赖安装 |
| [.vntrader/vt_setting.json](../.vntrader/vt_setting.json) | vnpy 全局配置 + tushare token |
| [vnpy_tushare_pro/tushare_datafeed.py](../vnpy_tushare_pro/tushare_datafeed.py) | 数据源 + DailyIngestPipeline |
| [vnpy_ml_strategy/mlearnweb_writer.py](../vnpy_ml_strategy/mlearnweb_writer.py) | ⚠️ 跨工程 SQLite 写 — 见 A1 |
| [vnpy_qmt_sim/persistence.py](../vnpy_qmt_sim/persistence.py) | 模拟柜台状态 |
| [vnpy_common/scheduler.py](../vnpy_common/scheduler.py) | APScheduler 包装 |
