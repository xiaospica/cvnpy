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
# 自动装的依赖 (无需用户预先 winget):
#   - NSSM: winget → 官方 zip 直下载 fallback. 关闭走 -NoAutoInstallNssm
#
# 不做的:
#   - 安装 Python (3.13 vnpy + 3.11 推理) — 体积大, 自己 winget install Python.Python.3.13
#   - 安装 7zip (备份脚本可降级 zip, 不阻断)
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

    # 默认开启 NSSM 自动安装. 关闭时若未找到 nssm Apply 阶段 raise.
    # 自动安装顺序: winget → 直接下载官方 zip 解压 (无前置依赖)
    [Parameter(ParameterSetName = 'Apply')]
    [switch]$NoAutoInstallNssm,

    # 默认开启 Python 自动装. 关闭时若 .env / 自动检测都拿不到 Python 则 raise.
    # 检测顺序: 显式 -VnpyPython/-InferencePython > .env.production > py launcher >
    # 常见安装位置 > 注册表; 仍找不到时 -Apply 自动 'winget install Python.Python.X'
    [Parameter(ParameterSetName = 'Apply')]
    [switch]$NoAutoInstallPython,

    [string]$VnpyRoot = (Split-Path -Parent (Split-Path -Parent $PSCommandPath)),
    # 所有路径留空 = 从 .env.production / 自动检测 取 (见 deploy/_lib.ps1
    # Get-DeployContext). 显式给则跳过解析直接用. 部署机一般只需编辑
    # .env.production 一处, 不需要在每个脚本上改命令行.
    [string]$VnpyPython = "",
    [string]$InferencePython = "",
    [string]$QsDataRoot = "",
    [string]$MlOutputRoot = "",
    [string]$LogRoot = "",
    [string]$BackupRoot = "",
    # NSSM 直下载 fallback 用. 默认装到 C:\Program Files\nssm\, 加系统 PATH.
    [string]$NssmInstallDir = "C:\Program Files\nssm",
    [string]$NssmDownloadUrl = "https://nssm.cc/release/nssm-2.24.zip"
)

$ErrorActionPreference = "Stop"
$IsApplyMode = $PSCmdlet.ParameterSetName -eq 'Apply'

# 共享 deploy helper (Read-EnvFile / Find-PythonExe / Get-DeployContext)
. (Join-Path $PSScriptRoot "_lib.ps1")

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

function Install-Python($majorMinor) {
    <#
    .SYNOPSIS
        winget 装 Python X.Y. 装完用 _lib.Find-PythonExe 取路径.
        winget 不可用时 throw, 让用户自己装.
    #>
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget 不可用 — 手动装 Python $majorMinor (https://www.python.org/downloads/)"
    }
    Write-Host "  [py$majorMinor] 走 winget 装 Python.Python.$majorMinor..." -ForegroundColor DarkGray
    & winget install --id "Python.Python.$majorMinor" --silent --accept-source-agreements --accept-package-agreements 2>&1 | Out-Host
    # 刷新 PATH (winget 装的解释器可能新加 PATH 项)
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    return Find-PythonExe -MajorMinor $majorMinor
}

