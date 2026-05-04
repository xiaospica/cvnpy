# [P2-2] vnpy_strategy_dev 推理服务器一键 IaC bootstrap
#
# 干啥:
#   一台空 Windows server 上从零跑通推理端 stack 的最少必要步骤. 涵盖:
#     1. 前置检查 (Python 解释器 / NSSM / 7zip / .env / yaml)
#     2. 数据目录创建 (${QS_DATA_ROOT}, ML_OUTPUT_ROOT, LOG_ROOT, 备份盘)
#     3. Python 依赖安装 (vnpy 主 + 推理子两个 env, 关键 wheel)
#     4. (可选) NTP 同步 (configure_ntp.ps1)
#     5. (可选) NSSM 服务化 (install_services.ps1)
#     6. (可选) 任务计划程序 — 每日 02:00 备份
#     7. (可选) 一次冷启动 dry-run — 不连券商, 仅验证 import + .env 解析 OK
#
# 运行模式:
#   .\deploy\bootstrap.ps1 -Check          # 只跑前置检查, 不动任何东西 (默认)
#   .\deploy\bootstrap.ps1 -Apply          # 执行所有步骤 (Administrator 必需)
#   .\deploy\bootstrap.ps1 -Apply -SkipServices  # Apply 但不装 NSSM 服务
#
# 已就位的组件 (本脚本不重复实现, 调用其他脚本):
#   - configure_ntp.ps1   — NTP 配置
#   - install_services.ps1 — NSSM 装 vnpy_headless
#   - daily_backup.ps1    — 备份逻辑 (本脚本只调度任务)
#
# 不做的:
#   - 安装 Python / NSSM / 7zip 二进制 (用 winget / choco 自己装, 建议先做)
#   - 装 miniqmt 客户端 (券商私有 installer, 走券商提供的安装流程)
#   - 拷 bundle (训练机 rsync; 详见 vnpy_ml_strategy/docs/deployment.md Step 8)
#   - 改 vt_setting.json (P0-1 凭证, 详见 deployment.md Step 4c)

[CmdletBinding(DefaultParameterSetName = 'Check')]
param(
    [Parameter(ParameterSetName = 'Check')]
    [switch]$Check,

    [Parameter(ParameterSetName = 'Apply')]
    [switch]$Apply,

    [Parameter(ParameterSetName = 'Apply')]
    [switch]$SkipServices,

    [Parameter(ParameterSetName = 'Apply')]
    [switch]$SkipNtp,

    [Parameter(ParameterSetName = 'Apply')]
    [switch]$SkipBackupSchedule,

    [Parameter(ParameterSetName = 'Apply')]
    [switch]$SkipDryRun,

    [string]$VnpyRoot = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
    [string]$VnpyPython = "F:\Program_Home\vnpy\python.exe",
    [string]$InferencePython = "E:\ssd_backup\Pycharm_project\python-3.11.0-amd64\python.exe",
    [string]$QsDataRoot = "D:\vnpy_data",
    [string]$MlOutputRoot = "D:\ml_output",
    [string]$LogRoot = "D:\vnpy_logs",
    [string]$BackupRoot = "D:\backups"
)

$ErrorActionPreference = "Stop"
$IsApplyMode = $PSCmdlet.ParameterSetName -eq 'Apply'

# ---- helper ---------------------------------------------------------------

$script:CheckResults = @()
$script:ChecksFailed = 0
$script:WarningsCount = 0

function Add-Check($name, $ok, $detail, [switch]$IsWarning) {
    $status = if ($ok) { "OK" } elseif ($IsWarning) { "WARN" } else { "FAIL" }
    $color  = if ($ok) { "Green" } elseif ($IsWarning) { "Yellow" } else { "Red" }
    Write-Host ("  [{0,-4}] {1}: {2}" -f $status, $name, $detail) -ForegroundColor $color
    $script:CheckResults += [pscustomobject]@{ name = $name; ok = $ok; detail = $detail; warn = $IsWarning.IsPresent }
    if (-not $ok -and -not $IsWarning) { $script:ChecksFailed++ }
    if ($IsWarning) { $script:WarningsCount++ }
}

function Section($title) {
    Write-Host ""
    Write-Host ("─" * 70) -ForegroundColor DarkGray
    Write-Host $title -ForegroundColor Cyan
    Write-Host ("─" * 70) -ForegroundColor DarkGray
}

function Require-Admin() {
    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Error "Apply 模式必须以 Administrator 运行 (NSSM 装服务 / 任务计划程序 / NTP 配置 都需要)."
        exit 1
    }
}

# ---- step 1: 前置检查 -----------------------------------------------------

Section "Step 1 · 前置检查 (binaries + paths)"

