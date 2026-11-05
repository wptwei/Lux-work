#Requires -Version 5.1
<#
.SYNOPSIS
    重启服务（不动代码、不跑迁移），然后做一次健康检查。
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [int]$TimeoutSec = 0
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

$revision = Get-LuxRevision
Write-LuxLog "重启服务（当前版本 $($revision.describe)）"

Stop-LuxService
Start-LuxService

if (Wait-LuxHealthy -TimeoutSec $TimeoutSec) {
    Write-LuxLog "重启完成，健康检查通过"
    exit 0
}
Write-LuxLog "重启后健康检查未通过，请查看 $($config.LogDir)" 'ERROR'
exit 1
