#Requires -Version 5.1
<#
.SYNOPSIS
    手动发布：备份 → 停服 → 切到指定 tag → 依赖/迁移 → 起服 → 健康检查，失败自动回退。

.DESCRIPTION
    代码只通过 Git 发布；业务数据、配置、密钥不在仓库内，切换版本不会动它们。
    发布前一定会备份（可用 -SkipBackup 关闭），数据库结构变化走 tools/migrate.py。

.EXAMPLE
    .\lux-deploy.ps1 v1.3.0
    .\lux-deploy.ps1 v1.3.0 -DryRun
    .\lux-deploy.ps1 v1.3.0 -SkipTests -NoRollback
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Ref,
    [string]$ConfigPath,
    [switch]$SkipBackup,
    [switch]$SkipTests,
    [switch]$NoRollback,
    [switch]$AllowDirty,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'LuxDeploy.psm1') -Force -DisableNameChecking
$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

$startedAt = (Get-Date).ToString('o')
Write-LuxLog "==== 发布开始：$Ref ===="
if (-not (Test-Path -LiteralPath (Join-Path $config.RepoPath '.git'))) {
    throw "不是 Git 仓库：$($config.RepoPath)"
}
if (-not (Test-LuxRepoClean) -and -not $AllowDirty) {
    throw "工作区有未提交改动；请先提交或还原，确认要用当前状态发布时加 -AllowDirty。"
}

$current = Get-LuxRevision
Write-LuxLog "当前版本：$($current.describe)（$($current.commit)）"
$target = Resolve-LuxRef -Ref $Ref -AllowBranch:([bool]$config.AllowBranchRefs)
Write-LuxLog "目标版本：$Ref → $($target.commit)$(if ($target.isTag) { '（tag）' } else { '（commit）' })"

$statusResult = Invoke-LuxPythonCapture @('tools/migrate.py', 'status', '--json') '迁移状态检查'
if ($statusResult.Code -ne 0) {
    throw "迁移状态检查失败：$($statusResult.Output -join ' ')"
}
$migrationStatus = ($statusResult.Output -join "`n") | ConvertFrom-Json
$pendingTotal = 0
foreach ($item in $migrationStatus) {
    $count = @($item.pending).Count
    if ($count -gt 0) { Write-LuxLog "[$($item.alias)] 待执行迁移：$($item.pending -join '、')" }
    if (@($item.drift).Count -gt 0) { Write-LuxLog "[$($item.alias)] 迁移文件校验和已变化：$($item.drift -join '、')" 'WARN' }
    $pendingTotal += $count
}

if ($target.commit -eq $current.commit -and $pendingTotal -eq 0) {
    Write-LuxLog "已是指定版本且没有待执行迁移，无需发布"
    exit 0
}

if ($DryRun) {
    Write-LuxLog "[DRY-RUN] 将执行：停止服务 → 备份 → 切换到 $($target.commit) → 按需安装依赖 → 应用迁移 → 启动服务 → 健康检查"
    Write-LuxLog "[DRY-RUN] 备份目录：$($config.BackupDir)；保留 $($config.BackupKeep) 份"
    Write-LuxLog "[DRY-RUN] 未做任何改动"
    exit 0
}

$backupPath = $null
$migrationsApplied = @()
$rolledBack = $false
$serviceStopped = $false

