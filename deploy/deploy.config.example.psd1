# 复制为 deploy/deploy.config.psd1 后按服务器实际情况修改。
# 该文件含本机路径与任务名，已被 .gitignore 排除，不会进入代码仓库。
@{
    # 代码仓库根目录（Git 工作区）；相对路径按 deploy\ 的上一级解析
    RepoPath = '.'

    Remote = 'origin'
    # 仅作说明用；发布目标由 lux-deploy.ps1 的 -Ref 指定
    Branch = 'main'

    # Python 运行时（相对 RepoPath）
    PythonExe = '.\.venv\Scripts\python.exe'

    # 服务托管方式（二选一，不能同时启用两个守护者）：
    #   'Pid'           = 由 lux.ps1 / service.ps1 用 Start-Process 拉起 app.py（推荐）
    #   'ScheduledTask' = 由 Windows 计划任务前台托管（沿用 service.ps1），需填 TaskName
    ServiceMode = 'Pid'
    TaskName    = 'LUX Web'

    # 监听地址与端口一律以 .env 的 LUX_HOST / LUX_PORT 为准，无需在此配置。
    # 旧版的 BaseUrl 已废弃：若仍显式写死一个与 LUX_PORT 不同的端口，会直接报错，
    # 避免静默检查错误的端口。请删除该项。

    # 起服后等待健康的秒数（冷启动 + Ollama 就绪可能较慢）
    HealthTimeoutSec = 120

    # 备份目录与保留份数（0 表示不自动清理）
    BackupDir = ''
    BackupKeep = 10

    # 发布时是否先跑测试集（约半分钟）；紧急发布可用 -SkipTests 跳过
    RunTests = $true

    # 是否允许直接用分支名/hash 发布（默认只接受 tag）
    AllowBranchRefs = $false

    # 备份是否包含 .env（含密钥）。默认 false，只备份数据与 config
    IncludeSecretsInBackup = $false

    # 日志与状态文件；留空时默认 <RepoPath>\logs
    LogDir    = ''
    StateFile = ''
    LogFile   = ''
    PidFile   = ''
}
