# P1-6: 推理端日常数据备份
#
# 备份范围 (按重要度由高到低):
#   1. mlearnweb.db          — 训练记录 + 部署元数据 + 历史快照 (跨工程, 在监控端)
#                              ⚠️ 跨机部署时由监控端独立备份, 推理端跳过
#   2. replay_history.db     — vnpy 端本地回放权益历史 (A1/B2)
#   3. sim_<gateway>.db × N  — 模拟柜台状态 (持仓 / 资金 / 订单 / 成交)
#   4. .vntrader/database.db — vnpy bar 数据库 (历史 K 线)
#   5. .vntrader/vt_setting.json — tushare token / miniqmt 路径 / email 凭据
#   6. bundle 元数据         — params.pkl 太大跳过, 训练机有, 仅备 manifest/filter_config/task json
#   7. .env.production       — P0-1 凭证文件 (含 tushare token / miniqmt 资金账号)
#
# 不备份: qlib_data_bin (可由训练机重生), ml_output (可重跑 21:00 cron),
#         daily_merged 快照 (tushare 20:00 cron 可重拉)
#
# 用法:
#   # 任务计划程序 02:00 触发:
#   schtasks /create /tn "vnpy_daily_backup" /tr "powershell -File F:\Quant\vnpy\vnpy_strategy_dev\deploy\daily_backup.ps1" /sc daily /st 02:00 /ru SYSTEM
#
#   # 手动跑一次:
#   .\deploy\daily_backup.ps1
#
#   # 自定义路径:
#   .\deploy\daily_backup.ps1 -BackupRoot "E:\backups" -Retention 14
#
# 依赖: 7zip 可执行文件 (默认在 PATH 找 7z; 没装则降级 zip)

param(
    [string]$VnpyRoot = "F:\Quant\vnpy\vnpy_strategy_dev",
    [string]$BackupRoot = "D:\backups",
    [int]$Retention = 30,                            # 保留 30 天
    [string]$VntraderRoot = "$env:USERPROFILE\.vntrader",
    [string]$VnpyDataRoot = "D:\vnpy_data",
    [string]$ModelsRoot = "D:\vnpy_data\models",
    [switch]$IncludeMlearnweb,                       # 默认 false (mlearnweb 在监控端独立备)
    [string]$MlearnwebDb = "F:\Quant\code\qlib_strategy_dev\mlearnweb\backend\mlearnweb.db"
)

$ErrorActionPreference = "Stop"
$today = Get-Date -Format "yyyyMMdd"
$staging = Join-Path $BackupRoot "staging_$today"
$archive = Join-Path $BackupRoot "vnpy_daily_$today.7z"

Write-Host "[backup] start $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "[backup] staging dir: $staging"
Write-Host "[backup] archive:     $archive"

if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging -Force | Out-Null

# 1. replay_history.db (vnpy 端本地)
$replayDb = Join-Path $VnpyDataRoot "state\replay_history.db"
if (Test-Path $replayDb) {
    Copy-Item $replayDb $staging -ErrorAction SilentlyContinue
    Write-Host "[backup]   ✓ replay_history.db" -ForegroundColor Green
} else {
    Write-Host "[backup]   - replay_history.db 不存在 (尚未跑过回放?)" -ForegroundColor Yellow
}

# 2. sim_<gateway>.db × N (模拟柜台状态; .lock 跳过)
# [A2] 状态文件统一到 ${QS_DATA_ROOT}/state/, 与 replay_history.db 同级.
$simStateDir = Join-Path $VnpyDataRoot "state"
if (Test-Path $simStateDir) {
    $simDbs = Get-ChildItem $simStateDir -Filter "sim_*.db"
    $simDbs | Copy-Item -Destination $staging -ErrorAction SilentlyContinue
    Write-Host "[backup]   ✓ sim_*.db × $($simDbs.Count)" -ForegroundColor Green
}

