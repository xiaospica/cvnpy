#requires -RunAsAdministrator
<#
.SYNOPSIS
    P0-4 — 推理端 NSSM 服务化 (仅 vnpy_headless 一个服务).

.DESCRIPTION
    本脚本只装**推理端服务**. mlearnweb (监控前端) 是独立项目, 通常部署在
    另一台机器, 用 mlearnweb 仓库自己的 deploy 脚本安装.

    服务清单 (推理端):
      vnpy_headless    run_ml_headless.py — vnpy 主进程 (策略 + 双 cron + 撮合)
                       内部自动 spawn:
                         * vnpy_webtrader uvicorn :8001  (mlearnweb 通过此端拉)
                         * 推理子进程 (短生命周期, 21:00 cron 触发)
                       NSSM 重启 vnpy_headless 时, 子进程一并重启 — 不需要
                       为子进程单独装服务.

    NSSM 安装: https://nssm.cc/  或  choco install nssm

.PARAMETER NssmPath
    nssm.exe 路径. 默认 'nssm' (假定已在 PATH).

.PARAMETER VnpyRoot
    vnpy_strategy_dev 仓库根. 默认 F:\Quant\vnpy\vnpy_strategy_dev.

.PARAMETER VnpyPython
    vnpy 主 Python (3.13). 默认 F:\Program_Home\vnpy\python.exe.

.PARAMETER LogRoot
    NSSM 日志输出目录. 默认 D:\vnpy_logs.

.EXAMPLE
    PS C:\> .\deploy\install_services.ps1

.EXAMPLE
    PS C:\> .\deploy\install_services.ps1 -VnpyPython "C:\Python313\python.exe"

.NOTES
    管理员权限必须. 卸载用 deploy\uninstall_services.ps1.
    监控端 (mlearnweb) 部署见 mlearnweb 项目 deploy/.
#>

[CmdletBinding()]
param(
    [string]$NssmPath   = "nssm",
    [string]$VnpyRoot   = "F:\Quant\vnpy\vnpy_strategy_dev",
    [string]$VnpyPython = "F:\Program_Home\vnpy\python.exe",
    [string]$LogRoot    = "D:\vnpy_logs"
)

# ─── 前置检查 ─────────────────────────────────────────────────────────────

function Test-PathOrFail {
    param([string]$Path, [string]$What)
    if (-not (Test-Path $Path)) {
        Write-Error "[X] $What 不存在: $Path"
        exit 1
    }
}

Write-Host ("=" * 60)
Write-Host "P0-4 推理端 NSSM 服务化"
Write-Host ("=" * 60)

# 1. NSSM 在 PATH 或显式给路径
$nssmExe = Get-Command $NssmPath -ErrorAction SilentlyContinue
if (-not $nssmExe) {
    Write-Error "[X] nssm 未找到 (路径: $NssmPath). 装 NSSM (https://nssm.cc/) 或 choco install nssm"
    exit 1
}
Write-Host "[OK] nssm = $($nssmExe.Source)"

# 2. 关键路径 + .env / yaml 配置 (P0-1 + P0-2)
Test-PathOrFail $VnpyRoot "VnpyRoot"
Test-PathOrFail $VnpyPython "VnpyPython"
Test-PathOrFail "$VnpyRoot\run_ml_headless.py" "run_ml_headless.py"
Test-PathOrFail "$VnpyRoot\.env.production" ".env.production (P0-1, 拷贝 .env.example 后填值)"
Test-PathOrFail "$VnpyRoot\config\strategies.production.yaml" "config\strategies.production.yaml (P0-2)"

Write-Host "[OK] 路径 + 配置 检查通过"

# 3. 创建日志目录
if (-not (Test-Path $LogRoot)) {
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    Write-Host "[OK] 创建日志目录 $LogRoot"
}


# ─── 装 vnpy_headless 服务 ────────────────────────────────────────────────

$svc = "vnpy_headless"
Write-Host ""
Write-Host "─── 安装服务: $svc ───" -ForegroundColor Cyan

