# [P2-2] 一次性历史数据回填 — 推理服务器首次部署 / 灾备恢复用
#
# bootstrap.ps1 -Apply 完成后会提示:
#   "下一步 (人工): 第一次手动跑 daily_ingest 拉历史数据 (deployment.md Step 7)"
# 本脚本就是"一行替代手工命令" — 自动从 .env.production 取 VnpyPython,
# spawn deploy/initial_ingest.py 跑.
#
# 用法:
#   .\deploy\initial_ingest.ps1                              # 拉今天 1 天
#   .\deploy\initial_ingest.ps1 -Date 20260505               # 拉指定 1 天
#   .\deploy\initial_ingest.ps1 -From 20260101 -To 20260505  # 范围回填
#   .\deploy\initial_ingest.ps1 -From 20260101 -To 20260505 -Force
#
# 先决条件 (bootstrap.ps1 -Apply 已搞定 + 用户手工补的部分):
#   - .env.production / strategies.production.yaml 已配
#   - bundle 已 rsync 到 ${VNPY_MODEL_ROOT}/<run_id>/, 含 filter_config.json
#   - vt_setting.json 的 datafeed.username / datafeed.password (tushare token) 已填
#   - vnpy 主 Python 已装 (脚本自动从 .env / 检测 / winget 装)

[CmdletBinding()]
param(
    [string]$Date,
    [string]$From,
    [string]$To,
    [switch]$Force,
    [string]$VnpyRoot = "",
    [string]$VnpyPython = ""
)

$ErrorActionPreference = "Stop"

# 共享 deploy helper
. (Join-Path $PSScriptRoot "_lib.ps1")

# 路径解析 — 与 bootstrap / install_services 同源, 用户改 .env 一处生效
if (-not $VnpyRoot)   { $VnpyRoot   = Resolve-RepoRootFromScript $PSScriptRoot }
$ctx = Get-DeployContext -RepoRoot $VnpyRoot
if (-not $VnpyPython) { $VnpyPython = $ctx.VnpyPython }

if (-not $VnpyPython -or -not (Test-Path $VnpyPython)) {
    Write-Error "VnpyPython 未找到; 在 .env.production 加 'VNPY_PYTHON=...' 或 -VnpyPython <path>"
    exit 1
}

$pyScript = Join-Path $PSScriptRoot "initial_ingest.py"
if (-not (Test-Path $pyScript)) {
    Write-Error "initial_ingest.py 不存在: $pyScript"
    exit 1
}

# 组装 Python args (互斥校验交给 Python 自己)
$pyArgs = @($pyScript)
if ($Date)  { $pyArgs += @("--date", $Date) }
if ($From)  { $pyArgs += @("--from", $From) }
if ($To)    { $pyArgs += @("--to",   $To) }
if ($Force) { $pyArgs += @("--force") }

Write-Host "[ingest] $VnpyPython $($pyArgs -join ' ')" -ForegroundColor Cyan
Write-Host ""

& $VnpyPython @pyArgs
$rc = $LASTEXITCODE
if ($rc -eq 0) {
    Write-Host ""
    Write-Host "[ingest] 成功. 后续验证:" -ForegroundColor Green
    $qlibBin = Join-Path $ctx.QsDataRoot "qlib_data_bin\calendars\day.txt"
    Write-Host "  Get-Content '$qlibBin' -Tail 3   # qlib calendar 末尾 3 天"
    Write-Host "  ls $($ctx.QsDataRoot)\snapshots\filtered\*_$(Get-Date -Format yyyyMMdd).parquet  # 当日 filter snapshot"
    Write-Host ""
    Write-Host "现在可以 nssm start vnpy_headless / 等 21:00 cron 自动推理." -ForegroundColor Green
    exit 0
} else {
    Write-Host ""
    Write-Error "[ingest] 失败 (rc=$rc) — 检查 vt_setting.json datafeed.password / network / disk."
    exit $rc
}
