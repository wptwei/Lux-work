#Requires -Version 5.1
<#
.SYNOPSIS
    LUX 统一服务管理入口：启动、停止、重启、状态、日志、诊断。

.DESCRIPTION
    所有命令共享同一套配置加载与进程归属逻辑（deploy/LuxDeploy.psm1）。
    部署、回退、自启脚本也复用同一套生命周期，不存在相互竞争的多个管理器。

.EXAMPLE
    .\lux.ps1 start
    .\lux.ps1 stop
    .\lux.ps1 restart
    .\lux.ps1 status
    .\lux.ps1 status -Json
    .\lux.ps1 logs -Which err -N 50
    .\lux.ps1 doctor
    .\lux.ps1 adopt -DryRun

    退出码：
        0 成功（已启动/已停止/健康）
        1 失败（配置错误、启动失败、健康检查未通过）
        2 端口冲突（被无关进程占用）
        3 无法确认进程归属 / 权限不足
        4 服务锁超时（另一个管理命令正在运行）
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'doctor', 'adopt')][string]$Command,
    [string]$ConfigPath,
    [int]$TimeoutSec = 0,
    [switch]$Json,
    [string]$Which = 'app',
    [int]$N = 50,
    [int]$AdoptPid = 0,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'deploy\LuxDeploy.psm1') -Force -DisableNameChecking

if (-not $Command) {
    Write-Host "用法：.\lux.ps1 start|stop|restart|status|logs|doctor|adopt"
    exit 2
}

$config = Initialize-LuxEnvironment -ConfigPath $ConfigPath

switch ($Command) {
    'start' {
        try {
            Start-LuxService
            $state = Get-LuxServiceState
            if ($state.running) {
                Write-LuxLog "服务运行中：$($state.detail)"
                if (-not (Wait-LuxHealthy -TimeoutSec $TimeoutSec)) {
                    Write-LuxLog "服务已启动但健康检查未通过，请查看 $($config.LogDir)" 'ERROR'
                    exit 1
                }
                Write-LuxLog "启动完成，健康检查通过"
                exit 0
            }
            Write-LuxLog "启动后服务未运行（$($state.detail)）" 'ERROR'
            exit 1
        }
        catch {
            Write-LuxLog "启动失败：$_" 'ERROR'
            if ("$_" -match '端口.*已占用|已被占用|不属于本项目') { exit 2 }
            if ("$_" -match '无法确认|权限|拒绝|拒绝访问') { exit 3 }
            if ("$_" -match '锁超时') { exit 4 }
            exit 1
        }
    }

    'stop' {
        try {
            Stop-LuxService -TimeoutSec $TimeoutSec
            exit 0
        }
        catch {
            Write-LuxLog "停止失败：$_" 'ERROR'
            if ("$_" -match '无法确认|权限|拒绝') { exit 3 }
            exit 1
        }
    }

    'restart' {
        try {
            Restart-LuxService -TimeoutSec $TimeoutSec
            if (-not (Wait-LuxHealthy -TimeoutSec $TimeoutSec)) {
                Write-LuxLog "重启后健康检查未通过" 'ERROR'
                exit 1
            }
            Write-LuxLog "重启完成，健康检查通过"
            exit 0
        }
        catch {
            Write-LuxLog "重启失败：$_" 'ERROR'
            if ("$_" -match '端口.*已占用|已被占用|不属于本项目') { exit 2 }
            if ("$_" -match '无法确认|权限|拒绝') { exit 3 }
            if ("$_" -match '锁超时') { exit 4 }
            exit 1
        }
    }

    'status' {
        try {
            $status = Get-LuxStatusObject
        }
        catch {
            Write-LuxLog "读取状态失败：$_" 'ERROR'
            exit 1
        }
        if ($Json) {
            $status | ConvertTo-Json -Depth 6
            exit 0
        }
        Write-Host "== 服务 =="
        Write-Host ("状态：{0}" -f $status.status)
        Write-Host ("详情：{0}" -f $status.detail)
        Write-Host ("本机地址：{0}" -f $status.display_url)
        Write-Host ("本机 HTTP：{0}" -f $(if ($status.local_http) { '正常' } else { '无响应' }))
        if ($status.running -and $null -ne $status.model_ok) {
            Write-Host ("本地模型：{0}" -f $(if ($status.model_ok) { '可用' } else { '不可用' }))
        }
        Write-Host ("托管模式：{0}" -f $status.service_mode)
        Write-Host "`n== 公网分享 =="
        if ($status.share.enabled) {
            Write-Host ("已开启：{0}" -f $(if ($status.share.url) { $status.share.url } else { '未找到 public URL' }))
        }
        else {
            Write-Host "未开启"
        }
        Write-Host "`n== 版本 =="
        Write-Host ("当前：{0}（{1}）" -f $status.revision.describe, $status.revision.commit)
        Write-Host ("路径：{0}" -f $status.repo_path)
        Write-Host ("日志：{0}" -f $status.log_dir)
        exit 0
    }

    'logs' {
        $lines = Get-LuxLogTail -Which $Which -Lines $N
        if ($lines.Count -eq 0) {
            Write-Host "（无日志：$Which）"
        }
        else {
            $lines | ForEach-Object { Write-Host $_ }
        }
        exit 0
    }

    'doctor' {
        try {
            $result = Invoke-LuxDoctor
        }
        catch {
            Write-LuxLog "诊断失败：$_" 'ERROR'
            exit 1
        }
        $s = $result.status
        Write-Host "== 服务 =="
        Write-Host ("状态：{0}（{1}）" -f $s.status, $s.detail)
        Write-Host ("本机 HTTP：{0}" -f $(if ($s.local_http) { '正常' } else { '无响应' }))
        if ($s.running -and $null -ne $s.model_ok) {
            Write-Host ("本地模型：{0}" -f $(if ($s.model_ok) { '可用' } else { '不可用' }))
        }
        Write-Host "== 公网分享 =="
        if ($s.share.enabled) {
            if ($s.share.url) {
                $probe = Test-LuxPublicUrl -Url $s.share.url
                Write-Host ("URL：{0}" -f $s.share.url)
                Write-Host ("探测：{0}" -f $(if ($probe.ok) { "正常（认证=$($probe.auth)）" } else { "失效（$($probe.reason)）" }))
            }
            else {
                Write-Host "已开启，但未找到 public URL"
            }
        }
        else {
            Write-Host "未开启"
        }
        Write-Host "== 版本 =="
        Write-Host ("当前：{0}（{1}）" -f $s.revision.describe, $s.revision.commit)
        Write-Host "== 结论 =="
        if ($result.healthy) {
            Write-Host "健康：全部通过" -ForegroundColor Green
            exit 0
        }
        else {
            $result.problems | ForEach-Object { Write-Host "问题：$_" -ForegroundColor Yellow }
            Write-Host "存在问题，详见上方" -ForegroundColor Yellow
            exit 1
        }
    }

    'adopt' {
        try {
            if ($AdoptPid -gt 0) {
                Adopt-LuxService -ListenPid $AdoptPid -DryRun:$DryRun | Out-Null
            }
            else {
                Adopt-LuxService -DryRun:$DryRun | Out-Null
            }
            exit 0
        }
        catch {
            Write-LuxLog "接管失败：$_" 'ERROR'
            exit 1
        }
    }
}
