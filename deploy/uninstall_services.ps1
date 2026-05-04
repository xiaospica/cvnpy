#requires -RunAsAdministrator
<#
.SYNOPSIS
    卸载 install_services.ps1 装的所有 NSSM 服务.

.DESCRIPTION
    stop + remove 4 个服务. 不删日志 / 不删 .env / 不删数据.

.PARAMETER NssmPath
    nssm.exe 路径. 默认 'nssm' (假定已在 PATH).

.EXAMPLE
    PS C:\> .\deploy\uninstall_services.ps1
#>

[CmdletBinding()]
param(
    [string]$NssmPath = "nssm"
)

$nssmExe = Get-Command $NssmPath -ErrorAction SilentlyContinue
if (-not $nssmExe) {
    Write-Error "❌ nssm 未找到"
    exit 1
}

# 推理端只装一个服务 (mlearnweb 在另一项目, 不在这里管理)
$services = @("vnpy_headless")
foreach ($svc in $services) {
    & $nssmExe.Source status $svc 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "─── 卸载 $svc ───" -ForegroundColor Cyan
        & $nssmExe.Source stop $svc | Out-Null
        Start-Sleep -Seconds 2
        & $nssmExe.Source remove $svc confirm | Out-Null
        Write-Host "✓ $svc 已卸载"
    } else {
        Write-Host "ℹ️ $svc 不存在, 跳过"
    }
}

Write-Host ""
Write-Host "✓ 全部 NSSM 服务已卸载"
Write-Host "ℹ️ 日志 / 数据 / .env / config 等保留, 未删除"
