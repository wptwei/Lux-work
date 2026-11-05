# LUX 发布/运维共用函数：统一配置加载、进程归属、单实例启停、健康检查与发布状态。
# 只做本机操作；不联网（除 doctor 主动探测公网 URL）、不改动 .env、不打印密钥或隧道 token。

$script:LuxRoot = Split-Path -Parent $PSScriptRoot
$script:LuxConfig = $null
$script:LuxLogFile = $null

function Get-LuxRoot { return $script:LuxRoot }

function Get-LuxConfig { return $script:LuxConfig }

function Write-LuxLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = "[{0}] [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    switch ($Level) {
        'WARN' { Write-Host $line -ForegroundColor Yellow }
        'ERROR' { Write-Host $line -ForegroundColor Red }
        default { Write-Host $line }
    }
    if ($script:LuxLogFile) {
        try { Add-Content -LiteralPath $script:LuxLogFile -Value $line -Encoding UTF8 } catch { }
    }
}

function Resolve-LuxPath {
    param([string]$Path, [string]$Base = $script:LuxRoot)
    if (-not $Path) { return $Base }
    if ([System.IO.Path]::IsPathRooted($Path)) { return [System.IO.Path]::GetFullPath($Path) }
    return [System.IO.Path]::GetFullPath((Join-Path $Base $Path))
}

function Import-LuxEnv {
    <# 把 .env 读进当前进程（与 start_web.ps1 行为一致），不覆盖已显式设置的值。 #>
    param([string]$Path)
    if (-not $Path) {
        $Path = $env:LUX_ENV_FILE
        if (-not $Path) { $Path = Join-Path $script:LuxRoot '.env' }
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-LuxLog "未找到密钥配置文件：$Path（将使用已有环境变量）" 'WARN'
        return $Path
    }
    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
            $name, $value = $line.Split('=', 2)
            $name = $name.Trim()
            if (-not [Environment]::GetEnvironmentVariable($name, 'Process')) {
                [Environment]::SetEnvironmentVariable($name, $value.Trim(), 'Process')
            }
        }
    }
    return $Path
}

function Get-LuxDefaults {
    $logDir = $env:LUX_LOG_DIR
    if (-not $logDir) { $logDir = Join-Path $script:LuxRoot 'logs' }
    $hostName = $env:LUX_HOST
    $portStr = $env:LUX_PORT
    if (-not $hostName) { $hostName = '127.0.0.1' }
    if (-not $portStr) { $portStr = '7860' }
    if ($portStr -notmatch '^\d+$') { throw "LUX_PORT 不是有效端口：$portStr" }
    $port = [int]$portStr
    # 监听通配地址时，本机探测固定走回环；具体地址则探测该地址本身。
    $probeHost = $hostName
    if ($hostName -in @('0.0.0.0', '::', '*')) { $probeHost = '127.0.0.1' }
    return @{
        RepoPath              = $script:LuxRoot
        Remote                = 'origin'
        Branch                = 'main'
        PythonExe             = '.\.venv\Scripts\python.exe'
        ServiceMode           = 'Pid'          # Pid = 脚本自己拉起 app.py；ScheduledTask = 计划任务
        TaskName              = ''
        Host                  = $hostName
        Port                  = $port
        ProbeUrl              = "http://${probeHost}:$port"
        BaseUrl               = "http://${probeHost}:$port"   # 本地探活地址（回环归一化）
        DisplayUrl            = "http://${hostName}:$port"    # 展示给使用者的地址
        HealthTimeoutSec      = 120
        BackupKeep            = 10
        BackupDir             = ''
        RunTests              = $true
        AllowBranchRefs       = $false
        IncludeSecretsInBackup = $false
        LogDir                = $logDir
        StateFile             = (Join-Path $logDir 'deploy-state.json')
        LogFile               = (Join-Path $logDir 'deploy.log')
        PidFile               = (Join-Path $logDir 'lux.pid')
        ServiceStateFile      = (Join-Path $logDir 'lux-service-state.json')
        LockFile              = (Join-Path $logDir 'lux.lock')
    }
}

