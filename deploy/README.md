# deploy/ — 部署脚本

P0-4 NSSM 服务化方案. Windows server 一键装 / 卸 4 个长跑服务.

## 文件清单

| 文件 | 作用 |
|---|---|
| `install_services.ps1` | 一键装 NSSM 服务 × 4 (vnpy_headless + mlearnweb 双 uvicorn + 可选前端) |
| `uninstall_services.ps1` | 一键卸载 |
| `README.md` | 本文档 |

## 前置要求

1. **NSSM** 已装 (https://nssm.cc/ 或 `choco install nssm`)
2. **以 Administrator 运行 PowerShell** (NSSM 需要)
3. `.env.production` 已配 (参 [`.env.example`](../.env.example))
4. `config/strategies.production.yaml` 已配 (参 [`config/strategies.example.yaml`](../config/strategies.example.yaml))
5. miniqmt 客户端已装好 (实盘必须)

## 一键装

```powershell
# 默认配置 (3 个服务: vnpy_headless + 2 个 mlearnweb)
.\deploy\install_services.ps1

# 含前端 (4 个服务):
.\deploy\install_services.ps1 -InstallFrontend

# 自定义路径:
.\deploy\install_services.ps1 `
    -VnpyRoot "F:\Quant\vnpy\vnpy_strategy_dev" `
    -VnpyPython "F:\Program_Home\vnpy\python.exe" `
    -InferencePython "E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe" `
    -MLearnwebRoot "F:\Quant\code\qlib_strategy_dev\mlearnweb" `
    -LogRoot "D:\vnpy_logs"
```

## 服务清单

| 服务名 | 进程 | 端口 | 备注 |
|---|---|---|---|
| `vnpy_headless` | run_ml_headless.py (vnpy 主) | 2014/4102 (RPC), 8001 (HTTP, spawn 子进程) | 优先级 BELOW_NORMAL |
| `mlearnweb_research` | uvicorn app.main:app | 8000 | research 侧 |
| `mlearnweb_live` | uvicorn app.live_main:app | 8100 | 实盘侧 + 5 个 sync_loop |
| `mlearnweb_frontend` | vite preview (可选) | 5173 | 默认不装 |

## NSSM 配置

每个服务都设了:
- `AppStdout` / `AppStderr` → `D:\vnpy_logs\<service>.log` / `.err`
- `AppRotateFiles=1 AppRotateBytes=10MB` → 日志滚动 (P1-2 部分覆盖)
- `AppRestartDelay=10s` → 崩溃自动重启
- `Start=SERVICE_AUTO_START` → 开机自启
- `vnpy_headless` 额外设 `AppPriority=BELOW_NORMAL_PRIORITY_CLASS` (P1-5 雏形)

## 日常运维

```powershell
# 看状态
nssm status vnpy_headless

# 重启
nssm restart vnpy_headless

# 看日志
Get-Content D:\vnpy_logs\vnpy_headless.log -Wait -Tail 50

# 看 stderr (推理子进程异常 / qlib import 失败 等)
Get-Content D:\vnpy_logs\vnpy_headless.err -Wait -Tail 50

# 测端口
Test-NetConnection 127.0.0.1 -Port 8001
Test-NetConnection 127.0.0.1 -Port 8100
```

## 卸载

```powershell
.\deploy\uninstall_services.ps1
# 服务卸载, 但保留 .env / config / 日志 / 数据
```

## 故障排查

### vnpy_headless 启动后立即崩溃

```powershell
Get-Content D:\vnpy_logs\vnpy_headless.err -Tail 50
```

常见:
- `RuntimeError: QS_DATA_ROOT 未设` → `.env.production` 缺 / `STRATEGIES_CONFIG` 路径错
- `RuntimeError: DailyIngestPipeline 未启用` → 设 `ML_DAILY_INGEST_ENABLED=1`
- `FileNotFoundError: bundle ...` → `config/strategies.production.yaml` 中 `bundle_dir` 路径不存在

### mlearnweb_live 拉不到 vnpy 数据

```powershell
# 检查 vnpy_webtrader HTTP 8001 在不在
Test-NetConnection 127.0.0.1 -Port 8001
# 检查 vnpy_nodes.yaml base_url
Get-Content F:\Quant\code\qlib_strategy_dev\mlearnweb\backend\vnpy_nodes.yaml
```

### 服务"无法启动"

NSSM 默认 30s 内必须能进入 RUNNING 状态. vnpy 主进程加载 ~5s, 应该够. 若超时:
```powershell
nssm set vnpy_headless AppRestartDelay 60000   # 增加重启间隔到 60s
```

## 进一步阅读

- [`vnpy_ml_strategy/docs/operations.md`](../vnpy_ml_strategy/docs/operations.md) — 运维手册 (升级/备份/告警)
- [`vnpy_ml_strategy/docs/deployment.md`](../vnpy_ml_strategy/docs/deployment.md) — 完整部署 checklist
