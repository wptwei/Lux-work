#Requires -Version 5.1
<#
.SYNOPSIS
    把 service.ps1 注册为 Windows 计划任务，由任务托管 LUX 服务。

.DESCRIPTION
    默认注册为“当前用户登录后启动”（不需要管理员、不需要输入密码）。
    任务已存在时必须加 -Force 才会改为指向本仓库的 service.ps1。
    需要“不管是否登录都运行”时用 -RunWhetherLoggedOn（会提示输入该账号密码，
    计划任务会安全保存；不能用 S4U，因为该服务需要联网搜索的外网访问）。

.EXAMPLE
    .\install-task.ps1 -TaskName 'LUX Web'
    .\install-task.ps1 -TaskName 'LUX-H区安保助手' -Force     # 把旧任务改为指向本仓库
    .\install-task.ps1 -TaskName 'LUX Web' -RunWhetherLoggedOn
    .\install-task.ps1 -TaskName 'LUX Web' -Uninstall
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$TaskName,
    [switch]$RunWhetherLoggedOn,
    [System.Management.Automation.PSCredential]$Credential,
    [switch]$Force,
    [switch]$Uninstall,
    [switch]$StartNow
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

if (-not $TaskName) { $TaskName = $config.TaskName }
if (-not $TaskName) { $TaskName = 'LUX Web' }
if ($TaskName -match '[^\x00-\x7F]') {
    # 部分电脑（含本机）会因任务名含非 ASCII 字符而启动失败，错误 2147942405。
    Write-LuxLog "任务名「$TaskName」含非 ASCII 字符，部分电脑上会无法启动（2147942405）；建议改用纯英文名，例如 LUX-H-Agent" 'WARN'
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Uninstall) {
    if (-not $existing) {
        Write-LuxLog "任务不存在：$TaskName" 'WARN'
        exit 0
    }
    if ($existing.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
        Write-LuxLog "已停止任务：$TaskName"
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-LuxLog "已删除计划任务：$TaskName（服务如仍在运行请自行停止）"
    exit 0
}

$servicePath = Join-Path $config.RepoPath 'service.ps1'
if (-not (Test-Path -LiteralPath $servicePath)) {
    throw "找不到 $servicePath"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $servicePath) `
    -WorkingDirectory $config.RepoPath

$userId = "$env:USERDOMAIN\$env:USERNAME"
$triggers = @(New-ScheduledTaskTrigger -AtLogOn -User $userId)
if ($RunWhetherLoggedOn) { $triggers += New-ScheduledTaskTrigger -AtStartup }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

function Register-LuxTask {
    param([System.Management.Automation.PSCredential]$RunCredential)
    if ($RunWhetherLoggedOn) {
        if (-not $RunCredential) {
            $RunCredential = Get-Credential -UserName $userId `
                -Message '输入运行 LUX 服务的 Windows 账号密码（计划任务会安全保存）'
        }
        if (-not $RunCredential) { throw '未提供账号凭据，已取消。' }
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
            -Settings $settings -User $RunCredential.UserName `
            -Password $RunCredential.GetNetworkCredential().Password `
            -RunLevel Limited -Description 'LUX 本地 Agent 服务（deploy\install-task.ps1 注册）' `
            -Force | Out-Null
        Write-LuxLog "已注册计划任务「$TaskName」：开机/登录即运行（不论是否登录）"
    }
    else {
        $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
            -Settings $settings -Principal $principal `
            -Description 'LUX 本地 Agent 服务（deploy\install-task.ps1 注册）' -Force | Out-Null
        Write-LuxLog "已注册计划任务「$TaskName」：登录后自动启动（用户 $userId）"
    }
}

if ($existing) {
    if (-not $Force) {
        throw "计划任务「$TaskName」已存在。加 -Force 可把它的启动目标改为本仓库的 service.ps1。"
    }
    $oldExec = $existing.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }
    # 只改 Action 会保留旧任务（可能来自别的机器/别的账号）的权限与运行条件，
    # 曾出现“任务无法启动，错误值 2147942405（拒绝访问）”；所以整体重新注册。
    Register-LuxTask -RunCredential $Credential
    Write-LuxLog "已更新计划任务「$TaskName」（整体重新注册）"
    Write-LuxLog "  原目标：$oldExec"
    Write-LuxLog "  新目标：powershell.exe -File `"$servicePath`""
}
else {
    Register-LuxTask -RunCredential $Credential
}

Write-LuxLog "任务状态：$((Get-ScheduledTask -TaskName $TaskName).State)"
if ($StartNow) {
    if (Test-LuxHttp) {
        Write-LuxLog "端口 $($config.BaseUrl) 已有服务在响应，未启动任务，避免起第二个实例" 'WARN'
    }
    else {
        Start-ScheduledTask -TaskName $TaskName
        Write-LuxLog "已启动任务；稍后用 .\lux-health.ps1 检查"
    }
}
else {
    Write-LuxLog "尚未启动任务。请先停掉手工运行的服务，再执行：Start-ScheduledTask -TaskName '$TaskName'"
}
exit 0