function Get-LuxConfigPath {
    param([string]$Path)
    if ($Path) { return (Resolve-LuxPath $Path) }
    if ($env:LUX_DEPLOY_CONFIG) { return (Resolve-LuxPath $env:LUX_DEPLOY_CONFIG) }
    $candidates = @(
        (Join-Path $script:LuxRoot 'deploy\deploy.config.psd1'),
        (Join-Path $script:LuxRoot 'config\deploy.config.psd1')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Initialize-LuxEnvironment {
    <# 读取 .env 与发布配置，设置日志文件；所有脚本的第一步。
       端口/地址以最终解析的 LUX_HOST/LUX_PORT 为准；BaseUrl 兼容旧配置但冲突时报错。 #>
    param([string]$ConfigPath)
    Import-LuxEnv | Out-Null
    $config = Get-LuxDefaults
    $resolved = Get-LuxConfigPath $ConfigPath
    if ($resolved) {
        try {
            $overrides = Import-PowerShellDataFile -LiteralPath $resolved
        }
        catch {
            throw "无法读取发布配置 $resolved：$($_.Exception.Message)（文件需保存为带 BOM 的 UTF-8）"
        }
        if ($null -eq $overrides) { throw "发布配置为空：$resolved" }
        foreach ($key in $overrides.Keys) { $config[$key] = $overrides[$key] }
        $config.RepoPath = Resolve-LuxPath $config.RepoPath
        if (-not $overrides.ContainsKey('LogDir') -or -not $config.LogDir) {
            if (-not $env:LUX_LOG_DIR) { $config.LogDir = Join-Path $config.RepoPath 'logs' }
        }
        $config.LogDir = Resolve-LuxPath $config.LogDir $config.RepoPath
        if (-not $overrides.ContainsKey('StateFile') -or -not $config.StateFile) {
            $config.StateFile = Join-Path $config.LogDir 'deploy-state.json'
        } else {
            $config.StateFile = Resolve-LuxPath $config.StateFile
        }
        if (-not $overrides.ContainsKey('LogFile') -or -not $config.LogFile) {
            $config.LogFile = Join-Path $config.LogDir 'deploy.log'
        } else {
            $config.LogFile = Resolve-LuxPath $config.LogFile
        }
        if (-not $overrides.ContainsKey('PidFile') -or -not $config.PidFile) {
            $config.PidFile = Join-Path $config.LogDir 'lux.pid'
        } else {
            $config.PidFile = Resolve-LuxPath $config.PidFile
        }
        $config.ServiceStateFile = Join-Path $config.LogDir 'lux-service-state.json'
        $config.LockFile = Join-Path $config.LogDir 'lux.lock'
        if (-not $overrides.ContainsKey('BackupDir') -or -not $config.BackupDir) {
            $config.BackupDir = Join-Path $config.RepoPath 'backups'
        } else {
            $config.BackupDir = Resolve-LuxPath $config.BackupDir
        }
        $config.PythonExe = Resolve-LuxPath $config.PythonExe $config.RepoPath
        Write-LuxLog "发布配置：$resolved"
    } else {
        $config.BackupDir = Join-Path $config.RepoPath 'backups'
        $config.PythonExe = Resolve-LuxPath $config.PythonExe $config.RepoPath
        Write-LuxLog "未找到 deploy.config.psd1，使用默认配置" 'WARN'
    }

    # 端口/地址最终以 LUX_HOST / LUX_PORT 为准（deploy.config 不该再固定端口）。
    $hostName = $env:LUX_HOST
    $portStr = $env:LUX_PORT
    if (-not $hostName) { $hostName = '127.0.0.1' }
    if (-not $portStr) { $portStr = '7860' }
    if ($portStr -notmatch '^\d+$') { throw "LUX_PORT 不是有效端口：$portStr" }
    $port = [int]$portStr
    $probeHost = $hostName
    if ($hostName -in @('0.0.0.0', '::', '*')) { $probeHost = '127.0.0.1' }
    # 兼容旧 BaseUrl：显式配置且端口与 LUX_PORT 不一致时，明确报错，不静默换端口。
    if ($config.ContainsKey('BaseUrl') -and "$($config.BaseUrl)".Trim() -and "$($config.BaseUrl)".Trim() -ne "http://${probeHost}:$port") {
        $cfgUri = $null
        try { $cfgUri = [System.Uri]"$($config.BaseUrl)" } catch { }
        if (-not $cfgUri) {
            throw "deploy.config.psd1 的 BaseUrl「$($config.BaseUrl)」无法解析；请删除该项或改为与 LUX_PORT 一致。"
        }
        if ($cfgUri.Port -ne $port) {
            throw "deploy.config.psd1 的 BaseUrl 端口 $($cfgUri.Port) 与 LUX_PORT=$port 不一致；请删除 BaseUrl 或改一致，避免检查错误端口。"
        }
        Write-LuxLog "BaseUrl 主机 $($cfgUri.Host) 与 LUX_HOST $hostName 不一致，以 LUX_HOST 为准" 'WARN'
    }
    $config.Host = $hostName
    $config.Port = $port
    $config.ProbeUrl = "http://${probeHost}:$port"
    $config.BaseUrl = "http://${probeHost}:$port"
    $config.DisplayUrl = "http://${hostName}:$port"
    if (-not $config.ContainsKey('ServiceStateFile') -or -not $config.ServiceStateFile) {
        $config.ServiceStateFile = Join-Path $config.LogDir 'lux-service-state.json'
    }
    if (-not $config.ContainsKey('LockFile') -or -not $config.LockFile) {
        $config.LockFile = Join-Path $config.LogDir 'lux.lock'
    }

    if (-not (Test-Path -LiteralPath $config.LogDir)) {
        New-Item -ItemType Directory -Path $config.LogDir -Force | Out-Null
    }
    $script:LuxConfig = $config
    $script:LuxLogFile = $config.LogFile
    return $config
}

function Invoke-LuxCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory,
        [string]$Label = $Exe
    )
    if (-not $WorkingDirectory) { $WorkingDirectory = $script:LuxConfig.RepoPath }
    Write-LuxLog ("执行：" + $Exe + ' ' + ($Arguments -join ' '))
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $text = "$_"
            Write-Host $text
            if ($script:LuxLogFile) {
                try { Add-Content -LiteralPath $script:LuxLogFile -Value $text -Encoding UTF8 } catch { }
            }
        }
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
        $ErrorActionPreference = $previous
    }
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0) { throw "$Label 失败（exit $code）" }
    return $code
}

function Invoke-LuxGit {
    param([string[]]$Arguments, [switch]$AllowFailure)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & git @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw "git $($Arguments -join ' ') 失败：$out"
    }
    return @{ Output = $out; Code = $code }
}

function Invoke-LuxPython {
    param([string[]]$Arguments, [string]$Label = 'python')
    $python = $script:LuxConfig.PythonExe
    if (-not (Test-Path -LiteralPath $python)) {
        throw "找不到 Python 运行时：$python（请先运行 install.ps1）"
    }
    return Invoke-LuxCommand -Exe $python -Arguments $Arguments -Label $Label
}