# Python 解释器
$vpyOk = Test-Path $VnpyPython
$detail = if ($vpyOk) { (& $VnpyPython --version 2>&1) -join " " } else { "缺失 $VnpyPython — 自己装 vnpy 用的 Python 3.13 (winget install Python.Python.3.13)" }
Add-Check "vnpy Python" $vpyOk $detail

$ipyOk = Test-Path $InferencePython
$detail = if ($ipyOk) { (& $InferencePython --version 2>&1) -join " " } else { "缺失 $InferencePython — 推理子进程用的 Python 3.11" }
Add-Check "inference Python" $ipyOk $detail

# NSSM
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
$nssmOk = $null -ne $nssm
$detail = if ($nssmOk) { $nssm.Source } else { "未找到 nssm — 跑 'choco install nssm' 或下 https://nssm.cc/ 手装" }
Add-Check "NSSM" $nssmOk $detail

# 7zip (备份用, 没装则降级 zip — 仅 warning)
$sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
$sevenOk = $null -ne $sevenZip
$detail = if ($sevenOk) { $sevenZip.Source } else { "未找到 7z — daily_backup.ps1 会降级 zip (压缩比稍差但可用)" }
Add-Check "7zip" $sevenOk $detail -IsWarning:(-not $sevenOk)

# 配置文件
$envProd = Join-Path $VnpyRoot ".env.production"
$envProdOk = Test-Path $envProd
$detail = if ($envProdOk) { $envProd } else { "缺 $envProd — 拷 .env.example 后填值, 详见 vnpy_ml_strategy/docs/deployment.md Step 4" }
Add-Check ".env.production" $envProdOk $detail

$yamlProd = Join-Path $VnpyRoot "config\strategies.production.yaml"
$yamlProdOk = Test-Path $yamlProd
$detail = if ($yamlProdOk) { $yamlProd } else { "缺 $yamlProd — 拷 strategies.example.yaml 后填值, 详见 deployment.md Step 9" }
Add-Check "strategies.production.yaml" $yamlProdOk $detail

# vt_setting.json (vnpy 框架配置, 含 tushare token / SMTP)
$vtSetting = "$env:USERPROFILE\.vntrader\vt_setting.json"
$vtOk = Test-Path $vtSetting
$detail = if ($vtOk) { $vtSetting } else { "缺 $vtSetting — 启动 vnpy 一次或手动创建; 详见 deployment.md Step 4c" }
Add-Check "vt_setting.json" $vtOk $detail -IsWarning:(-not $vtOk)

if ($script:ChecksFailed -gt 0) {
    Write-Host ""
    Write-Host "[bootstrap] 前置检查失败 $script:ChecksFailed 项, 修后再跑." -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "[bootstrap] 前置检查通过 (warning $script:WarningsCount 项, 不阻断)" -ForegroundColor Green

if (-not $IsApplyMode) {
    Write-Host ""
    Write-Host "[bootstrap] 当前是 -Check 模式, 不实际改任何状态." -ForegroundColor Yellow
    Write-Host "[bootstrap] 真正部署: .\deploy\bootstrap.ps1 -Apply  (Administrator)" -ForegroundColor Yellow
    exit 0
}

Require-Admin

# ---- step 2: 数据目录 -----------------------------------------------------

Section "Step 2 · 创建数据目录"

$dirs = @(
    $QsDataRoot,
    (Join-Path $QsDataRoot "snapshots\merged"),
    (Join-Path $QsDataRoot "snapshots\filtered"),
    (Join-Path $QsDataRoot "stock_data"),
    (Join-Path $QsDataRoot "state"),         # [A2] 状态统一目录
    (Join-Path $QsDataRoot "models"),
    (Join-Path $QsDataRoot "jq_index"),
    $MlOutputRoot,
    $LogRoot,
    $BackupRoot
)
foreach ($d in $dirs) {
    if (Test-Path $d) {
        Write-Host "  ✓ $d (existed)" -ForegroundColor DarkGreen
    } else {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Write-Host "  + $d (created)" -ForegroundColor Green
    }
}

# ---- step 3: Python 依赖 --------------------------------------------------

Section "Step 3 · Python 依赖安装"

$vnpyDeps = @("psutil", "python-dotenv", "pyyaml", "loguru", "apscheduler", "httpx", "pandas", "pyarrow")
Write-Host "  [vnpy env] $VnpyPython"
& $VnpyPython -m pip install --upgrade pip 2>&1 | Out-Null
$pipOut = & $VnpyPython -m pip install $vnpyDeps 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "vnpy env pip install 失败:`n$pipOut"
    exit 1
}
Write-Host "  ✓ vnpy env: $($vnpyDeps -join ', ') 已就位" -ForegroundColor Green

$inferDeps = @("psutil", "pandas", "pyarrow", "lightgbm", "scikit-learn", "mlflow")
Write-Host "  [inference env] $InferencePython"
& $InferencePython -m pip install --upgrade pip 2>&1 | Out-Null
$pipOut = & $InferencePython -m pip install $inferDeps 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warning "inference env pip install 失败 (可能是 ta-lib wheel 未装), 继续:`n$pipOut"
} else {
    Write-Host "  ✓ inference env: $($inferDeps -join ', ') 已就位" -ForegroundColor Green
}