# 3. vntrader 框架数据库 + vt_setting.json
if (Test-Path "$VntraderRoot\database.db") {
    Copy-Item "$VntraderRoot\database.db" $staging -ErrorAction SilentlyContinue
    Write-Host "[backup]   ✓ vntrader database.db" -ForegroundColor Green
}
if (Test-Path "$VntraderRoot\vt_setting.json") {
    Copy-Item "$VntraderRoot\vt_setting.json" $staging -ErrorAction SilentlyContinue
    Write-Host "[backup]   ✓ vt_setting.json" -ForegroundColor Green
}

# 4. bundle 元数据 (跳 params.pkl, 太大且训练机有)
$bundleMetaDir = Join-Path $staging "bundles_meta"
New-Item -ItemType Directory -Path $bundleMetaDir -Force | Out-Null
if (Test-Path $ModelsRoot) {
    Get-ChildItem $ModelsRoot -Directory | ForEach-Object {
        $bundle = $_.FullName
        $runId = $_.Name
        $dest = Join-Path $bundleMetaDir $runId
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
        foreach ($f in @("manifest.json", "filter_config.json", "task.json")) {
            $src = Join-Path $bundle $f
            if (Test-Path $src) { Copy-Item $src $dest }
        }
    }
    $cnt = (Get-ChildItem $bundleMetaDir -Directory).Count
    Write-Host "[backup]   ✓ bundle 元数据 × $cnt" -ForegroundColor Green
}

# 5. .env.production (凭证)
$envFile = Join-Path $VnpyRoot ".env.production"
if (Test-Path $envFile) {
    Copy-Item $envFile $staging -ErrorAction SilentlyContinue
    Write-Host "[backup]   ✓ .env.production" -ForegroundColor Green
}
$yamlFile = Join-Path $VnpyRoot "config\strategies.production.yaml"
if (Test-Path $yamlFile) {
    Copy-Item $yamlFile $staging -ErrorAction SilentlyContinue
    Write-Host "[backup]   ✓ strategies.production.yaml" -ForegroundColor Green
}

# 6. 可选: mlearnweb.db (跨机部署时关闭)
if ($IncludeMlearnweb -and (Test-Path $MlearnwebDb)) {
    Copy-Item $MlearnwebDb $staging -ErrorAction SilentlyContinue
    Write-Host "[backup]   ✓ mlearnweb.db" -ForegroundColor Green
}

# 7. 压缩 (优先 7z, fallback zip)
$sevenZip = Get-Command 7z -ErrorAction SilentlyContinue
if ($sevenZip) {
    Write-Host "[backup] 压缩 → $archive (7zip max)" -ForegroundColor Cyan
    & 7z a -t7z -mx9 -bso0 -bsp0 $archive "$staging\*" | Out-Host
} else {
    $archive = $archive -replace "\.7z$", ".zip"
    Write-Host "[backup] 压缩 → $archive (zip, 7zip 未装)" -ForegroundColor Yellow
    Compress-Archive -Path "$staging\*" -DestinationPath $archive -Force
}

# 8. 清理 staging
Remove-Item $staging -Recurse -Force

# 9. retention: 删除 N 天前的归档
$cutoff = (Get-Date).AddDays(-$Retention)
$old = Get-ChildItem $BackupRoot -ErrorAction SilentlyContinue |
       Where-Object { $_.Name -like "vnpy_daily_*.7z" -or $_.Name -like "vnpy_daily_*.zip" } |
       Where-Object { $_.LastWriteTime -lt $cutoff }
if ($old) {
    $old | Remove-Item -Force
    Write-Host "[backup] 清理 $($old.Count) 个 > $Retention 天的旧归档" -ForegroundColor Yellow
}

# 10. 最终大小报告
$sizeMb = [math]::Round((Get-Item $archive).Length / 1MB, 2)
$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host "[backup] done $ts   archive size = $sizeMb MB" -ForegroundColor Green

# 11. (可选) 异地: rclone copy $archive my-s3:vnpy-backup/ ; aws s3 cp ...
#     不在本脚本写死, 部署机自行加. 当前仅本地保留 30 天.