try {
    Stop-LuxService
    if (-not (Assert-LuxServiceStopped)) {
        throw "停止后端口仍在响应，服务不归本脚本托管；请配置 ScheduledTask 或手动停止后再发布。"
    }
    $serviceStopped = $true

    if (-not $SkipBackup) {
        $backupPath = New-LuxBackup -Note "deploy $Ref" -IncludeSecrets:([bool]$config.IncludeSecretsInBackup)
    }
    else {
        Write-LuxLog "已跳过备份（-SkipBackup）；发布失败时将无法自动恢复数据" 'WARN'
    }

    Invoke-LuxGit @('checkout', '--force', $target.commit) | Out-Null
    Write-LuxLog "代码已切换到 $($target.commit)"

    $changed = (Invoke-LuxGit @(
            'diff', '--name-only', $current.commit, $target.commit, '--',
            'requirements.txt', 'requirements-library.txt', 'requirements-voice.txt')).Output
    $changedText = "$changed".Trim()
    if ($changedText) {
        Write-LuxLog "依赖清单有变化：$changedText"
        if ("$changedText" -match 'requirements\.txt') {
            Invoke-LuxPython @('-m', 'pip', 'install', '-r', 'requirements.txt') '安装 Python 依赖' | Out-Null
        }
        if ("$changedText" -match 'requirements-(library|voice)\.txt') {
            Write-LuxLog "library/voice 依赖需按需运行 install_library.ps1 / install_voice.ps1" 'WARN'
        }
    }

    $migrate = Invoke-LuxPythonCapture @('tools/migrate.py', 'up', '--json') '数据库迁移'
    if ($migrate.Code -ne 0) {
        throw "数据库迁移失败：$($migrate.Output -join ' ')"
    }
    $migrationResult = ($migrate.Output -join "`n") | ConvertFrom-Json
    foreach ($item in $migrationResult) {
        foreach ($version in @($item.applied)) { $migrationsApplied += "$($item.alias)#$version" }
    }
    if ($migrationsApplied.Count -gt 0) {
        Write-LuxLog "已应用迁移：$($migrationsApplied -join '、')"
    }
    else {
        Write-LuxLog "没有需要应用的迁移"
    }

    if ($config.RunTests -and -not $SkipTests) {
        Write-LuxLog "运行测试集"
        Invoke-LuxPython @('-m', 'unittest', 'discover', '-s', 'tests') '测试集' | Out-Null
    }

    Start-LuxService
    if (-not (Wait-LuxHealthy)) {
        throw "健康检查未通过"
    }
}
catch {
    $failure = "$_"
    Write-LuxLog "发布失败：$failure" 'ERROR'
    if (-not $serviceStopped) {
        # 还没停服务/切代码，环境保持原样，不需要回退。
        Write-LuxLog "尚未停止服务或切换代码，环境未改动，无需回退" 'WARN'
        $state = @{
            release = $Ref; commit = $target.commit; previous_commit = $current.commit
            started = $startedAt; finished = (Get-Date).ToString('o')
            result = 'failed'; backup = "$backupPath"; migrations_applied = @($migrationsApplied)
            detail = $failure
        }
        Save-LuxState -State $state
        throw
    }
    if ($NoRollback) {
        Write-LuxLog "已指定 -NoRollback，服务保持停止/当前代码状态，请人工处理" 'ERROR'
        $state = @{
            release = $Ref; commit = $target.commit; previous_commit = $current.commit
            started = $startedAt; finished = (Get-Date).ToString('o')
            result = 'failed'; backup = "$backupPath"; migrations_applied = @($migrationsApplied)
            detail = $failure
        }
        Save-LuxState -State $state
        throw
    }

    Write-LuxLog "自动回退到 $($current.describe)（$($current.commit)）" 'WARN'
    try {
        Stop-LuxService
        Assert-LuxServiceStopped | Out-Null
        Invoke-LuxGit @('checkout', '--force', $current.commit) | Out-Null
        if ($backupPath -and $migrationsApplied.Count -gt 0) {
            Restore-LuxData -Archive $backupPath
        }
        Start-LuxService
        $healthy = Wait-LuxHealthy
        if ($healthy) { Write-LuxLog "已回退并恢复服务" 'WARN' }
        else { Write-LuxLog "回退后健康检查仍未通过，请人工检查 logs 目录" 'ERROR' }
        $rolledBack = $true
    }
    catch {
        Write-LuxLog "自动回退失败：$_（请人工按 deploy\README.md 处理）" 'ERROR'
    }

    $state = @{
        release = $Ref; commit = $target.commit; previous_commit = $current.commit
        started = $startedAt; finished = (Get-Date).ToString('o')
        result = 'rolled_back'; backup = "$backupPath"; migrations_applied = @($migrationsApplied)
        detail = $failure
    }
    Save-LuxState -State $state
    if (-not $rolledBack) { throw "发布失败且自动回退未完成：$failure" }
    throw "发布失败，已回退：$failure"
}

$state = @{
    release = $Ref; commit = $target.commit; previous_commit = $current.commit
    previous_describe = $current.describe
    started = $startedAt; finished = (Get-Date).ToString('o')
    result = 'success'; backup = "$backupPath"; migrations_applied = @($migrationsApplied)
    detail = ''
}
Save-LuxState -State $state
if ($serviceStopped) { Write-LuxLog "服务已按新版本运行" }
Write-LuxLog "==== 发布完成：$Ref ===="
exit 0
