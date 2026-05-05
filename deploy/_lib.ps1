# vnpy_strategy_dev 部署脚本共享 helper.
#
# 作用: 让 bootstrap.ps1 / install_services.ps1 / daily_backup.ps1 /
# uninstall_services.ps1 共用一份"路径来源"逻辑, 用户只在 .env.production
# 编辑一次, 所有脚本同步生效.
#
# 用法:
#   . (Join-Path $PSScriptRoot "_lib.ps1")    # 顶部 dot-source
#   $ctx = Get-DeployContext -RepoRoot <repo>
#   $ctx.VnpyPython, $ctx.LogRoot, ...        # 直接用
#
# 解析优先级 (高 → 低):
#   1. 显式 -ParamName <值> (调用方 explicit)
#   2. .env.production 字段 (KEY=VALUE 行解析)
#   3. 自动检测 (Python: py launcher / 注册表 / 常见位置)
#   4. 安全默认值 (D:\vnpy_data, D:\vnpy_logs, D:\backups, ...)


function Read-EnvFile {
    <#
    .SYNOPSIS
        简单 .env 解析为 hashtable.

    .DESCRIPTION
        只支持 KEY=VALUE 单行格式. 不做插值 / 不解析多行 / 不做 export 关键字
        (vnpy 的 .env 格式很简单, 不需要完整 dotenv 实现).
        # 开头的行 + 空行 跳过. 两端 " 或 ' 引号会被去掉.
    #>
    param([string]$Path)
    if (-not (Test-Path $Path)) { return @{} }
    $envMap = @{}
    Get-Content $Path -ErrorAction SilentlyContinue | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) { return }
        if ($line -match '^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $val = $matches[2].Trim()
            if ($val -match '^"(.*)"$' -or $val -match "^'(.*)'$") {
                $val = $matches[1]
            }
            $envMap[$matches[1]] = $val
        }
    }
    return $envMap
}


function Find-PythonExe {
    <#
    .SYNOPSIS
        在常见位置找 Python X.Y. 顺序: py launcher > 常见安装目录 > 注册表.
        都找不到返回 $null.
    #>
    param([string]$MajorMinor)

    # 1. py launcher (Win 上 Python 装好默认会注册)
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $exe = (& py "-$MajorMinor" -c "import sys; print(sys.executable)" 2>$null)
            if ($LASTEXITCODE -eq 0 -and $exe) {
                $exe = $exe.ToString().Trim()
                if ($exe -and (Test-Path $exe)) { return $exe }
            }
        } catch {}
    }

    # 2. 常见安装位置
    $tag = $MajorMinor.Replace('.', '')   # 3.13 → 313
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python$tag\python.exe",
        "C:\Python$tag\python.exe",
        "C:\Program Files\Python$tag\python.exe",
        "C:\Program Files (x86)\Python$tag\python.exe"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $p }
    }

    # 3. 注册表 — Python 官方 installer 写 PythonCore\X.Y\InstallPath
    foreach ($hive in @("HKLM:", "HKCU:")) {
        $regKey = "$hive\SOFTWARE\Python\PythonCore\$MajorMinor\InstallPath"
        if (Test-Path $regKey) {
            $installDir = (Get-ItemProperty $regKey -ErrorAction SilentlyContinue).'(default)'
            if ($installDir -and (Test-Path "$installDir\python.exe")) {
                return "$installDir\python.exe"
            }
        }
    }

    return $null
}


function Get-DeployContext {
    <#
    .SYNOPSIS
        返回标准化的部署上下文 hashtable, 把"用户已在 .env.production 配的"和
        "需要自动推断的"路径合并起来. 所有部署脚本都用这个 + Override 自己的
        param 默认值, 实现"一处编辑, 全脚本生效".

    .PARAMETER RepoRoot
        vnpy_strategy_dev 仓库根. 默认推: 调用脚本上溯一级
        (deploy\<script>.ps1 → deploy\.. → 仓库根).

    .PARAMETER EnvFile
        .env 文件路径. 默认 $RepoRoot\.env.production.

    .OUTPUTS
        Hashtable with keys:
            RepoRoot, EnvFile, EnvVars (raw .env hashtable)
            VnpyPython, InferencePython
            QsDataRoot, MlOutputRoot, VnpyModelRoot
            LogRoot, BackupRoot
    #>
    param(
        [Parameter(Mandatory)] [string]$RepoRoot,
        [string]$EnvFile = ""
    )

    if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot ".env.production" }
    if (-not (Test-Path $EnvFile)) {
        # fallback 到 .env (开发期使用)
        $alt = Join-Path $RepoRoot ".env"
        if (Test-Path $alt) { $EnvFile = $alt }
    }
    $envVars = Read-EnvFile $EnvFile

    function _Get($key, $default) {
        $v = $envVars[$key]
        if ($v) { return $v }
        return $default
    }

    # Python 路径: .env 优先, 缺则自动检测; 仍缺则返回空 (调用方决定 install)
    $vnpyPy = _Get 'VNPY_PYTHON' ''
    if (-not $vnpyPy -or -not (Test-Path $vnpyPy)) {
        $detected = Find-PythonExe '3.13'
        if ($detected) { $vnpyPy = $detected }
    }
    $infPy = _Get 'INFERENCE_PYTHON' ''
    if (-not $infPy -or -not (Test-Path $infPy)) {
        $detected = Find-PythonExe '3.11'
        if ($detected) { $infPy = $detected }
    }

    return @{
        RepoRoot         = $RepoRoot
        EnvFile          = $EnvFile
        EnvVars          = $envVars
        VnpyPython       = $vnpyPy
        InferencePython  = $infPy
        QsDataRoot       = _Get 'QS_DATA_ROOT'    'D:\vnpy_data'
        MlOutputRoot     = _Get 'ML_OUTPUT_ROOT'  'D:\ml_output'
        VnpyModelRoot    = _Get 'VNPY_MODEL_ROOT' 'D:\vnpy_data\models'
        LogRoot          = _Get 'LOG_ROOT'        'D:\vnpy_logs'
        BackupRoot       = _Get 'BACKUP_ROOT'     'D:\backups'
    }
}


function Resolve-RepoRootFromScript {
    <#
    .SYNOPSIS
        从调用脚本位置推 vnpy_strategy_dev 仓库根. 假设脚本在 deploy/ 子目录.

    .EXAMPLE
        $RepoRoot = Resolve-RepoRootFromScript $PSScriptRoot
        # 若 $PSScriptRoot = F:\...\vnpy_strategy_dev\deploy
        # 返回 F:\...\vnpy_strategy_dev
    #>
    param([string]$ScriptRoot)
    return Split-Path -Parent $ScriptRoot
}
