<#
.SYNOPSIS
    Copy legacy vnpy runtime data into the VNPY_DATA_ROOT layout.

.DESCRIPTION
    Dry-run by default. Use -Execute to copy files. The script copies and
    verifies, but never deletes legacy files.

    Target layout:
      <VNPY_DATA_ROOT>\state\replay_history.db
      <VNPY_DATA_ROOT>\state\event_journal.db
      <VNPY_DATA_ROOT>\state\sim_<gateway>.db
      <VNPY_DATA_ROOT>\ml_output\
      <VNPY_DATA_ROOT>\snapshots\
      <VNPY_DATA_ROOT>\models\
      <VNPY_DATA_ROOT>\logs\
      <VNPY_DATA_ROOT>\backups\
#>
param(
    [string]$VnpyRoot = "",
    [string]$DataRoot = "",
    [string]$PythonExe = "",
    [switch]$Execute,
    [string[]]$ExtraLegacyPaths = @()
)

$ErrorActionPreference = "Stop"

if (-not $VnpyRoot) { $VnpyRoot = Split-Path -Parent $PSScriptRoot }
if (-not $DataRoot) { $DataRoot = $env:VNPY_DATA_ROOT }
if (-not $DataRoot) { $DataRoot = "D:\vnpy_data" }
if (-not $PythonExe) {
    if (Test-Path "F:\Program_Home\vnpy\python.exe") { $PythonExe = "F:\Program_Home\vnpy\python.exe" }
    else { $PythonExe = "python" }
}

$DataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$stateRoot = Join-Path $DataRoot "state"
$reportRoot = Join-Path $DataRoot "backups"
New-Item -ItemType Directory -Path $reportRoot -Force | Out-Null

function Add-Candidate([System.Collections.ArrayList]$items, [string]$name, [string]$source, [string]$target, [string]$kind) {
    if (-not $source) { return }
    $items.Add([ordered]@{ Name=$name; Source=$source; Target=$target; Kind=$kind }) | Out-Null
}

function Same-Path([string]$a, [string]$b) {
    try { return [System.IO.Path]::GetFullPath($a).TrimEnd('\') -ieq [System.IO.Path]::GetFullPath($b).TrimEnd('\') }
    catch { return $false }
}

function Get-SqliteQuickCheck([string]$dbPath) {
    if (-not (Test-Path $dbPath)) { return "missing" }
    try {
        $code = "import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); print(con.execute('PRAGMA quick_check').fetchone()[0]); con.close()"
        $out = & $PythonExe -c $code $dbPath 2>$null
        if ($LASTEXITCODE -ne 0) { return "error" }
        return ($out | Select-Object -First 1)
    } catch {
        return "error: $($_.Exception.Message)"
    }
}

function Get-SqliteTableCounts([string]$dbPath) {
    if (-not (Test-Path $dbPath)) { return $null }
    try {
        $code = @'
import json
import sqlite3
import sys
con = sqlite3.connect(sys.argv[1])
tables = [
    row[0]
    for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
]
counts = {}
for table in tables:
    quoted = '"' + table.replace('"', '""') + '"'
    counts[table] = con.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
print(json.dumps(counts, ensure_ascii=False))
con.close()
'@
        $out = & $PythonExe -c $code $dbPath 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($out | Select-Object -First 1 | ConvertFrom-Json)
    } catch {
        return $null
    }
}

$candidates = [System.Collections.ArrayList]::new()
Add-Candidate $candidates "legacy_state_dir" "D:\vnpy_data\state" $stateRoot "directory_merge"
Add-Candidate $candidates "repo_trading_state" (Join-Path $VnpyRoot "vnpy_qmt_sim\.trading_state") $stateRoot "directory_merge"
Add-Candidate $candidates "legacy_ml_output" "D:\ml_output" (Join-Path $DataRoot "ml_output") "directory"
Add-Candidate $candidates "repo_ml_output" (Join-Path $VnpyRoot "ml_output") (Join-Path $DataRoot "ml_output") "directory"
Add-Candidate $candidates "repo_replay_history" (Join-Path $VnpyRoot "replay_history.db") (Join-Path $stateRoot "replay_history.db") "file"
Add-Candidate $candidates "qmt_sim_replay_history" (Join-Path $VnpyRoot "vnpy_qmt_sim\replay_history.db") (Join-Path $stateRoot "replay_history.db") "file"

foreach ($extra in $ExtraLegacyPaths) {
    $leaf = Split-Path -Leaf $extra
    $target = if ((Test-Path $extra -PathType Container) -or $extra.EndsWith('\')) { Join-Path $DataRoot $leaf } else { Join-Path $stateRoot $leaf }
    Add-Candidate $candidates "extra_$leaf" $extra $target "auto"
}

Write-Host "[migrate] VNPY_DATA_ROOT = $DataRoot" -ForegroundColor Cyan
Write-Host "[migrate] mode = $(if ($Execute) { 'execute' } else { 'dry-run' })"

$report = @()
foreach ($c in $candidates) {
    $source = [string]$c.Source
    $target = [string]$c.Target
    $status = "missing"
    $quickCheck = ""
    $tableCounts = $null
    $bytes = 0
    $mtime = $null

    if (Test-Path $source) {
        if (Same-Path $source $target) {
            $status = "same_path_skipped"
        } else {
            $status = if ($Execute) { "copied" } else { "would_copy" }
            if ($Execute) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                if ((Test-Path $source -PathType Container) -or $c.Kind -like "directory*") {
                    New-Item -ItemType Directory -Path $target -Force | Out-Null
                    Copy-Item -Path (Join-Path $source "*") -Destination $target -Recurse -Force -ErrorAction SilentlyContinue
                } else {
                    Copy-Item -Path $source -Destination $target -Force
                }
            }
        }
        if (Test-Path $source -PathType Leaf) {
            $sourceItem = Get-Item $source
            $bytes = $sourceItem.Length
            $mtime = $sourceItem.LastWriteTime.ToString("s")
        }
        if ($source.EndsWith(".db")) {
            $checkPath = if ($Execute -and (Test-Path $target)) { $target } else { $source }
            $quickCheck = Get-SqliteQuickCheck $checkPath
            $tableCounts = Get-SqliteTableCounts $checkPath
        }
    }

    Write-Host ("[migrate] {0,-24} {1} -> {2} [{3}]" -f $c.Name, $source, $target, $status)
    $report += [ordered]@{
        name = $c.Name
        source = $source
        target = $target
        kind = $c.Kind
        status = $status
        bytes = $bytes
        mtime = $mtime
        sqlite_quick_check = $quickCheck
        sqlite_table_counts = $tableCounts
    }
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$reportPath = Join-Path $reportRoot "migrate_vnpy_data_root_$stamp.json"
$report | ConvertTo-Json -Depth 5 | Set-Content -Path $reportPath -Encoding UTF8
Write-Host "[migrate] report: $reportPath" -ForegroundColor Green
Write-Host "[migrate] legacy files are not deleted. Verify services before manual cleanup." -ForegroundColor Yellow
