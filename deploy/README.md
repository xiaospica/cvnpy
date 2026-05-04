# deploy/ — 推理端部署脚本

⚠️ **本目录只装推理端服务** (vnpy_headless 一个 NSSM 服务).

监控端 (mlearnweb 双 uvicorn + 前端) 是**独立项目**, 通常部署在另一台机器,
有自己的 `mlearnweb/deploy/` 安装脚本, 不在本目录管理.

---

## 推理端 ↔ 监控端 部署架构

```
┌────────────────────────────────────┐         ┌────────────────────────────────────┐
│  推理服务器 (vnpy_strategy_dev)    │         │  监控服务器 (mlearnweb)            │
│                                    │         │                                    │
│  NSSM service: vnpy_headless       │  HTTP   │  NSSM service: mlearnweb_live      │
│  ├ vnpy 主进程 + 双 cron           │ ◄────── │  └ uvicorn :8100 (5 个 sync_loop)  │
│  ├ webtrader uvicorn :8001 (子)    │  pulls  │                                    │
│  └ 推理子进程 (spawn 短)            │         │  NSSM service: mlearnweb_research  │
│                                    │         │  └ uvicorn :8000                   │
│  本目录 deploy/ 安装这一个         │         │                                    │
└────────────────────────────────────┘         │  本目录 mlearnweb/deploy/ 安装     │
                                                └────────────────────────────────────┘
```

**推理端**: vnpy_strategy_dev — 跑策略 / 撮合 / 推理子进程, 数据写本地
`replay_history.db` + 暴露 vnpy_webtrader HTTP :8001.