function Install-Nssm() {
    <#
    .SYNOPSIS
        自动装 NSSM. 三层 fallback, 不依赖 chocolatey.

    .DESCRIPTION
        NSSM 是单 exe 工具, 装法非常轻. 顺序:
          1. winget (Win11 / Server 2022 自带, 无人值守一行)
          2. 直接下载官方 zip → 提 nssm.exe → 加系统 PATH
        全失败时 throw, 由调用方决定 abort.
    #>

    # 1. 尝试 winget — Win11 / Server 2022 默认装
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "  [nssm] 走 winget 装..." -ForegroundColor DarkGray
        $rc = & winget install --id NSSM.NSSM --silent --accept-source-agreements --accept-package-agreements 2>&1
        # winget 可能返回 "已是最新" 或 "成功", 都视为 OK; 检查 PATH 即可.
        # 刷新当前进程 PATH (winget 装的工具新增到系统 PATH, 但当前会话不会自动取)
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        if (Get-Command nssm -ErrorAction SilentlyContinue) {
            Write-Host "  [nssm] winget 装成功" -ForegroundColor Green
            return $true
        }
        Write-Host "  [nssm] winget 装似乎完成但 PATH 没刷新出来 — 转 fallback" -ForegroundColor Yellow
    }

    # 2. fallback: 直接下载官方 zip → 提 nssm.exe
    Write-Host "  [nssm] 走 zip 直下载 ($NssmDownloadUrl)..." -ForegroundColor DarkGray
    if (-not (Test-Path $NssmInstallDir)) {
        New-Item -ItemType Directory -Path $NssmInstallDir -Force | Out-Null
    }
    $tmpZip = Join-Path $env:TEMP "nssm_$(Get-Date -Format yyyyMMddHHmmss).zip"
    $tmpDir = Join-Path $env:TEMP "nssm_extract_$(Get-Date -Format yyyyMMddHHmmss)"

    try {
        # TLS 1.2 给 nssm.cc HTTPS — 老 PS5 默认 TLS 可能太低
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $NssmDownloadUrl -OutFile $tmpZip -UseBasicParsing
        Expand-Archive -Path $tmpZip -DestinationPath $tmpDir -Force

        # zip 内部结构: nssm-2.24/win64/nssm.exe
        $exeCandidate = Get-ChildItem -Path $tmpDir -Recurse -Filter "nssm.exe" |
            Where-Object { $_.FullName -match "win64" } |
            Select-Object -First 1
        if (-not $exeCandidate) {
            # 兜底: 任意 nssm.exe (32-bit)
            $exeCandidate = Get-ChildItem -Path $tmpDir -Recurse -Filter "nssm.exe" |
                Select-Object -First 1
        }
        if (-not $exeCandidate) {
            throw "下载的 zip 里没找到 nssm.exe"
        }

        Copy-Item $exeCandidate.FullName (Join-Path $NssmInstallDir "nssm.exe") -Force
        Write-Host "  [nssm] 已装到 $NssmInstallDir\nssm.exe" -ForegroundColor Green
    }
    finally {
        Remove-Item $tmpZip -ErrorAction SilentlyContinue
        Remove-Item $tmpDir -Recurse -ErrorAction SilentlyContinue
    }

    # 加到系统 PATH (要 admin, Apply 模式已经要求)
    $sysPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($sysPath -notlike "*$NssmInstallDir*") {
        [System.Environment]::SetEnvironmentVariable("Path", "$sysPath;$NssmInstallDir", "Machine")
        Write-Host "  [nssm] 已加 $NssmInstallDir 到系统 PATH" -ForegroundColor Green
    }
    # 当前进程 PATH 同步 (新装的 nssm 立即可用)
    $env:Path = "$env:Path;$NssmInstallDir"

    if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
        throw "NSSM 装好了但 'nssm' 仍不在 PATH; 手动: $NssmInstallDir\nssm.exe install ..."
    }
    return $true
}

# ---- step 1: 前置检查 -----------------------------------------------------

Section "Step 1 · 前置检查 (binaries + paths)"

# 路径单一来源 (.env.production via Get-DeployContext). 所有未显式给的 param
# 都从 ctx 填充 — 部署机用户只需编辑 .env.production 即可.
$ctx = Get-DeployContext -RepoRoot $VnpyRoot
if (-not $VnpyPython)      { $VnpyPython      = $ctx.VnpyPython }
if (-not $InferencePython) { $InferencePython = $ctx.InferencePython }
if (-not $QsDataRoot)      { $QsDataRoot      = $ctx.QsDataRoot }
if (-not $MlOutputRoot)    { $MlOutputRoot    = $ctx.MlOutputRoot }
if (-not $LogRoot)         { $LogRoot         = $ctx.LogRoot }
if (-not $BackupRoot)      { $BackupRoot      = $ctx.BackupRoot }

# Python 检测兜底 — Get-DeployContext 已尝试 Find-PythonExe, 这里仅记录
# 当前是否找到 (用于 Step 1.5a 决策是否走 winget 自动装)
if ($VnpyPython -and -not (Test-Path $VnpyPython)) { $VnpyPython = "" }
if ($InferencePython -and -not (Test-Path $InferencePython)) { $InferencePython = "" }
$vpyOk = $VnpyPython -and (Test-Path $VnpyPython)
$vpyAutoInstall = (-not $vpyOk) -and ((-not $IsApplyMode) -or (-not $NoAutoInstallPython))
if ($vpyOk) {
    Add-Check "vnpy Python (3.13)" $true ((& $VnpyPython --version 2>&1) -join " " + " @ $VnpyPython")
} elseif ($vpyAutoInstall) {
    Add-Check "vnpy Python (3.13)" $false "未检测到 — Apply 时自动 'winget install Python.Python.3.13'" -IsWarning
} else {
    Add-Check "vnpy Python (3.13)" $false "未检测到, 且 -NoAutoInstallPython 已设. 手动: winget install Python.Python.3.13"
}

