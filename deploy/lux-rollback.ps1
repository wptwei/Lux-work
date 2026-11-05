#Requires -Version 5.1
<#
.SYNOPSIS
    版本回退：切回指定版本；如需连同数据一起回退，用 -RestoreData 指定备份。

.DESCRIPTION
    不做“向下迁移”——数据只在确认需要时从备份整库恢复。回退前会为当前状态再做一次备份。

.EXAMPLE
    .\lux-rollback.ps1 -List
    .\lux-rollback.ps1 -To v1.2.9
    .\lux-rollback.ps1 -To v1.2.9 -RestoreData .\backups\lux-backup-20261031-090000.zip
#>
[CmdletBinding()]
param(
    [string]$To,
    [string]$ConfigPath,
    [switch]$List,
    [string]$RestoreData,
    [switch]$SkipBackup,
    [switch]$NoHealthCheck,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

if ($List) {
    $state = Get-LuxState
    if ($state) {
        Write-Host ("上次发布：{0} → {1}（{2}）" -f $state.release, $state.commit, $state.result)
        Write-Host ("时间：{0}；备份：{1}" -f $state.finished, $state.backup)
    }
    Write-Host "`n可用备份："
    $listing = Invoke-LuxPythonCapture @('tools/backup_data.py', 'list', '--out', $config.BackupDir) '列出备份'
    if ($listing.Code -eq 0) {
        $listing.Output | ForEach-Object { Write-Host "$_" }
    }
    Write-Host "`n可回退的版本："
    $tags = (Invoke-LuxGit @('tag', '--sort=-creatordate')).Output
    @($tags) | Select-Object -First 15 | ForEach-Object { Write-Host "  $_" }
    exit 0
}

if (-not $To) {
    throw "请用 -To 指定要回退到的 tag/commit，或用 -List 查看可选项。"
}

$current = Get-LuxRevision
$target = Resolve-LuxRef -Ref $To -AllowBranch:$true
Write-LuxLog "==== 回退开始：$($current.describe) → $To（$($target.commit)）===="

if ($DryRun) {
    Write-LuxLog "[DRY-RUN] 将：停服 → 备份当前数据 → 切到 $($target.commit) → $(if ($RestoreData) { "恢复数据 $RestoreData" } else { '保留当前数据' }) → 起服 → 健康检查"
    exit 0
}

$backupPath = $null
try {
    Stop-LuxService
    if (-not (Assert-LuxServiceStopped)) {
        throw "停止后端口仍在响应，服务不归本脚本托管；请配置 ScheduledTask 或手动停止后再回退。"
    }
    if (-not $SkipBackup) {
        $backupPath = New-LuxBackup -Note "pre-rollback from $($current.describe)"
    }
    Invoke-LuxGit @('checkout', '--force', $target.commit) | Out-Null
    Write-LuxLog "代码已切到 $($target.commit)"

    if ($RestoreData) {
        Restore-LuxData -Archive $RestoreData
    }

    $status = Invoke-LuxPythonCapture @('tools/migrate.py', 'status', '--json') '迁移状态检查'
    if ($status.Code -ne 0) { throw "迁移状态检查失败：$($status.Output -join ' ')" }

    Start-LuxService
    if (-not $NoHealthCheck -and -not (Wait-LuxHealthy)) {
        throw "回退后健康检查未通过；回退前备份：$backupPath"
    }
}
catch {
    Write-LuxLog "回退失败：$_" 'ERROR'
    $state = @{
        release = "rollback:$To"; commit = $target.commit; previous_commit = $current.commit
        started = (Get-Date).ToString('o'); finished = (Get-Date).ToString('o')
        result = 'rollback_failed'; backup = "$backupPath"; migrations_applied = @()
        detail = "$_"
    }
    Save-LuxState -State $state
    throw
}

$state = @{
    release = "rollback:$To"; commit = $target.commit; previous_commit = $current.commit
    started = (Get-Date).ToString('o'); finished = (Get-Date).ToString('o')
    result = 'rolled_back'; backup = "$backupPath"; migrations_applied = @()
    detail = ''
}
Save-LuxState -State $state
Write-LuxLog "==== 回退完成：$To ===="
exit 0