# 已存在 → 先 stop + remove (重装)
# 不用 'nssm status' 检查 — 服务不存在时它写 stderr "Can't open service!",
# PS5.1 + 父脚本 $ErrorActionPreference=Stop 会被包装成 NativeCommandError.
# 改用 Get-Service 静默判断, 不触发原生 stderr.
$existing = Get-Service -Name $svc -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  [!] $svc 已存在 (status=$($existing.Status)), 先 stop + remove"
    if ($existing.Status -eq 'Running') {
        & $nssmExe.Source stop $svc | Out-Null
        Start-Sleep -Seconds 2
    }
    & $nssmExe.Source remove $svc confirm | Out-Null
    Start-Sleep -Seconds 1
}

# install
& $nssmExe.Source install $svc $VnpyPython "$VnpyRoot\run_ml_headless.py" | Out-Null
& $nssmExe.Source set $svc AppDirectory $VnpyRoot | Out-Null

# 日志重定向 + 滚动 (P1-2 部分覆盖)
& $nssmExe.Source set $svc AppStdout "$LogRoot\$svc.log" | Out-Null
& $nssmExe.Source set $svc AppStderr "$LogRoot\$svc.err" | Out-Null
& $nssmExe.Source set $svc AppRotateFiles 1 | Out-Null
& $nssmExe.Source set $svc AppRotateOnline 1 | Out-Null
& $nssmExe.Source set $svc AppRotateBytes 10485760 | Out-Null   # 10 MB

# 崩溃重启 (10s 防震荡)
& $nssmExe.Source set $svc AppRestartDelay 10000 | Out-Null

# 开机自启
& $nssmExe.Source set $svc Start SERVICE_AUTO_START | Out-Null

# 描述
& $nssmExe.Source set $svc Description "vnpy ML 策略推理端 (run_ml_headless.py): 双 cron 调度 + 推理 spawn + 撮合 + webtrader HTTP 8001" | Out-Null

# P1-5 雏形: 推理子进程优先级低于 OS / 网络 I/O, 避免抢主进程 / mlearnweb 拉数据
& $nssmExe.Source set $svc AppPriority BELOW_NORMAL_PRIORITY_CLASS | Out-Null

Write-Host "[OK] $svc 装好 (logs -> $LogRoot\$svc.log)"


# ─── 启动 + 状态 ─────────────────────────────────────────────────────────

Write-Host ""
Write-Host ("=" * 60)
Write-Host "启动服务..." -ForegroundColor Cyan
Write-Host ("=" * 60)

& $nssmExe.Source start $svc
Start-Sleep -Seconds 5
$status = & $nssmExe.Source status $svc
if ($status -match "RUNNING") {
    Write-Host "[OK] $svc -> RUNNING" -ForegroundColor Green
} else {
    Write-Host "[!] $svc -> $status (检查 $LogRoot\$svc.err)" -ForegroundColor Yellow
}


# ─── 验收 ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host ("=" * 60)
Write-Host "验收 cmd:"
Write-Host ("=" * 60)
Write-Host @"

# 1. 服务状态
nssm status vnpy_headless

# 2. 实时日志 (vnpy 主进程 stdout)
Get-Content $LogRoot\vnpy_headless.log -Wait -Tail 50

# 3. stderr (推理子进程异常 / qlib import 失败 等)
Get-Content $LogRoot\vnpy_headless.err -Wait -Tail 50

# 4. 端口 (启动 ~10s 后)
Test-NetConnection 127.0.0.1 -Port 2014   # vnpy_webtrader RPC
Test-NetConnection 127.0.0.1 -Port 4102   # vnpy_webtrader RPC pub
Test-NetConnection 127.0.0.1 -Port 8001   # vnpy_webtrader HTTP (mlearnweb 拉数据入口)

# 5. mlearnweb (另一台机器 / 同机另一项目) 连过来
#    在 mlearnweb 部署机的 vnpy_nodes.yaml 里配:
#      base_url: http://<推理机 IP>:8001
#    详见 mlearnweb 项目 deploy/README.md

# 6. 重启 / 卸载
nssm restart vnpy_headless
.\deploy\uninstall_services.ps1
"@