**监控端**: mlearnweb — 通过 vnpy_nodes.yaml 配置的 base_url (http://推理机:8001)
fanout 拉策略状态. 不依赖 vnpy_strategy_dev 仓库, 单独项目.

**单机开发**: 推理 + 监控可同机运行 (本机走 127.0.0.1:8001), 但**安装命令各自跑**:
- 推理端: `vnpy_strategy_dev/deploy/install_services.ps1`
- 监控端: `mlearnweb/deploy/install_services.ps1`

---

## 推理端服务清单 (本目录)

| 服务名 | 进程 | 端口 | 备注 |
|---|---|---|---|
| `vnpy_headless` | run_ml_headless.py (vnpy 3.13) | 2014/4102 (RPC), 8001 (HTTP, 主进程子进程) | 唯一服务 |

**为什么只一个**: vnpy_webtrader uvicorn 8001 是 vnpy_headless **主进程内部 spawn**
的子进程 (`subprocess.Popen`), NSSM 重启 vnpy_headless 时它一并被重启,
不需要独立服务. 推理子进程同理 (短生命周期, 21:00 cron 触发, 几分钟跑完即退).

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `bootstrap.ps1` | [P2-2] 一键 IaC: 前置检查 + 数据目录 + Python 依赖 + NTP + NSSM + 备份计划 + dry-run 验证 |
| `install_services.ps1` | 一键装 vnpy_headless (NSSM) — `bootstrap.ps1 -Apply` 内部会调 |
| `uninstall_services.ps1` | 一键卸载 |
| `configure_ntp.ps1` | [P1-7] 配置 NTP 同步 (国家授时中心 / 阿里云 / Windows 默认 fallback) |
| `daily_backup.ps1` | [P1-6] 每日备份 vnpy 端关键数据 (replay_history / sim_db / vt_setting / .env / yaml + bundle 元数据) |
| `README.md` | 本文档 |

## 一键 bootstrap (P2-2 推荐)

```powershell
# 1. 前置检查 (不动状态)
.\deploy\bootstrap.ps1 -Check

# 2. 修齐前置后 (Administrator) 全量装
.\deploy\bootstrap.ps1 -Apply

# 3. 自定义跳过某些步骤
.\deploy\bootstrap.ps1 -Apply -SkipNtp -SkipBackupSchedule
```

bootstrap 把下面 5 个脚本协调跑通, 通常**不需要**单独跑各个脚本.

## 前置要求

**部署机最小前置** — 只这一项必须人工装:

1. **Python 3.13 (vnpy 主) + Python 3.11 (推理子进程)** —
   `winget install Python.Python.3.13` + `winget install Python.Python.3.11`

**bootstrap.ps1 -Apply 自动处理**:

- ✅ NSSM (winget → 官方 zip 直下载 fallback) — 关闭走 `-NoAutoInstallNssm`
- ✅ 数据目录 (`D:\vnpy_data\{snapshots,state,models,...}`)
- ✅ pip 依赖 (psutil/loguru/pyyaml/pyqlib/lightgbm 等)
- ✅ NTP 时钟同步 (国家授时中心) — 关闭走 `-SkipNtp`
- ✅ NSSM 服务 vnpy_headless 注册 — 关闭走 `-SkipServices`
- ✅ 任务计划程序 02:00 备份 — 关闭走 `-SkipBackupSchedule`
- ✅ 启动期 dry-run 验证 import / .env / yaml — 关闭走 `-SkipDryRun`

**用户在跑 -Apply 之前需要做**:

1. **以 Administrator 运行 PowerShell** (NSSM / NTP / 任务计划程序都要 admin)
2. `.env.production` 已配 (拷贝 [`.env.example`](../.env.example) 后填值)
3. `config/strategies.production.yaml` 已配 (拷贝 [`config/strategies.example.yaml`](../config/strategies.example.yaml))
4. miniqmt 客户端已装好 (仅 `kind=live` 实盘需要; 全模拟 `kind=sim` 跳过)

## 上线一次性配置 (手工部署时用; bootstrap.ps1 -Apply 自动做了这些)

```powershell
# 配置 NTP (Administrator) — 09:26 时间窗准
.\deploy\configure_ntp.ps1

# 配置每日 02:00 备份 (Administrator)
schtasks /create /tn "vnpy_daily_backup" `
    /tr "powershell -File F:\Quant\vnpy\vnpy_strategy_dev\deploy\daily_backup.ps1" `
    /sc daily /st 02:00 /ru SYSTEM
```

## 一键装

```powershell
# 默认配置
.\deploy\install_services.ps1

# 自定义路径:
.\deploy\install_services.ps1 `
    -VnpyRoot "F:\Quant\vnpy\vnpy_strategy_dev" `
    -VnpyPython "F:\Program_Home\vnpy\python.exe" `
    -LogRoot "D:\vnpy_logs"
```

## NSSM 配置详情

`vnpy_headless` 装好后的 NSSM 设置:

| 配置项 | 值 | 作用 |
|---|---|---|
| `Application` | `<VnpyPython>` | F:\Program_Home\vnpy\python.exe |
| `AppParameters` | `run_ml_headless.py` | 主入口 |
| `AppDirectory` | `<VnpyRoot>` | 工作目录 = 仓库根 (load_dotenv 找 .env 用) |
| `AppStdout` / `AppStderr` | `D:\vnpy_logs\vnpy_headless.{log,err}` | 标准输出 + 错误 |
| `AppRotateFiles=1` `AppRotateBytes=10485760` | 10 MB 文件 | 日志滚动 (P1-2 部分覆盖) |
| `AppRestartDelay=10000` | 10s | 崩溃 → 等 10s → 自动重启 (防震荡) |
| `Start=SERVICE_AUTO_START` | 开机自启 | 服务器重启自动起 |
| `AppPriority=BELOW_NORMAL_PRIORITY_CLASS` | 低优先级 | 不抢 OS / 监控端拉数据线程 (P1-5) |

## 日常运维

```powershell
# 看状态
nssm status vnpy_headless

# 重启
nssm restart vnpy_headless

# 实时看 stdout 日志
Get-Content D:\vnpy_logs\vnpy_headless.log -Wait -Tail 50

# 看 stderr (推理子进程 / qlib import 异常专属)
Get-Content D:\vnpy_logs\vnpy_headless.err -Wait -Tail 50

# 测推理端端口 (启动 ~10s 后)
Test-NetConnection 127.0.0.1 -Port 2014    # vnpy_webtrader RPC rep
Test-NetConnection 127.0.0.1 -Port 4102    # vnpy_webtrader RPC pub
Test-NetConnection 127.0.0.1 -Port 8001    # vnpy_webtrader HTTP (mlearnweb 拉数据入口)

# 跨机部署: 推理机防火墙开 8001 端口给监控机 IP
New-NetFirewallRule -DisplayName "vnpy webtrader 8001" `
    -Direction Inbound -Protocol TCP -LocalPort 8001 `
    -RemoteAddress <监控机IP> -Action Allow
```

## 卸载

```powershell
.\deploy\uninstall_services.ps1
# 服务卸载, 但保留 .env / config / 日志 / 数据 (D:\vnpy_data, sim_db, ml_output)
```

## 故障排查

### vnpy_headless 启动后立即崩溃

```powershell
Get-Content D:\vnpy_logs\vnpy_headless.err -Tail 50
```

常见原因:

| 错误 | 修复 |
|---|---|
| `RuntimeError: QS_DATA_ROOT 未设` | `.env.production` 缺字段 / 路径错 |
| `RuntimeError: DailyIngestPipeline 未启用` | `.env.production` 设 `ML_DAILY_INGEST_ENABLED=1` (P0-3) |
| `ValueError: GATEWAYS 含 N 个 kind=live` | `strategies.production.yaml` gateway 配错 — miniqmt 单进程单账户 ≤ 1 |
| `FileNotFoundError: bundle ...` | yaml 中 `bundle_dir` 路径不存在; rsync bundle 到 `${VNPY_MODEL_ROOT}/<run_id>` |
| `ImportError: vnpy_qmt` | kind=live 但没装 vnpy_qmt; 改 kind=sim (sim 模式) 或 `pip install vnpy_qmt` |
| `OS 时区 ≠ APScheduler` | P1-4 警告, 修: `tzutil /s "China Standard Time"` + `w32tm /resync` |

### 监控端 (mlearnweb) 拉不到推理端数据

```powershell
# 1. 确认 8001 在 LISTENING
Test-NetConnection 127.0.0.1 -Port 8001

# 2. 监控端 vnpy_nodes.yaml 配的 base_url 是否正确
# 在 mlearnweb 部署机:
Get-Content <mlearnweb-root>\backend\vnpy_nodes.yaml

# 3. 防火墙 (跨机部署时):
Get-NetFirewallRule -DisplayName "vnpy webtrader 8001"
```

### 服务"启动超时"

NSSM 默认 30s 内必须进入 RUNNING. vnpy 主进程加载 ~5s, 应该够. 若超时:
```powershell
nssm set vnpy_headless AppRestartDelay 60000   # 增加重启间隔到 60s
```

## 进一步阅读

- [`../vnpy_ml_strategy/docs/operations.md`](../vnpy_ml_strategy/docs/operations.md) — 运维手册 (升级/备份/告警)
- [`../vnpy_ml_strategy/docs/deployment.md`](../vnpy_ml_strategy/docs/deployment.md) — 完整部署 checklist
- mlearnweb 项目 deploy/ — 监控端独立部署脚本
