#requires -RunAsAdministrator
<#
.SYNOPSIS
    P0-4 — 一键装 NSSM 服务 × 4 (vnpy_headless + mlearnweb 双 uvicorn).

.DESCRIPTION
    部署机以 Administrator 运行本脚本, 把 4 个长跑进程装成 Windows 服务,
    崩溃自动重启, 开机自启, 日志重定向到 D:\vnpy_logs\.

    服务清单:
      vnpy_headless          run_ml_headless.py (vnpy 主进程 + 推理 spawn)
      mlearnweb_research     mlearnweb research uvicorn :8000
      mlearnweb_live         mlearnweb live uvicorn :8100
      mlearnweb_frontend     (可选) Vite 前端 :5173 (仅生产部署需要)

    NSSM 安装地址: https://nssm.cc/
    或 choco install nssm

.PARAMETER NssmPath
    nssm.exe 路径. 默认 'nssm' (假定已在 PATH).

.PARAMETER VnpyRoot
    vnpy_strategy_dev 仓库根. 默认 F:\Quant\vnpy\vnpy_strategy_dev.

.PARAMETER VnpyPython
    vnpy 主 Python (3.13). 默认 F:\Program_Home\vnpy\python.exe.

.PARAMETER InferencePython
    推理 Python (3.11). 默认 E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe.

.PARAMETER MLearnwebRoot
    mlearnweb 仓库根. 默认 F:\Quant\code\qlib_strategy_dev\mlearnweb.

.PARAMETER LogRoot
    日志输出目录. 默认 D:\vnpy_logs\.

.PARAMETER InstallFrontend
    是否安装前端服务 (默认 false; 开发机用浏览器访问 npm run dev 即可).

.EXAMPLE
    PS C:\> .\deploy\install_services.ps1
    PS C:\> .\deploy\install_services.ps1 -InstallFrontend

.NOTES
    管理员权限必须. 第一次跑后用 'nssm status <service>' / 'nssm restart <service>'
    管理服务. 卸载用 deploy\uninstall_services.ps1.
#>

[CmdletBinding()]
param(
    [string]$NssmPath        = "nssm",
    [string]$VnpyRoot        = "F:\Quant\vnpy\vnpy_strategy_dev",
    [string]$VnpyPython      = "F:\Program_Home\vnpy\python.exe",
    [string]$InferencePython = "E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe",
    [string]$MLearnwebRoot   = "F:\Quant\code\qlib_strategy_dev\mlearnweb",
    [string]$LogRoot         = "D:\vnpy_logs",
    [switch]$InstallFrontend
)

# ─── 前置检查 ─────────────────────────────────────────────────────────────

function Test-Path-Or-Fail {
    param([string]$Path, [string]$What)
    if (-not (Test-Path $Path)) {
        Write-Error "❌ $What 不存在: $Path"
        exit 1
    }
}

Write-Host "=" * 60
Write-Host "P0-4 NSSM 服务化安装"
Write-Host "=" * 60

# 1. NSSM 在 PATH 或显式给路径
$nssmExe = Get-Command $NssmPath -ErrorAction SilentlyContinue
if (-not $nssmExe) {
    Write-Error "❌ nssm 未找到 (路径: $NssmPath). 装 NSSM (https://nssm.cc/) 或 choco install nssm"
    exit 1
}
Write-Host "✓ nssm = $($nssmExe.Source)"

# 2. 关键路径
Test-Path-Or-Fail $VnpyRoot "VnpyRoot"
Test-Path-Or-Fail $VnpyPython "VnpyPython"
Test-Path-Or-Fail $InferencePython "InferencePython"
Test-Path-Or-Fail "$VnpyRoot\run_ml_headless.py" "run_ml_headless.py"
Test-Path-Or-Fail "$VnpyRoot\.env.production" ".env.production (P0-1)"
Test-Path-Or-Fail "$VnpyRoot\config\strategies.production.yaml" "strategies.production.yaml (P0-2)"
Test-Path-Or-Fail $MLearnwebRoot "MLearnwebRoot"
Test-Path-Or-Fail "$MLearnwebRoot\backend\app\main.py" "mlearnweb backend/app/main.py"

Write-Host "✓ 路径检查通过"

# 3. 创建日志目录
if (-not (Test-Path $LogRoot)) {
    New-Item -ItemType Directory -Path $LogRoot | Out-Null
    Write-Host "✓ 创建日志目录 $LogRoot"
}


# ─── 服务通用配置 helper ──────────────────────────────────────────────────