$ipyOk = $InferencePython -and (Test-Path $InferencePython)
$ipyAutoInstall = (-not $ipyOk) -and ((-not $IsApplyMode) -or (-not $NoAutoInstallPython))
if ($ipyOk) {
    Add-Check "inference Python (3.11)" $true ((& $InferencePython --version 2>&1) -join " " + " @ $InferencePython")
} elseif ($ipyAutoInstall) {
    Add-Check "inference Python (3.11)" $false "未检测到 — Apply 时自动 'winget install Python.Python.3.11'" -IsWarning
} else {
    Add-Check "inference Python (3.11)" $false "未检测到, 且 -NoAutoInstallPython 已设. 手动: winget install Python.Python.3.11"
}

# NSSM — 装在 PATH 里; 没装时 Apply 模式会自动装 (除非 -NoAutoInstallNssm)
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
$nssmOk = $null -ne $nssm
$nssmAutoInstall = (-not $nssmOk) -and ((-not $IsApplyMode) -or (-not $NoAutoInstallNssm))
if ($nssmOk) {
    $detail = $nssm.Source
    Add-Check "NSSM" $true $detail
} elseif ($nssmAutoInstall) {
    # 没装但会自动装 → WARN, 不阻断 -Check (Apply 阶段 Step 1.5 会装并复查)
    $detail = "未找到 nssm — Apply 时自动装 (winget → 官方 zip 直下载 fallback)"
    Add-Check "NSSM" $false $detail -IsWarning
} else {
    # Apply + -NoAutoInstallNssm 时才算 FAIL
    $detail = "未找到 nssm 且 -NoAutoInstallNssm 已设 — 手动: winget install NSSM.NSSM"
    Add-Check "NSSM" $false $detail
}

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

# ---- step 1.5a: Python 自动安装 ------------------------------------------

if (-not $vpyOk) {
    if ($NoAutoInstallPython) {
        Write-Error "[bootstrap] vnpy Python (3.13) 未检测到, 且 -NoAutoInstallPython 已设. abort."
        exit 1
    }
    Section "Step 1.5a · 自动安装 vnpy Python (3.13)"
    try {
        $installed = Install-Python "3.13"
        if (-not $installed -or -not (Test-Path $installed)) {
            throw "winget install 完成但 Find-PythonExe 仍找不到"
        }
        $VnpyPython = $installed
        Write-Host "  ✓ Python 3.13 现在可用: $VnpyPython" -ForegroundColor Green
    }
    catch {
        Write-Error "[bootstrap] 自动装 Python 3.13 失败: $_`n手动: 下 https://www.python.org/downloads/release/python-3138/"
        exit 1
    }
}

if (-not $ipyOk) {
    if ($NoAutoInstallPython) {
        Write-Error "[bootstrap] inference Python (3.11) 未检测到, 且 -NoAutoInstallPython 已设. abort."
        exit 1
    }
    Section "Step 1.5a · 自动安装 inference Python (3.11)"
    try {
        $installed = Install-Python "3.11"
        if (-not $installed -or -not (Test-Path $installed)) {
            throw "winget install 完成但 Find-PythonExe 仍找不到"
        }
        $InferencePython = $installed
        Write-Host "  ✓ Python 3.11 现在可用: $InferencePython" -ForegroundColor Green
    }
    catch {
        Write-Error "[bootstrap] 自动装 Python 3.11 失败: $_`n手动: 下 https://www.python.org/downloads/release/python-3119/"
        exit 1
    }
}

# ---- step 1.5b: NSSM 自动安装 --------------------------------------------

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    if ($NoAutoInstallNssm) {
        Write-Error "[bootstrap] NSSM 未装且 -NoAutoInstallNssm 已设, abort. 手动装后重试."
        exit 1
    }
    Section "Step 1.5b · 自动安装 NSSM"
    try {
        Install-Nssm | Out-Null
        # 二次校验
        $nssmCmd = Get-Command nssm -ErrorAction SilentlyContinue
        if (-not $nssmCmd) {
            throw "Install-Nssm 返回但 nssm 仍不可用"
        }
        Write-Host "  ✓ nssm 现在可用: $($nssmCmd.Source)" -ForegroundColor Green
    }
    catch {
        Write-Error "[bootstrap] 自动装 NSSM 失败: $_`n手动: 下 https://nssm.cc/release/nssm-2.24.zip 解压, 把 win64\nssm.exe 放到 PATH 上"
        exit 1
    }
}

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