# ---- step 4: NTP ----------------------------------------------------------

if (-not $SkipNtp) {
    Section "Step 4 · NTP 时钟同步"
    $ntpScript = Join-Path $VnpyRoot "deploy\configure_ntp.ps1"
    if (Test-Path $ntpScript) {
        & $ntpScript
    } else {
        Write-Warning "configure_ntp.ps1 不存在, 跳过"
    }
}

# ---- step 5: NSSM 服务 ----------------------------------------------------

if (-not $SkipServices) {
    Section "Step 5 · NSSM 服务化"
    $instScript = Join-Path $VnpyRoot "deploy\install_services.ps1"
    if (Test-Path $instScript) {
        & $instScript -VnpyRoot $VnpyRoot -VnpyPython $VnpyPython -LogRoot $LogRoot
    } else {
        Write-Warning "install_services.ps1 不存在, 跳过"
    }
}

# ---- step 6: 备份计划任务 -------------------------------------------------

if (-not $SkipBackupSchedule) {
    Section "Step 6 · 任务计划程序: 每日 02:00 备份"

    $backupScript = Join-Path $VnpyRoot "deploy\daily_backup.ps1"
    if (-not (Test-Path $backupScript)) {
        Write-Warning "daily_backup.ps1 不存在, 跳过任务计划"
    } else {
        $taskName = "vnpy_daily_backup"
        $existing = schtasks /query /tn $taskName 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  ✓ task '$taskName' 已存在 (skip)" -ForegroundColor DarkGreen
        } else {
            $tr = "powershell -ExecutionPolicy Bypass -File `"$backupScript`""
            schtasks /create /tn $taskName /tr $tr /sc daily /st 02:00 /ru SYSTEM /f | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  + task '$taskName' 已创建 (每日 02:00 跑)" -ForegroundColor Green
            } else {
                Write-Warning "schtasks 创建失败, 手动: schtasks /create /tn $taskName /tr `"$tr`" /sc daily /st 02:00 /ru SYSTEM"
            }
        }
    }
}

# ---- step 7: 冷启动 dry-run -----------------------------------------------

if (-not $SkipDryRun) {
    Section "Step 7 · run_ml_headless 冷启动 dry-run"
    Write-Host "  跑 'python -c \"import run_ml_headless\"' 验证 .env / yaml / import 链..." -ForegroundColor DarkGray
    Push-Location $VnpyRoot
    try {
        $tmpScript = @'
import sys
import importlib.util
spec = importlib.util.spec_from_file_location("rmh", "run_ml_headless.py")
m = importlib.util.module_from_spec(spec)
# 不调 main(), 仅模块加载 — 触发 .env / yaml / 校验函数定义
spec.loader.exec_module(m)
print("[dry-run] GATEWAYS=", len(m.GATEWAYS), "STRATEGIES=", len(m.STRATEGIES))
print("[dry-run] _validate_trigger_time_unique...")
m._validate_trigger_time_unique()
print("[dry-run] _validate_signal_source_consistency...")
m._validate_signal_source_consistency()
print("[dry-run] OK — 配置链路无问题")
'@
        $tmpFile = Join-Path $env:TEMP "rmh_dry_run_$(Get-Date -Format yyyyMMddHHmmss).py"
        Set-Content -Path $tmpFile -Value $tmpScript -Encoding utf8
        & $VnpyPython $tmpFile
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "dry-run 失败 — 检查 .env.production / strategies.production.yaml"
        } else {
            Write-Host "  ✓ dry-run pass" -ForegroundColor Green
        }
        Remove-Item $tmpFile -ErrorAction SilentlyContinue
    } finally {
        Pop-Location
    }
}

# ---- 总结 -----------------------------------------------------------------

Section "完成"

Write-Host ""
Write-Host "[bootstrap] 推理端 IaC bootstrap 完成." -ForegroundColor Green
Write-Host ""
Write-Host "下一步 (人工):" -ForegroundColor Cyan
Write-Host "  1. rsync bundle 到 $QsDataRoot\models\<run_id>\  (训练机 → 部署机)" -ForegroundColor White
Write-Host "  2. 第一次手动跑 daily_ingest 拉历史数据 (deployment.md Step 7)" -ForegroundColor White
Write-Host "  3. nssm start vnpy_headless" -ForegroundColor White
Write-Host "  4. Get-Content $LogRoot\vnpy_headless_$(Get-Date -Format yyyy-MM-dd).log -Wait -Tail 50" -ForegroundColor White
Write-Host "  5. mlearnweb 端 (单独部署机): cd mlearnweb; .\deploy\install_services.ps1" -ForegroundColor White
