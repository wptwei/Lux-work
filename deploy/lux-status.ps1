#Requires -Version 5.1
<#
.SYNOPSIS
    发布/运行现状：当前版本、工作区、服务、健康、迁移、备份、上次发布结果。
#>
[CmdletBinding()]
param([string]$ConfigPath)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

Write-Host "== 版本 =="
$revision = Get-LuxRevision -AllowFailure
$branch = (Invoke-LuxGit @('rev-parse', '--abbrev-ref', 'HEAD') -AllowFailure).Output
Write-Host ("当前：{0}（{1}）" -f $(if ($revision.describe) { $revision.describe } else { '未知' }), $(if ($revision.commit) { $revision.commit } else { '未知' }))
Write-Host ("分支/HEAD：{0}" -f $(if ($branch) { "$branch".Trim() } else { '未知' }))
$clean = Test-LuxRepoClean -AllowFailure
if ($null -eq $clean) { Write-Host "工作区：未知（Git 不可读）" -ForegroundColor Yellow }
elseif ($clean) { Write-Host "工作区：干净" }
else { Write-Host "工作区：有未提交改动" -ForegroundColor Yellow }
$tags = @((Invoke-LuxGit @('tag', '--sort=-creatordate') -AllowFailure).Output) | Select-Object -First 5
Write-Host ("最近 tag：{0}" -f ($(if ($tags) { $tags -join '、' } else { '无' })))

Write-Host "`n== 服务 =="
$state = Get-LuxServiceState
Write-Host ("进程：{0}" -f $state.detail)
Write-Host ("状态：{0}" -f $state.status)
if (Test-LuxHttp) {
    Write-Host ("Web：{0} 可访问" -f $config.BaseUrl)
    $owner = Get-LuxPortOwner
    if ($owner) { Write-Host ("端口占用：PID {0}（{1}）" -f $owner.pid, $owner.command) }
}
else {
    Write-Host ("Web：{0} 无法访问" -f $config.BaseUrl) -ForegroundColor Yellow
}
Write-Host ("托管模式：{0}" -f $config.ServiceMode)
if (Test-LuxShareEnabled) {
    $shareUrl = Get-LuxShareUrl
    Write-Host ("公网分享：已开启 {0}" -f $(if ($shareUrl) { $shareUrl } else { '（未找到 URL）' }))
}
else {
    Write-Host "公网分享：未开启"
}

Write-Host "`n== 目录 =="
$paths = Invoke-LuxPythonCapture @('-c', 'import json;from core.paths import describe;print(json.dumps(describe(),ensure_ascii=False,indent=2))') 'paths'
$paths.Output | ForEach-Object { Write-Host "$_" }

Write-Host "`n== 数据库迁移 =="
$migrations = Invoke-LuxPythonCapture @('tools/migrate.py', 'status') 'migrate status'
$migrations.Output | ForEach-Object { Write-Host "$_" }

Write-Host "`n== 备份 =="
$backups = Invoke-LuxPythonCapture @('tools/backup_data.py', 'list', '--out', $config.BackupDir) 'backup list'
$lines = @($backups.Output)
if ($lines.Count -gt 0) { $lines | Select-Object -First 5 | ForEach-Object { Write-Host "$_" } }
else { Write-Host "暂无备份" }

Write-Host "`n== 上次发布 =="
$last = Get-LuxState
if ($last) {
    Write-Host ("{0} → {1}；结果：{2}；时间：{3}" -f $last.release, $last.commit, $last.result, $last.finished)
    if ($last.backup) { Write-Host ("备份：{0}" -f $last.backup) }
    if ($last.detail) { Write-Host ("详情：{0}" -f $last.detail) }
}
else {
    Write-Host "本机还没有部署记录（脚本首次发布后生成）"
}
exit 0
