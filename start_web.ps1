#Requires -Version 5.1
<#
.SYNOPSIS
    旧入口兼容：日常执行 start_web.ps1 也进入受控启动，不再直接起第二份。

.DESCRIPTION
    默认（不带参数）＝ .\lux.ps1 start：受控后台启动 + 健康检查。
    -Foreground：前台调试运行 app.py（仍会先做实例检查，已在运行则拒绝）。
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [switch]$Foreground,
    [int]$TimeoutSec = 0
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'deploy\LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

if ($Foreground) {
    $env:PYTHONIOENCODING = 'utf-8'
    $code = Invoke-LuxServiceForeground
    exit $code
}

# 默认：进入受控启动，与 .\lux.ps1 start 完全一致。
Start-LuxService
if (Wait-LuxHealthy -TimeoutSec $TimeoutSec) {
    Write-LuxLog "服务已启动，健康检查通过"
    exit 0
}
Write-LuxLog "服务已启动但健康检查未通过，请查看 $($config.LogDir)" 'ERROR'
exit 1