function Invoke-LuxPythonCapture {
    param([string[]]$Arguments, [string]$Label = 'python')
    $python = $script:LuxConfig.PythonExe
    if (-not (Test-Path -LiteralPath $python)) {
        throw "找不到 Python 运行时：$python（请先运行 install.ps1）"
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    Push-Location -LiteralPath $script:LuxConfig.RepoPath
    try {
        $output = & $python @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
        $ErrorActionPreference = $previous
    }
    if ($null -eq $code) { $code = 0 }
    Write-LuxLog "$Label 退出码：$code"
    return @{ Output = @($output); Code = $code }
}

function Get-LuxRevision {
    <# 版本信息；-AllowFailure 时 git 不可读返回空串（status/doctor 需要，发布仍需失败）。 #>
    param([switch]$AllowFailure)
    $describe = (Invoke-LuxGit @('describe', '--tags', '--always') -AllowFailure:$AllowFailure).Output
    $commit = (Invoke-LuxGit @('rev-parse', 'HEAD') -AllowFailure:$AllowFailure).Output
    return @{ describe = "$describe".Trim(); commit = "$commit".Trim() }
}

function Test-LuxRepoClean {
    param([switch]$AllowFailure)
    $status = (Invoke-LuxGit @('status', '--porcelain') -AllowFailure:$AllowFailure)
    if ($status.Code -ne 0) { return $null }
    return [string]::IsNullOrWhiteSpace("$($status.Output)")
}

function Resolve-LuxRef {
    param([string]$Ref, [switch]$AllowBranch)
    Invoke-LuxGit @('fetch', '--tags', '--prune', $script:LuxConfig.Remote) | Out-Null
    $tag = Invoke-LuxGit @('rev-parse', '--verify', '--quiet', "refs/tags/$Ref") -AllowFailure
    $commit = Invoke-LuxGit @('rev-parse', '--verify', '--quiet', "$Ref^{commit}") -AllowFailure
    if ($commit.Code -ne 0) { throw "找不到版本或提交：$Ref" }
    if ($tag.Code -ne 0 -and -not $AllowBranch) {
        $branch = Invoke-LuxGit @('rev-parse', '--verify', '--quiet', "refs/remotes/$($script:LuxConfig.Remote)/$Ref") -AllowFailure
        if ($branch.Code -eq 0) {
            throw "「$Ref」是分支名，不是发布版本（tag）。请使用 tag，或在配置中打开 AllowBranchRefs。"
        }
    }
    return @{ commit = "$($commit.Output)".Trim(); isTag = ($tag.Code -eq 0) }
}

# ============================================================
# 进程归属：跨进程锁、进程身份、状态记录、端口占用、frpc 后代
# ============================================================

function Enter-LuxServiceLock {
    <# 跨进程锁：两个终端同时 start/stop 只允许一个执行。锁文件在 LogDir（= 本项目的日志目录）。 #>
    param([int]$TimeoutSec = 30)
    if (-not $script:LuxConfig) { throw "未初始化配置" }
    $lockPath = $script:LuxConfig.LockFile
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $fs = $null
    while ((Get-Date) -lt $deadline) {
        try {
            $fs = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::OpenOrCreate,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
            return $fs
        }
        catch {
            Start-Sleep -Milliseconds 200
        }
    }
    throw "获取服务锁超时（$TimeoutSec 秒）：可能有另一个 lux 命令正在运行。"
}

function Exit-LuxServiceLock {
    param($Lock)
    if ($Lock) { try { $Lock.Dispose() } catch { } }
}

function Get-LuxProcessInfo {
    <# 读取单个进程的标识信息。失败/拒绝访问返回带 error 的哈希，进程不存在返回 $null。 #>
    param([int]$Pid)
    if ($Pid -le 0) { return $null }
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$Pid" -ErrorAction Stop
    }
    catch {
        return @{ pid = $Pid; error = "$($_.Exception.Message)" }
    }
    if (-not $p) { return $null }
    $cd = $p.CreationDate
    if ($cd -is [datetime]) { $created = $cd.ToString('o') } else { $created = "$cd" }
    return @{
        pid         = [int]$p.ProcessId
        parent_pid  = [int]$p.ParentProcessId
        name        = "$($p.Name)"
        exe         = "$($p.ExecutablePath)"
        cmd         = "$($p.CommandLine)"
        created     = $created
    }
}

