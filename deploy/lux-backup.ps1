#Requires -Version 5.1
<#
.SYNOPSIS
    立即备份业务数据与运行配置；也可列出或恢复备份。

.EXAMPLE
    .\lux-backup.ps1
    .\lux-backup.ps1 -List
    .\lux-backup.ps1 -Restore .\backups\lux-backup-20261031-090000.zip -Force
#>
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [switch]$List,
    [string]$Restore,
    [switch]$Force,
    [switch]$IncludeSecrets,
    [string]$Note = ''
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

if ($List) {
    $listing = Invoke-LuxPythonCapture @('tools/backup_data.py', 'list', '--out', $config.BackupDir) '列出备份'
    $listing.Output | ForEach-Object { Write-Host "$_" }
    exit $listing.Code
}

if ($Restore) {
    $arguments = @('tools/backup_data.py', 'restore', $Restore)
    if ($Force) { $arguments += '--force' }
    $result = Invoke-LuxPythonCapture $arguments '恢复备份'
    $result.Output | ForEach-Object { Write-Host "$_" }
    exit $result.Code
}

$path = New-LuxBackup -Note $Note -IncludeSecrets:($IncludeSecrets -or [bool]$config.IncludeSecretsInBackup)
Write-Host "备份文件：$path"
exit 0
