#Requires -Version 5.1
<#
.SYNOPSIS
    LUX 服务管理隔离测试：只用临时目录、临时端口与临时进程，不触碰真实服务/数据。

.DESCRIPTION
    覆盖：配置解析与端口一致、BaseUrl 冲突报错、进程归属判定、状态往返与失效、
    跨进程锁、端口冲突不杀进程。
    运行：powershell -NoProfile -ExecutionPolicy Bypass -File tests\service_management.ps1
#>

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
$script:Repo = Split-Path -Parent $PSScriptRoot
Import-Module (Join-Path $script:Repo 'deploy\LuxDeploy.psm1') -Force -DisableNameChecking

# 隔离环境：清理可能从父进程继承的变量，改用临时 .env / 日志目录 / 测试端口。
foreach ($k in @('LUX_HOST', 'LUX_PORT', 'LUX_LOG_DIR', 'LUX_ENV_FILE', 'LUX_DEPLOY_CONFIG')) {
    Remove-Item "Env:$k" -ErrorAction SilentlyContinue
}
$script:TestRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("lux-svc-test-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $script:TestRoot -Force | Out-Null
$script:EnvFile = Join-Path $script:TestRoot '.env'
$script:LogDir = Join-Path $script:TestRoot 'logs'
$script:TestPort = 17861

function Write-TestEnv {
    param([int]$Port, [string]$ListenHost = '127.0.0.1')
    $lines = @("LUX_MODEL_MODE=external", "LUX_HOST=$ListenHost", "LUX_PORT=$Port")
    Set-Content -LiteralPath $script:EnvFile -Value $lines -Encoding UTF8
    $env:LUX_ENV_FILE = $script:EnvFile
    $env:LUX_LOG_DIR = $script:LogDir
    Remove-Item Env:LUX_DEPLOY_CONFIG -ErrorAction SilentlyContinue
    Remove-Item Env:LUX_HOST -ErrorAction SilentlyContinue
    Remove-Item Env:LUX_PORT -ErrorAction SilentlyContinue
}

$script:Passed = 0
$script:Failed = 0
$script:Failures = @()

function Assert {
    param([bool]$Condition, [string]$Name, [string]$Detail = '')
    if ($Condition) {
        $script:Passed++
        Write-Host ("[PASS] {0}" -f $Name) -ForegroundColor Green
    }
    else {
        $script:Failed++
        $script:Failures += "$Name : $Detail"
        Write-Host ("[FAIL] {0}  {1}" -f $Name, $Detail) -ForegroundColor Red
    }
}

# ---------- 1. 配置解析与端口一致 ----------
Write-TestEnv -Port $script:TestPort
$config = Initialize-LuxEnvironment
Assert ($config.Port -eq $script:TestPort) "LUX_PORT=$script:TestPort 解析为 Port" "实际 $($config.Port)"
Assert ($config.BaseUrl -eq "http://127.0.0.1:$script:TestPort") "BaseUrl 由 LUX_HOST/LUX_PORT 生成" $config.BaseUrl

# 监听 0.0.0.0 时，本机探测走回环
Write-TestEnv -Port $script:TestPort -ListenHost '0.0.0.0'
$config2 = Initialize-LuxEnvironment
Assert ($config2.BaseUrl -eq "http://127.0.0.1:$script:TestPort") "0.0.0.0 时探测回环" $config2.BaseUrl
Assert ($config2.Host -eq '0.0.0.0') "Host 保留 0.0.0.0" $config2.Host

# ---------- 2. BaseUrl 与 LUX_PORT 冲突时报错 ----------
Write-TestEnv -Port $script:TestPort
$cfgPath = Join-Path $script:TestRoot 'deploy.config.psd1'
@'
@{
    RepoPath = '.'
    ServiceMode = 'Pid'
    BaseUrl = 'http://127.0.0.1:7860'
}
'@ | Set-Content -LiteralPath $cfgPath -Encoding UTF8
$env:LUX_DEPLOY_CONFIG = $cfgPath
$threw = $false
try { Initialize-LuxEnvironment | Out-Null } catch { $threw = ($_ -match '不一致') }
Assert $threw "BaseUrl 端口与 LUX_PORT 不一致时报错"

# ---------- 3. 进程归属判定（纯证据） ----------
$venv = Join-Path $script:Repo '.venv\Scripts\python.exe'
$r = Test-LuxOwnershipEvidence -Exe $venv -Cmd "`"$venv`" app.py" -ParentExe '' -ParentCmd ''
Assert $r.owned "venv 启动器自身 → 属于本项目" ($r.reason)
$r = Test-LuxOwnershipEvidence -Exe 'C:\Python312\python.exe' -Cmd 'python.exe app.py' -ParentExe $venv -ParentCmd "`"$venv`" app.py"
Assert $r.owned "基础解释器且父进程是 venv 启动器 → 属于本项目" ($r.reason)
$r = Test-LuxOwnershipEvidence -Exe 'C:\Python312\python.exe' -Cmd 'python.exe app.py' -ParentExe 'C:\Python312\python.exe' -ParentCmd 'python.exe app.py'
Assert (-not $r.owned) "无 venv 证据的 app.py → 不属于本项目（unmanaged）" ($r.reason)
$r = Test-LuxOwnershipEvidence -Exe $venv -Cmd "`"$venv`" -m http.server" -ParentExe '' -ParentCmd ''
Assert (-not $r.owned) "命令行不含 app.py → 不属于本项目" ($r.reason)

# ---------- 4. 状态记录往返与失效 ----------
Write-TestEnv -Port $script:TestPort
Initialize-LuxEnvironment | Out-Null
$st = @{ version = 1; repo_path = $script:Repo; pid = 2147483647; launcher_pid = 1; created = '2026-01-01T00:00:00.0000000+00:00'; host = '127.0.0.1'; port = $script:TestPort; share_enabled = $false }
Write-LuxServiceState -State $st
$back = Read-LuxServiceState
Assert ($back -and $back.pid -eq 2147483647) "状态写入后可读回" "$($back.pid)"
$inst = Get-LuxManagedInstance
Assert ($inst -and $inst.stale -and $inst.reason -eq 'process_gone') "记录指向已不存在进程 → 判为失效" ($(if ($inst) { $inst.reason } else { 'null' }))
Clear-LuxServiceState
Assert (-not (Test-Path -LiteralPath $config.ServiceStateFile)) "清理后状态文件移除"

# ---------- 5. 跨进程锁 ----------
Write-TestEnv -Port $script:TestPort
Initialize-LuxEnvironment | Out-Null
$lock1 = Enter-LuxServiceLock -TimeoutSec 2
Assert ($null -ne $lock1) "首次取得服务锁"
$secondTimedOut = $false
try { $lock2 = Enter-LuxServiceLock -TimeoutSec 1; if ($lock2) { $lock2.Dispose() } } catch { $secondTimedOut = ($_ -match '锁超时') }
Assert $secondTimedOut "同一作用域第二次取锁超时（并发 start 被串行化）"
Exit-LuxServiceLock $lock1
$lock3 = Enter-LuxServiceLock -TimeoutSec 2
Assert ($null -ne $lock3) "释放后能再次取锁"
Exit-LuxServiceLock $lock3

# ---------- 6. 无关程序占端口 → 报冲突、不杀进程 ----------
Write-TestEnv -Port $script:TestPort
$config = Initialize-LuxEnvironment
$dummy = $null
try {
    $dummy = Start-Process -FilePath $config.PythonExe -ArgumentList @('-m', 'http.server', "$script:TestPort") `
        -WorkingDirectory $script:TestRoot -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 800
    $identity = Get-LuxPortIdentity
    Assert ($identity -and -not $identity.owned) "端口占用者识别为无关进程" ($(if ($identity) { $identity.reason } else { '无占用者' }))
    $threw = $false
    try { Start-LuxService } catch { $threw = ($_ -match '已被占用|不属于本项目') }
    Assert $threw "start 对无关进程占用端口报冲突（不换端口）"
    Assert (-not [string]::IsNullOrEmpty((Get-Process -Id $dummy.Id -ErrorAction SilentlyContinue).Id)) "不杀无关进程"
}
finally {
    if ($dummy) { Stop-Process -Id $dummy.Id -Force -ErrorAction SilentlyContinue }
}

# ---------- 汇总 ----------
Write-Host ""
Write-Host ("通过 {0}，失败 {1}" -f $script:Passed, $script:Failed)
if ($script:Failed -gt 0) {
    $script:Failures | ForEach-Object { Write-Host ("  ✗ {0}" -f $_) -ForegroundColor Red }
    exit 1
}
exit 0