function Test-LuxOwnershipEvidence {
    <# 纯判定：给定可执行路径与命令行（含父进程），判断是否属于本项目。
       证据不足时返回 owned=false，绝不按名称批量判定。 #>
    param([string]$Exe, [string]$Cmd, [string]$ParentExe, [string]$ParentCmd)
    $repo = [System.IO.Path]::GetFullPath($script:LuxConfig.RepoPath)
    $venvPython = [System.IO.Path]::GetFullPath((Join-Path $repo '.venv\Scripts\python.exe'))
    if (-not $Cmd -or $Cmd -notmatch 'app\.py') {
        return @{ owned = $false; reason = 'not_app_py' }
    }
    if ($Exe -and [System.IO.Path]::GetFullPath($Exe) -ieq $venvPython) {
        return @{ owned = $true; reason = 'venv_launcher_self' }
    }
    if ($ParentExe -and $ParentCmd -match 'app\.py' `
            -and [System.IO.Path]::GetFullPath($ParentExe) -ieq $venvPython) {
        return @{ owned = $true; reason = 'venv_launcher_parent' }
    }
    return @{ owned = $false; reason = 'not_our_venv' }
}

function Test-LuxIsOwnedProcess {
    <# 判断一个进程是否属于本项目：命令行含 app.py，且自身或父进程是本项目 venv 的 python 启动器。
       证据不足时返回 owned=false，绝不按名称批量判定。 #>
    param([hashtable]$Info)
    if (-not $Info) { return @{ owned = $false; reason = 'gone' } }
    if ($Info.error) { return @{ owned = $false; reason = 'access_denied' } }
    $parent = $null
    if ($Info.parent_pid -gt 0) { $parent = Get-LuxProcessInfo $Info.parent_pid }
    $parentExe = ''
    $parentCmd = ''
    if ($parent -and -not $parent.error) { $parentExe = $parent.exe; $parentCmd = $parent.cmd }
    return Test-LuxOwnershipEvidence -Exe $Info.exe -Cmd $Info.cmd -ParentExe $parentExe -ParentCmd $parentCmd
}

function Get-LuxDescendantPids {
    <# 返回属于 RootPid 后代、进程名匹配 NamePattern 的 PID 列表（用于 frpc 隧道）。 #>
    param([int]$RootPid, [string]$NamePattern)
    if ($RootPid -le 0) { return @() }
    $all = Get-CimInstance Win32_Process -Filter "Name LIKE '$NamePattern'" -ErrorAction SilentlyContinue
    $result = @()
    foreach ($p in $all) {
        $cur = $p
        for ($i = 0; $i -lt 8; $i++) {
            if (-not $cur) { break }
            $ppid = [int]$cur.ParentProcessId
            if ($ppid -eq $RootPid) { $result += [int]$p.ProcessId; break }
            if ($ppid -le 0 -or $ppid -eq [int]$cur.ProcessId) { break }
            $cur = Get-CimInstance Win32_Process -Filter "ProcessId=$ppid" -ErrorAction SilentlyContinue
        }
    }
    return @($result | Select-Object -Unique)
}

function Get-LuxPortOwner {
    <# 谁在监听服务端口。返回 @{ pid; command }（兼容旧调用），另有 Get-LuxPortIdentity 返回完整归属。 #>
    if (-not $script:LuxConfig) { return $null }
    $conn = Get-NetTCPConnection -LocalPort $script:LuxConfig.Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    $pid = [int]$conn.OwningProcess
    $info = Get-LuxProcessInfo $pid
    return @{ pid = $pid; command = $info.cmd }
}

function Get-LuxPortIdentity {
    <# 端口占用者的完整归属：pid + 证据 + owned 判定。 #>
    if (-not $script:LuxConfig) { return $null }
    $conn = Get-NetTCPConnection -LocalPort $script:LuxConfig.Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $conn) { return $null }
    $pid = [int]$conn.OwningProcess
    $info = Get-LuxProcessInfo $pid
    $owned = Test-LuxIsOwnedProcess $info
    return @{ pid = $pid; info = $info; owned = $owned.owned; reason = $owned.reason }
}

function Read-LuxServiceState {
    $path = $script:LuxConfig.ServiceStateFile
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { return $null }
}

function Write-LuxServiceState {
    param([hashtable]$State)
    $path = $script:LuxConfig.ServiceStateFile
    $tmp = "$path.tmp"
    ($State | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $path -Force
}

function Clear-LuxServiceState {
    Remove-Item -LiteralPath $script:LuxConfig.ServiceStateFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $script:LuxConfig.PidFile -Force -ErrorAction SilentlyContinue
}

function Get-LuxManagedInstance {
    <# 从状态文件读取并现场核验当前托管实例。返回 $null（无托管），或哈希：
       @{ running; pid; launcher_pid; created; state } 或 @{ stale=$true; reason; ... }。 #>
    $state = Read-LuxServiceState
    if (-not $state) { return $null }
    $pid = [int]$state.pid
    $info = Get-LuxProcessInfo $pid
    if (-not $info) {
        return @{ stale = $true; reason = 'process_gone'; state = $state; pid = $pid }
    }
    if ($info.error) {
        return @{ stale = $true; reason = 'access_denied'; state = $state; pid = $pid }
    }
    if ($state.created -and $info.created -and "$($state.created)" -ne "$($info.created)") {
        return @{ stale = $true; reason = 'pid_reused'; state = $state; pid = $pid }
    }
    $owned = Test-LuxIsOwnedProcess $info
    if (-not $owned.owned) {
        return @{ stale = $true; reason = 'not_owned'; state = $state; pid = $pid; owned = $owned }
    }
    $portIdentity = Get-LuxPortIdentity
    if (-not $portIdentity -or $portIdentity.pid -ne $pid) {
        return @{ running = $false; reason = 'not_listening'; state = $state; pid = $pid; launcher_pid = [int]$state.launcher_pid }
    }
    return @{
        running      = $true
        pid          = $pid
        launcher_pid = [int]$state.launcher_pid
        created      = $info.created
        info         = $info
        state        = $state
    }
}

# ============================================================
# 服务生命周期（统一实现；deploy/rollback/restart/自启都走这里）
# ============================================================

function Get-LuxServicePid {
    <# 兼容旧调用：返回托管实例的监听 PID，无法确认则 $null。 #>
    if ($script:LuxConfig.ServiceMode -eq 'ScheduledTask') { return $null }
    $inst = Get-LuxManagedInstance
    if ($inst -and $inst.running) { return [int]$inst.pid }
    return $null
}

function Get-LuxServiceState {
    <# 兼容旧调用，返回 @{ running; detail; status; ... }。 #>
    if ($script:LuxConfig.ServiceMode -eq 'ScheduledTask') {
        $task = Get-ScheduledTask -TaskName $script:LuxConfig.TaskName -ErrorAction SilentlyContinue
        if (-not $task) { return @{ running = $false; status = 'stopped'; detail = '计划任务不存在' } }
        $running = ($task.State -eq 'Running')
        return @{ running = $running; status = $(if ($running) { 'running' } else { 'stopped' }); detail = "计划任务 $($script:LuxConfig.TaskName)：$($task.State)" }
    }
    $inst = Get-LuxManagedInstance
    if ($inst -and $inst.running) {
        return @{ running = $true; status = 'running'; pid = $inst.pid; launcher_pid = $inst.launcher_pid; detail = "PID $($inst.pid)" }
    }
    if ($inst -and $inst.stale) {
        return @{ running = $false; status = 'stale'; detail = "残留状态：$($inst.reason)（PID $($inst.pid)）" }
    }
    # 状态文件里没有，但端口被本项目进程占用：属于旧实例/未接管，标 unmanaged。
    $portIdentity = Get-LuxPortIdentity
    if ($portIdentity) {
        if ($portIdentity.owned) {
            return @{ running = $true; status = 'unmanaged'; pid = $portIdentity.pid; detail = "PID $($portIdentity.pid)（端口占用，未由本脚本托管）" }
        }
        return @{ running = $false; status = 'port_conflict'; pid = $portIdentity.pid; detail = "端口被无关进程 PID $($portIdentity.pid) 占用" }
    }
    return @{ running = $false; status = 'stopped'; detail = '未运行' }
}

function Start-LuxService {
    <# 幂等启动：已运行（且确认归属）时返回现状；端口被无关进程占用时报错，不换端口、不杀进程。 #>
    $lock = Enter-LuxServiceLock
    try {
        if ($script:LuxConfig.ServiceMode -eq 'ScheduledTask') {
            return (Start-LuxScheduledTask)
        }
        # 1) 已由本脚本托管且健在 → 幂等返回。
        $inst = Get-LuxManagedInstance
        if ($inst -and $inst.running) {
            Write-LuxLog "服务已在运行（PID $($inst.pid)），不重复启动" 'WARN'
            return
        }
        # 2) 端口被占用：确认是否本项目；无关/未知则报冲突。
        $portIdentity = Get-LuxPortIdentity
        if ($portIdentity) {
            if ($portIdentity.owned) {
                # 旧实例（无状态文件）：接管，写入状态，不新起第二个。
                Write-LuxLog "检测到本项目的运行实例 PID $($portIdentity.pid)（无状态文件），接管并写入状态" 'WARN'
                Adopt-LuxService -ListenPid $portIdentity.pid | Out-Null
                return
            }
            $hint = "占用进程 PID $($portIdentity.pid)"
            if ($portIdentity.reason -eq 'access_denied') { $hint += "（无法读取进程信息，无法确认归属）" }
            else { $hint += "（非本项目进程）" }
            throw "端口 $($script:LuxConfig.BaseUrl) 已被占用且不属于本项目，不杀进程、不自动换端口。$hint"
        }
        # 3) 清理残留状态后启动。
        Clear-LuxServiceState
        $python = $script:LuxConfig.PythonExe
        if (-not (Test-Path -LiteralPath $python)) { throw "找不到 Python 运行时：$python" }
        Ensure-LuxOllama | Out-Null
        $outLog = Join-Path $script:LuxConfig.LogDir 'app.out.log'
        $errLog = Join-Path $script:LuxConfig.LogDir 'app.err.log'
        $appPath = Join-Path $script:LuxConfig.RepoPath 'app.py'
        $launcher = Start-Process -FilePath $python -ArgumentList @('-u', "`"$appPath`"") `
            -WorkingDirectory $script:LuxConfig.RepoPath `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
            -PassThru -WindowStyle Hidden
        # venv 的 python.exe 会再拉起真实解释器，端口由子进程持有；等端口出现并确认归属。
        $deadline = (Get-Date).AddSeconds([int]$script:LuxConfig.HealthTimeoutSec)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $portIdentity = Get-LuxPortIdentity
            if ($portIdentity -and $portIdentity.owned) {
                $listenInfo = $portIdentity.info
                $launcherPid = [int]$launcher.Id
                if ($portIdentity.pid -ne $launcherPid) {
                    $launcherPid = [int]$listenInfo.parent_pid
                }
                $state = @{
                    version       = 1
                    repo_path     = [System.IO.Path]::GetFullPath($script:LuxConfig.RepoPath)
                    pid           = $portIdentity.pid
                    launcher_pid  = $launcherPid
                    created       = $listenInfo.created
                    host          = $script:LuxConfig.Host
                    port          = $script:LuxConfig.Port
                    base_url      = $script:LuxConfig.BaseUrl
                    out_log       = $outLog
                    err_log       = $errLog
                    share_enabled = (Test-LuxShareEnabled)
                    share_url     = $null
                    started       = (Get-Date).ToString('o')
                }
                # 公网分享：等 gradio 打印 public URL，短超时，拿不到留给 doctor 再扫。
                if ($state.share_enabled) {
                    $state.share_url = Wait-LuxShareUrl -TimeoutSec 20
                }
                Write-LuxServiceState -State $state
                Set-Content -LiteralPath $script:LuxConfig.PidFile -Value $portIdentity.pid -Encoding ASCII
                Write-LuxLog "服务已启动（PID $($portIdentity.pid)）"
                return
            }
            if (-not (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) { break }
        }
        Set-Content -LiteralPath $script:LuxConfig.PidFile -Value $launcher.Id -Encoding ASCII
        throw "服务进程已启动但未在 $([int]$script:LuxConfig.HealthTimeoutSec) 秒内监听 $($script:LuxConfig.BaseUrl)，请查看 $errLog"
    }
    finally {
        Exit-LuxServiceLock $lock
    }
}

function Stop-LuxService {
    <# 幂等停止：只终止确认属于本实例的监听进程、启动器与 frpc 后代，不按名称批量结束。 #>
    param([int]$TimeoutSec = 30)
    $lock = Enter-LuxServiceLock
    try {
        if ($script:LuxConfig.ServiceMode -eq 'ScheduledTask') {
            return (Stop-LuxScheduledTask -TimeoutSec $TimeoutSec)
        }
        $inst = Get-LuxManagedInstance
        $targets = New-Object System.Collections.Generic.List[int]

        if ($inst -and $inst.running) {
            $targets.Add([int]$inst.pid)
            if ($inst.launcher_pid -gt 0 -and $inst.launcher_pid -ne $inst.pid) { $targets.Add([int]$inst.launcher_pid) }
        }
        elseif ($inst -and $inst.stale) {
            # 状态文件存在但进程已消失/PID 复用/归属变化：只清残留，不杀任何进程。
            Write-LuxLog "托管状态已失效（$($inst.reason)），清理残留状态" 'WARN'
            Clear-LuxServiceState
            return
        }

        # 状态文件没有、但端口被本项目进程占用：也一并接管式停止（避免 frpc 残留）。
        $portIdentity = Get-LuxPortIdentity
        if ($portIdentity -and $portIdentity.owned -and -not $targets.Contains([int]$portIdentity.pid)) {
            $targets.Add([int]$portIdentity.pid)
        }

        if ($targets.Count -eq 0) {
            $portIdentity = Get-LuxPortIdentity
            if ($portIdentity -and -not $portIdentity.owned) {
                Write-LuxLog "端口 $($script:LuxConfig.BaseUrl) 被无关进程 PID $($portIdentity.pid) 占用，本服务未运行，不处理该进程" 'WARN'
            }
            else {
                Write-LuxLog "服务未运行，无需停止"
            }
            Clear-LuxServiceState
            return
        }

        # frpc 后代：先杀隧道，避免残留公网转发。
        foreach ($listenPid in @($targets | Where-Object { $_ -ne $inst.launcher_pid })) {
            $frpc = Get-LuxDescendantPids -RootPid $listenPid -NamePattern 'frpc%'
            foreach ($fp in $frpc) {
                if (-not $targets.Contains([int]$fp)) { $targets.Add([int]$fp) }
            }
        }

        # 逐个停止前，先核验目标仍是本项目进程，防止 PID 复用后误杀。
        $killed = @()
        foreach ($t in $targets) {
            $info = Get-LuxProcessInfo ([int]$t)
            if (-not $info) { continue }
            if ($info.error) {
                Write-LuxLog "无法读取进程 $t 的信息（$($info.error)），跳过不停止" 'WARN'
                continue
            }
            $isOwned = (Test-LuxIsOwnedProcess $info).owned
            $isFrpc = ($info.name -like 'frpc*')
            if (-not $isOwned -and -not $isFrpc) {
                Write-LuxLog "进程 $t 不再确认属于本项目，跳过不停止" 'WARN'
                continue
            }
            Stop-Process -Id ([int]$t) -Force -ErrorAction SilentlyContinue
            $killed += [int]$t
        }

        $deadline = (Get-Date).AddSeconds($TimeoutSec)
        while ((Get-Date) -lt $deadline) {
            if (-not (Get-LuxPortOwner)) { break }
            Start-Sleep -Milliseconds 300
        }
        Clear-LuxServiceState
        if ($killed.Count -gt 0) {
            Write-LuxLog ("服务已停止（PID {0}）" -f ($killed -join '、'))
        }
        else {
            Write-LuxLog "未终止任何进程（目标均无法确认归属）" 'WARN'
        }
    }
    finally {
        Exit-LuxServiceLock $lock
    }
}

function Restart-LuxService {
    param([int]$TimeoutSec = 0)
    if (-not $TimeoutSec) { $TimeoutSec = [int]$script:LuxConfig.HealthTimeoutSec }
    Stop-LuxService -TimeoutSec $TimeoutSec
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-LuxPortOwner)) { break }
        Start-Sleep -Milliseconds 300
    }
    if (Get-LuxPortOwner) {
        throw "旧实例未在 $TimeoutSec 秒内退出，端口仍被占用；未启动新实例，请人工检查。"
    }
    Start-LuxService
}

function Test-LuxHttp {
    param([int]$TimeoutSec = 5)
    try {
        $response = Invoke-WebRequest -Uri $script:LuxConfig.BaseUrl -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($response.StatusCode -lt 600)
    }
    catch {
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

function Assert-LuxServiceStopped {
    param([int]$TimeoutSec = 15)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-LuxHttp -TimeoutSec 5)) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Wait-LuxHealthy {
    param([int]$TimeoutSec = 0)
    if (-not $TimeoutSec) { $TimeoutSec = [int]$script:LuxConfig.HealthTimeoutSec }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $state = Get-LuxServiceState
        if (-not $state.running) {
            Write-LuxLog "服务进程已退出（$($state.detail)）" 'ERROR'
            return $false
        }
        if (Test-LuxHttp) { break }
        Start-Sleep -Seconds 2
    }
    if (-not (Test-LuxHttp)) {
        Write-LuxLog "等待 $TimeoutSec 秒后仍无法访问 $($script:LuxConfig.BaseUrl)" 'ERROR'
        return $false
    }
    Write-LuxLog "Web 端口已就绪：$($script:LuxConfig.BaseUrl)"
    try {
        Invoke-LuxPython @('healthcheck.py') 'healthcheck.py' | Out-Null
    }
    catch {
        Write-LuxLog "$_" 'ERROR'
        return $false
    }
    return $true
}

# ============================================================
# 计划任务托管（保留，但与 Pid 二选一）
# ============================================================

function Register-LuxScheduledTask {
    $taskName = $script:LuxConfig.TaskName
    $servicePath = Join-Path $script:LuxConfig.RepoPath 'service.ps1'
    $userId = "$env:USERDOMAIN\$env:USERNAME"
    $argument = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $servicePath
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $script:LuxConfig.RepoPath
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'LUX 本地 Agent 服务（deploy 脚本注册）' -Force | Out-Null
}

function Start-LuxScheduledTask {
    $taskName = $script:LuxConfig.TaskName
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        if ($attempt -eq 3 -or $attempt -eq 5) {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($task -and $task.Principal.LogonType -eq 'InteractiveToken') {
                Register-LuxScheduledTask
                Write-LuxLog "已重新注册计划任务：$taskName"
            }
        }
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(6)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($task -and $task.State -eq 'Running') {
                Write-LuxLog "已启动计划任务：$taskName"
                return
            }
        }
        $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
        $result = ''
        if ($info) { $result = $info.LastTaskResult }
        Write-LuxLog ("计划任务未启动（第 {0}/6 次，LastResult={1}），5 秒后重试" -f $attempt, $result) 'WARN'
        Start-Sleep -Seconds 5
    }
    throw "无法启动计划任务 $taskName。请手动执行 Start-ScheduledTask -TaskName '$taskName'，并查看任务计划程序的操作历史。"
}

function Stop-LuxScheduledTask {
    param([int]$TimeoutSec = 30)
    $taskName = $script:LuxConfig.TaskName
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName
        $deadline = (Get-Date).AddSeconds($TimeoutSec)
        while ((Get-Date) -lt $deadline) {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if (-not $task -or $task.State -ne 'Running') { break }
            Start-Sleep -Milliseconds 300
        }
        Write-LuxLog "已停止计划任务：$taskName"
    }
}

function Invoke-LuxServiceForeground {
    <# 前台运行 app.py（计划任务托管 / start_web.ps1 -Foreground 调试用）。
       先做归属实例检查，避免起第二份；返回进程退出码。 #>
    $portIdentity = Get-LuxPortIdentity
    if ($portIdentity) {
        if ($portIdentity.owned) {
            Write-LuxLog "服务已在运行（PID $($portIdentity.pid)），本实例退出" 'WARN'
            return 0
        }
        Write-LuxLog "端口 $($script:LuxConfig.BaseUrl) 已被无关进程占用，拒绝启动" 'ERROR'
        return 1
    }
    Ensure-LuxOllama | Out-Null
    $python = $script:LuxConfig.PythonExe
    if (-not (Test-Path -LiteralPath $python)) { throw "找不到 Python 运行时：$python" }
    $appPath = Join-Path $script:LuxConfig.RepoPath 'app.py'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUNBUFFERED = '1'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $python $appPath
        return $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

# ============================================================
# 诊断：状态 / doctor / 日志 / 接管
# ============================================================

function Test-LuxShareEnabled {
    $v = $env:LUX_SHARE
    return ($v -and $v -match '^(1|true|yes|on)$')
}

function Get-LuxShareLogPaths {
    param([switch]$LegacyShareLogs)
    <# 公网分享 URL 优先看当前实例的 app.out.log；只在接管旧实例时才回退到 public-share-*.out.log。 #>
    $paths = @()
    $appOut = Join-Path $script:LuxConfig.LogDir 'app.out.log'
    if (Test-Path -LiteralPath $appOut) { $paths += $appOut }
    if ($LegacyShareLogs) {
        Get-ChildItem -LiteralPath $script:LuxConfig.LogDir -Filter 'public-share-*.out.log' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | ForEach-Object { $paths += $_.FullName }
    }
    return @($paths)
}

function Find-LuxShareUrlInLogs {
    param([switch]$LegacyShareLogs)
    $pattern = 'Running on public URL:\s*(\S+)'
    foreach ($path in (Get-LuxShareLogPaths -LegacyShareLogs:$LegacyShareLogs)) {
        $line = Select-String -LiteralPath $path -Pattern $pattern -ErrorAction SilentlyContinue | Select-Object -Last 1
        if ($line -and $line.Matches.Count -gt 0 -and $line.Matches[0].Groups[1].Value) {
            return $line.Matches[0].Groups[1].Value
        }
    }
    return $null
}

function Get-LuxShareUrl {
    <# 从状态文件或日志扫描公网分享 URL；不发起请求。接管旧实例时才回退到旧日志。 #>
    $state = Read-LuxServiceState
    if ($state -and $state.share_url) { return "$($state.share_url)" }
    return (Find-LuxShareUrlInLogs -LegacyShareLogs)
}

function Wait-LuxShareUrl {
    <# 仅扫描当前实例的 app.out.log，避免 fresh start 误取旧实例残留的 public-share-*.log。 #>
    param([int]$TimeoutSec = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $url = Find-LuxShareUrlInLogs
        if ($url) { return $url }
        Start-Sleep -Milliseconds 500
    }
    return $null
}

function Test-LuxPublicUrl {
    <# 主动探测公网分享 URL（doctor 用）：本机正常不代表公网可用；"No interface is running" 判公网故障。 #>
    param([string]$Url, [int]$TimeoutSec = 12)
    if (-not $Url) { return @{ ok = $false; reason = 'no_url' } }
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -MaximumRedirection 3
        $body = "$($r.Content)"
        if ($r.StatusCode -ge 400) { return @{ ok = $false; reason = "http_$($r.StatusCode)" } }
        if ($body -match 'No interface is running') { return @{ ok = $false; reason = 'no_interface' } }
        $auth = $false
        $infoUrl = $Url.TrimEnd('/') + '/gradio_api/info'
        try {
            Invoke-WebRequest -Uri $infoUrl -UseBasicParsing -TimeoutSec $TimeoutSec | Out-Null
            $auth = $false
        }
        catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -in @(401, 403)) { $auth = $true }
            else { $auth = $false }
        }
        return @{ ok = $true; status = $r.StatusCode; auth = $auth; checked = (Get-Date).ToString('o') }
    }
    catch {
        return @{ ok = $false; reason = "$($_.Exception.Message)" }
    }
}

function Get-LuxStatusObject {
    <# 结构化的运行状态，供 status / doctor / -Json 复用。 #>
    $state = Get-LuxServiceState
    $localHttp = Test-LuxHttp -TimeoutSec 4
    $shareEnabled = Test-LuxShareEnabled
    $shareUrl = $null
    $share = @{ enabled = $shareEnabled; url = $null; probed = $false }
    if ($shareEnabled) {
        $shareUrl = Get-LuxShareUrl
        $share.url = $shareUrl
    }
    $modelOk = $null
    if ($state.running) {
        $modelOk = (Ensure-LuxOllama)
    }
    $revision = Get-LuxRevision -AllowFailure
    return @{
        status        = $state.status
        running       = $state.running
        pid           = $state.pid
        launcher_pid  = $state.launcher_pid
        detail        = $state.detail
        host          = $script:LuxConfig.Host
        port          = $script:LuxConfig.Port
        display_url   = $script:LuxConfig.DisplayUrl
        base_url      = $script:LuxConfig.BaseUrl
        local_http    = $localHttp
        model_ok      = $modelOk
        service_mode  = $script:LuxConfig.ServiceMode
        share         = $share
        revision      = @{ describe = $revision.describe; commit = $revision.commit }
        repo_path     = [System.IO.Path]::GetFullPath($script:LuxConfig.RepoPath)
        log_dir       = $script:LuxConfig.LogDir
    }
}

function Invoke-LuxDoctor {
    <# 主动诊断：本地服务、本地模型、公网入口、配置一致性、Git 可读性。 #>
    $status = Get-LuxStatusObject
    $problems = @()
    if ($status.status -eq 'port_conflict') { $problems += "端口 $($status.base_url) 被无关进程占用（$($status.detail)）" }
    if ($status.running -and -not $status.local_http) { $problems += "本地 HTTP 无响应：$($status.base_url)" }
    if ($status.running -and $status.model_ok -eq $false) { $problems += "本地模型（Ollama）不可用" }
    if ($status.share.enabled) {
        if (-not $status.share.url) {
            $problems += "公网分享已开启，但未在日志中找到 public URL"
        }
        else {
            $probe = Test-LuxPublicUrl -Url $status.share.url
            if (-not $probe.ok) {
                $problems += "公网入口失效（$($probe.reason)）：$($status.share.url)"
            }
            elseif (-not $probe.auth) {
                $problems += "公网入口未要求认证，存在暴露风险"
            }
        }
    }
    # 配置一致性：deploy.config 若仍有固定 BaseUrl 与 LUX_PORT 冲突，上面 Initialize 已抛错，此处不再重复。
    return @{ status = $status; problems = @($problems); healthy = ($problems.Count -eq 0) }
}

function Get-LuxLogTail {
    param([string]$Which = 'app', [int]$Lines = 50)
    $map = @{
        app     = Join-Path $script:LuxConfig.LogDir 'app.out.log'
        err     = Join-Path $script:LuxConfig.LogDir 'app.err.log'
        service = Join-Path $script:LuxConfig.LogDir 'service.log'
        deploy  = $script:LuxConfig.LogFile
    }
    $path = $map[$Which]
    if (-not $path -or -not (Test-Path -LiteralPath $path)) {
        return @()
    }
    return @(Get-Content -LiteralPath $path -Tail $Lines -Encoding UTF8)
}

function Adopt-LuxService {
    <# 接管一个已运行的本项目实例（写入统一状态文件）。-DryRun 只打印证据，不修改、不终止。 #>
    param([int]$ListenPid = 0, [switch]$DryRun)
    if ($ListenPid -le 0) {
        $portIdentity = Get-LuxPortIdentity
        if (-not $portIdentity) { throw "端口 $($script:LuxConfig.BaseUrl) 没有监听进程，无法接管。" }
        if (-not $portIdentity.owned) { throw "端口被无关进程占用（$($portIdentity.reason)），不能接管。" }
        $ListenPid = [int]$portIdentity.pid
    }
    $info = Get-LuxProcessInfo $ListenPid
    if (-not $info) { throw "找不到进程 PID $ListenPid" }
    if ($info.error) { throw "无法读取进程信息（$($info.error)），拒绝接管" }
    $owned = Test-LuxIsOwnedProcess $info
    if (-not $owned.owned) { throw "进程 PID $ListenPid 不确认属于本项目（$($owned.reason)），拒绝接管" }
    $launcherPid = [int]$info.parent_pid
    $frpc = Get-LuxDescendantPids -RootPid $ListenPid -NamePattern 'frpc%'
    $evidence = @{
        pid          = $ListenPid
        launcher_pid = $launcherPid
        created      = $info.created
        exe          = $info.exe
        frpc         = @($frpc)
    }
    if ($DryRun) {
        Write-LuxLog "[接管预览] 监听进程 PID $ListenPid，启动器 PID $launcherPid，frpc 后代：$(@($frpc) -join '、')" 'WARN'
        Write-LuxLog "[接管预览] 可执行路径：$($info.exe)" 'WARN'
        Write-LuxLog "[接管预览] 未修改任何状态"
        return $evidence
    }
    $state = @{
        version       = 1
        repo_path     = [System.IO.Path]::GetFullPath($script:LuxConfig.RepoPath)
        pid           = $ListenPid
        launcher_pid  = $launcherPid
        created       = $info.created
        host          = $script:LuxConfig.Host
        port          = $script:LuxConfig.Port
        base_url      = $script:LuxConfig.BaseUrl
        out_log       = (Join-Path $script:LuxConfig.LogDir 'app.out.log')
        err_log       = (Join-Path $script:LuxConfig.LogDir 'app.err.log')
        share_enabled = (Test-LuxShareEnabled)
        share_url     = $(if (Test-LuxShareEnabled) { Get-LuxShareUrl } else { $null })
        started       = $null
    }
    Write-LuxServiceState -State $state
    Set-Content -LiteralPath $script:LuxConfig.PidFile -Value $ListenPid -Encoding ASCII
    Write-LuxLog "已接管运行实例 PID $ListenPid（启动器 PID $launcherPid，frpc：$(@($frpc) -join '、')）"
    return $evidence
}

function Ensure-LuxOllama {
    if ($env:LUX_MODEL_MODE -eq 'external') { return $true }
    $chatUrl = $env:OLLAMA_URL
    if (-not $chatUrl) { $chatUrl = 'http://127.0.0.1:11434/api/chat' }
    $base = ($chatUrl -replace '/api/.*$', '')
    $tagsUrl = "$base/api/tags"
    try {
        Invoke-RestMethod -Uri $tagsUrl -TimeoutSec 3 | Out-Null
        return $true
    }
    catch { }
    $ollama = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Write-LuxLog "本地模型服务不可用，也未找到 ollama.exe（LUX_MODEL_MODE=local）" 'WARN'
        return $false
    }
    Write-LuxLog "正在启动本地 Ollama 服务"
    Start-Process -FilePath $ollama.Source -ArgumentList 'serve' -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod -Uri $tagsUrl -TimeoutSec 2 | Out-Null
            Write-LuxLog "本地模型服务已就绪"
            return $true
        }
        catch { }
    }
    Write-LuxLog "等待 Ollama 就绪超时" 'WARN'
    return $false
}

# ============================================================
# 发布状态 / 备份恢复（与生命周期解耦）
# ============================================================

function Get-LuxState {
    $path = $script:LuxConfig.StateFile
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json) }
    catch { return $null }
}

function Save-LuxState {
    param([hashtable]$State)
    $path = $script:LuxConfig.StateFile
    $dir = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $State | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding UTF8
}

function New-LuxBackup {
    param([string]$Note = '', [switch]$IncludeSecrets)
    $arguments = @('tools/backup_data.py', '--keep', "$($script:LuxConfig.BackupKeep)", '--json')
    if ($script:LuxConfig.BackupDir) { $arguments += @('--out', $script:LuxConfig.BackupDir) }
    if ($Note) { $arguments += @('--note', $Note) }
    if ($IncludeSecrets) { $arguments += '--include-secrets' }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $script:LuxConfig.PythonExe @arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "备份失败：$output" }
    $text = ($output | Where-Object { "$_" -match '^\s*\{' } | Select-Object -Last 1)
    try {
        $result = "$text" | ConvertFrom-Json
        Write-LuxLog "备份完成：$($result.path)"
        return $result.path
    }
    catch {
        throw "备份已执行，但无法解析结果：$output"
    }
}

function Restore-LuxData {
    param([string]$Archive)
    if (-not (Test-Path -LiteralPath $Archive)) { throw "找不到备份文件：$Archive" }
    Write-LuxLog "恢复数据：$Archive"
    Invoke-LuxPython @('tools/backup_data.py', 'restore', $Archive, '--force') '恢复数据' | Out-Null
}

Export-ModuleMember -Function Get-LuxRoot, Get-LuxConfig, Write-LuxLog, Resolve-LuxPath, `
    Import-LuxEnv, Get-LuxDefaults, Get-LuxConfigPath, Initialize-LuxEnvironment, `
    Invoke-LuxCommand, Invoke-LuxGit, Invoke-LuxPython, Invoke-LuxPythonCapture, `
    Get-LuxRevision, Test-LuxRepoClean, `
    Resolve-LuxRef, Get-LuxServicePid, Get-LuxPortOwner, Get-LuxPortIdentity, Get-LuxServiceState, `
    Get-LuxManagedInstance, Get-LuxProcessInfo, Test-LuxIsOwnedProcess, Test-LuxOwnershipEvidence, Get-LuxDescendantPids, `
    Enter-LuxServiceLock, Exit-LuxServiceLock, `
    Read-LuxServiceState, Write-LuxServiceState, Clear-LuxServiceState, `
    Ensure-LuxOllama, Start-LuxService, Stop-LuxService, Restart-LuxService, Adopt-LuxService, `
    Invoke-LuxServiceForeground, `
    Test-LuxHttp, Assert-LuxServiceStopped, Wait-LuxHealthy, `
    Test-LuxShareEnabled, Get-LuxShareUrl, Test-LuxPublicUrl, Get-LuxStatusObject, Invoke-LuxDoctor, Get-LuxLogTail, `
    Get-LuxState, Save-LuxState, New-LuxBackup, Restore-LuxData