function Install-NssmService {
    param(
        [string]$Name,
        [string]$Application,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$Description = ""
    )

    Write-Host ""
    Write-Host "─── 安装服务: $Name ───" -ForegroundColor Cyan

    # 已存在 → 先 stop + remove (重装)
    & $nssmExe.Source status $Name 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  ⚠️ $Name 已存在, 先 stop + remove"
        & $nssmExe.Source stop $Name | Out-Null
        Start-Sleep -Seconds 2
        & $nssmExe.Source remove $Name confirm | Out-Null
    }

    # 装
    & $nssmExe.Source install $Name $Application $Arguments | Out-Null
    & $nssmExe.Source set $Name AppDirectory $WorkingDirectory | Out-Null
    & $nssmExe.Source set $Name AppStdout "$LogRoot\$Name.log" | Out-Null
    & $nssmExe.Source set $Name AppStderr "$LogRoot\$Name.err" | Out-Null
    # 日志滚动: 10 MB 一个文件, 保留 7 天
    & $nssmExe.Source set $Name AppRotateFiles 1 | Out-Null
    & $nssmExe.Source set $Name AppRotateOnline 1 | Out-Null
    & $nssmExe.Source set $Name AppRotateBytes 10485760 | Out-Null
    # 崩溃自动重启 (10 秒延迟)
    & $nssmExe.Source set $Name AppRestartDelay 10000 | Out-Null
    # 开机自启
    & $nssmExe.Source set $Name Start SERVICE_AUTO_START | Out-Null
    if ($Description) {
        & $nssmExe.Source set $Name Description $Description | Out-Null
    }
    # P1-5: 推理子进程优先级低 (避免抢主进程)
    if ($Name -eq "vnpy_headless") {
        & $nssmExe.Source set $Name AppPriority BELOW_NORMAL_PRIORITY_CLASS | Out-Null
    }

    Write-Host "✓ $Name 装好 (logs → $LogRoot\$Name.log)"
}


# ─── 1. vnpy_headless ─────────────────────────────────────────────────────

Install-NssmService `
    -Name "vnpy_headless" `
    -Application $VnpyPython `
    -Arguments "$VnpyRoot\run_ml_headless.py" `
    -WorkingDirectory $VnpyRoot `
    -Description "vnpy ML 策略主进程 (含 webtrader uvicorn 8001 spawn)"


# ─── 2. mlearnweb_research (uvicorn :8000) ───────────────────────────────

Install-NssmService `
    -Name "mlearnweb_research" `
    -Application $InferencePython `
    -Arguments "-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info" `
    -WorkingDirectory "$MLearnwebRoot\backend" `
    -Description "mlearnweb 研究侧 (实验/训练记录/SHAP)"


# ─── 3. mlearnweb_live (uvicorn :8100) ───────────────────────────────────

Install-NssmService `
    -Name "mlearnweb_live" `
    -Application $InferencePython `
    -Arguments "-m uvicorn app.live_main:app --host 127.0.0.1 --port 8100 --log-level info" `
    -WorkingDirectory "$MLearnwebRoot\backend" `
    -Description "mlearnweb 实盘监控 (5 个 sync_loop / 控制 endpoint)"


# ─── 4. mlearnweb_frontend (可选, Vite :5173) ────────────────────────────

if ($InstallFrontend) {
    $npmCmd = (Get-Command npm -ErrorAction SilentlyContinue).Source
    if (-not $npmCmd) {
        Write-Warning "npm 未找到, 跳过前端服务安装. 装 Node.js 或手动 npm run build + serve dist/"
    } else {
        Install-NssmService `
            -Name "mlearnweb_frontend" `
            -Application "node.exe" `
            -Arguments "$MLearnwebRoot\frontend\node_modules\vite\bin\vite.js preview --host 127.0.0.1 --port 5173" `
            -WorkingDirectory "$MLearnwebRoot\frontend" `
            -Description "mlearnweb 前端 Vite preview (生产模式)"
    }
} else {
    Write-Host ""
    Write-Host "ℹ️ 未装前端服务 (-InstallFrontend 启用)" -ForegroundColor Yellow
    Write-Host "  开发: cd $MLearnwebRoot\frontend && npm run dev"
    Write-Host "  生产: 用 nginx / IIS 服务 frontend\dist\ 静态文件"
}


# ─── 启动 + 状态 ─────────────────────────────────────────────────────────

Write-Host ""
Write-Host "=" * 60
Write-Host "启动所有服务..." -ForegroundColor Cyan
Write-Host "=" * 60

$services = @("vnpy_headless", "mlearnweb_research", "mlearnweb_live")
if ($InstallFrontend -and $npmCmd) { $services += "mlearnweb_frontend" }

foreach ($svc in $services) {
    Write-Host ""
    & $nssmExe.Source start $svc
    Start-Sleep -Seconds 3
    $status = & $nssmExe.Source status $svc
    if ($status -match "RUNNING") {
        Write-Host "✓ $svc → RUNNING" -ForegroundColor Green
    } else {
        Write-Host "⚠️ $svc → $status (检查 $LogRoot\$svc.err)" -ForegroundColor Yellow
    }
}


# ─── 验收 ────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "=" * 60
Write-Host "验收 cmd:"
Write-Host "=" * 60
Write-Host @"

# 1. 看服务状态
nssm status vnpy_headless
nssm status mlearnweb_research
nssm status mlearnweb_live

# 2. 看实时日志
Get-Content $LogRoot\vnpy_headless.log -Wait -Tail 50

# 3. 测端口 (vnpy 主进程启动 ~10s 后)
Test-NetConnection 127.0.0.1 -Port 8001  # vnpy_webtrader HTTP
Test-NetConnection 127.0.0.1 -Port 8000  # mlearnweb research
Test-NetConnection 127.0.0.1 -Port 8100  # mlearnweb live

# 4. 浏览器
# http://localhost:5173/live-trading

# 5. 重启某服务
nssm restart vnpy_headless

# 6. 卸载所有服务
.\deploy\uninstall_services.ps1
"@
