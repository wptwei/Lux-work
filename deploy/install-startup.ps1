#Requires -Version 5.1
<#
.SYNOPSIS
    把 service.ps1 放进当前用户的“启动”文件夹：登录后自动运行 LUX 服务。
    不经过计划任务，适合计划任务启动会被安全策略拒绝（2147942405）的电脑。

.DESCRIPTION
    只会为当前用户创建快捷方式，不需要管理员权限。
    服务已由部署脚本启动时，登录自启会检测到端口占用并直接退出，不会起第二个实例。

.EXAMPLE
    .\install-startup.ps1
    .\install-startup.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$ShortcutName = 'LUX Agent',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup ($ShortcutName + '.lnk')

if ($Uninstall) {
    if (Test-Path -LiteralPath $shortcutPath) {
        Remove-Item -LiteralPath $shortcutPath -Force
        Write-LuxLog "已删除登录自启：$shortcutPath"
    }
    else {
        Write-LuxLog "登录自启不存在：$shortcutPath" 'WARN'
    }
    exit 0
}

$servicePath = Join-Path $config.RepoPath 'service.ps1'
if (-not (Test-Path -LiteralPath $servicePath)) {
    throw "找不到 $servicePath"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $servicePath
$shortcut.WorkingDirectory = $config.RepoPath
$shortcut.WindowStyle = 7
$shortcut.Description = 'LUX 本地 Agent 服务（登录后自动启动）'
$shortcut.Save()
Write-LuxLog "已设置登录自启：$shortcutPath"
Write-LuxLog "  目标：powershell.exe -File `"$servicePath`""
exit 0
