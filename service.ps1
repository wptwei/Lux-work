# 登录自启 / 计划任务入口：统一走 LuxDeploy.psm1 的生命周期，避免第二份实例。
# Pid 模式：受控后台启动（幂等）；ScheduledTask 模式：前台运行，保持任务 Running。

$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot 'deploy\LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment

$logDir = $config.LogDir
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$log = Join-Path $logDir "service.log"

function Write-ServiceLog([string]$message) {
    "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message |
        Out-File -FilePath $log -Append -Encoding UTF8
}

if ($config.ServiceMode -eq 'Pid') {
    Write-ServiceLog "service start (pid=$PID, mode=Pid)"
    try {
        Start-LuxService
        $state = Get-LuxServiceState
        Write-ServiceLog "service done: $($state.status) ($($state.detail))"
        exit 0
    }
    catch {
        Write-ServiceLog "service error: $_"
        exit 1
    }
}

# ScheduledTask 模式：前台运行，输出写入 service.log。
Write-ServiceLog "service start (pid=$PID, mode=ScheduledTask)"
try {
    Invoke-LuxServiceForeground 2>&1 | Out-File -FilePath $log -Append -Encoding UTF8
    Write-ServiceLog "service foreground returned, exit=$LASTEXITCODE"
    exit $LASTEXITCODE
}
catch {
    Write-ServiceLog "service error: $_"
    exit 1
}
