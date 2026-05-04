# P1-7: NTP 时钟同步配置
#
# A 股 09:26 集合竞价时间窗严: 时钟漂移 30s 就可能错过最优挂单时机.
# Windows server 默认 NTP 在某些网段 (尤其内网) 不一定可靠, 主动指定大陆
# 公共 NTP 源, 并且把"自动同步"打开.
#
# 用法 (Administrator PowerShell):
#   .\deploy\configure_ntp.ps1
#
# 验证:
#   w32tm /query /status
#   # 看 "Source" 是否 = 配置的服务器, "Last Successful Sync Time" 在最近
#
# 选用 ntp.ntsc.ac.cn (国家授时中心) — 内网 / 公网均稳定. 备选: ntp.aliyun.com.

param(
    [string[]]$Peers = @(
        "ntp.ntsc.ac.cn,0x9",       # 国家授时中心 (优先)
        "ntp.aliyun.com,0x9",       # 阿里云 NTP (备)
        "time.windows.com,0x9"      # Windows 默认 (兜底)
    )
)

$ErrorActionPreference = "Stop"

# 必须 Administrator 才能改 w32time 配置
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "本脚本必须以 Administrator 身份运行 (右键 PowerShell → 以管理员身份运行)"
    exit 1
}

Write-Host "[ntp] 配置 NTP 服务器列表..." -ForegroundColor Cyan

# 1. 启动 w32time 服务 (Windows Server 2022 默认禁用)
Set-Service -Name w32time -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name w32time -ErrorAction SilentlyContinue

# 2. 配置 NTP peers
$peerList = $Peers -join " "
& w32tm /config /manualpeerlist:"$peerList" /syncfromflags:manual /reliable:yes /update | Out-Host

# 3. 立即同步一次
Write-Host "[ntp] 触发立即同步..." -ForegroundColor Cyan
& w32tm /resync /rediscover | Out-Host

Start-Sleep -Seconds 2

# 4. 显示当前状态
Write-Host ""
Write-Host "[ntp] 当前 w32tm 状态:" -ForegroundColor Green
& w32tm /query /status | Out-Host

Write-Host ""
Write-Host "[ntp] 配置完成. 建议加任务计划程序每日 02:00 跑一次 'w32tm /resync /rediscover'" -ForegroundColor Yellow
Write-Host "[ntp] 偏差监控: w32tm /stripchart /computer:ntp.ntsc.ac.cn /samples:5 /dataonly" -ForegroundColor Yellow
