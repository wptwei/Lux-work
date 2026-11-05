#Requires -Version 5.1
<#
.SYNOPSIS
    健康检查：服务进程、Web 端口、依赖/模型/路径。退出码 0 表示正常。
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [int]$TimeoutSec = 0
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

$state = Get-LuxServiceState
if ($state.running) {
    Write-LuxLog "服务状态：$($state.detail)"
}
else {
    $owner = Get-LuxPortOwner
    if (-not $owner) {
        Write-LuxLog "服务未运行，端口 $($config.BaseUrl) 无响应" 'ERROR'
        exit 1
    }
    Write-LuxLog "服务不是由部署脚本托管（PID $($owner.pid)：$($owner.command)），只做可用性检查" 'WARN'
}

if (Test-LuxHttp) {
    Write-LuxLog "Web 端口正常：$($config.BaseUrl)"
}
else {
    Write-LuxLog "无法访问 $($config.BaseUrl)" 'ERROR'
    exit 1
}

$check = Invoke-LuxPythonCapture @('healthcheck.py') 'healthcheck.py'
$check.Output | ForEach-Object { Write-Host "$_" }
if ($check.Code -ne 0) {
    Write-LuxLog "健康检查未通过（详见上面的 [FAIL] 项）" 'ERROR'
    exit 1
}
Write-LuxLog "健康检查通过"
exit 0
