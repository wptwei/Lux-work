# 部署与更新

代码只通过 Git 发布；业务数据、运行配置和密钥都在代码仓库之外（或至少在 Git 忽略范围内）。
更新时先备份、再切版本、再迁移数据库、再起服务、最后做健康检查；任一步失败自动回退。

## 日常启停（统一入口）

日常的启动/停止/重启/状态/日志/诊断统一走仓库根目录的 `.\lux.ps1`：

```powershell
.\lux.ps1 start     # 启动（幂等；端口被无关进程占用会报冲突，不换端口、不杀进程）
.\lux.ps1 stop      # 停止（先核实进程归属，绝不按名字批量杀 python）
.\lux.ps1 restart   # 重启
.\lux.ps1 status    # 状态（-Json 输出机器可读）
.\lux.ps1 logs      # 看日志（-Which app|err，-N 行数）
.\lux.ps1 doctor    # 健康诊断
.\lux.ps1 adopt     # 接管已在运行的本项目实例（先用 -DryRun 预演）
```

退出码：`0` 成功；`1` 失败；`2` 端口被无关进程占用；`3` 无法确认进程归属/权限不足；`4` 另一个管理命令正在运行（锁超时）。

端口以 `.env` 的 `LUX_HOST` / `LUX_PORT` 为准；`deploy\deploy.config.psd1` 不要再写 `BaseUrl`，与 `LUX_PORT` 不一致会直接报错。`lux-deploy.ps1` / `lux-restart.ps1` / `service.ps1` / `start_web.ps1` 内部都复用 `deploy\LuxDeploy.psm1` 的同一套启动/停止/归属逻辑，不存在互相竞争的多个管理器。设计见 [../docs/CLAUDE_UNIFIED_SERVICE_MANAGEMENT.md](../docs/CLAUDE_UNIFIED_SERVICE_MANAGEMENT.md)。

## 目录职责

| 位置 | 内容 | 是否进 Git |
| --- | --- | --- |
| 仓库 | 代码、迁移脚本、部署脚本 | 是 |
| `LUX_DATA_DIR`（默认 `data/`） | SQLite 台账、文件库原件、用户文件 | 否 |
| `LUX_CONFIG_DIR`（默认 `config/`） | 邮件格式、红线规则、区域策略等运行配置 | 否（首次由程序生成示例） |
| `LUX_LOG_DIR`（默认 `logs/`） | 服务日志、发布日志、发布状态 | 否 |
| `LUX_ENV_FILE`（默认 `.env`） | 密钥与开关 | **绝不** |
| `backups/`（可配置） | 更新前备份 | 否 |

三个目录变量写在 `.env` 或系统环境变量里即可；不设置时行为与以前完全一致。
`python -c "from core.paths import describe; print(describe())"` 可查看实际路径，健康检查也会打印。

## 一次性准备

1. 服务器上装好 Python 3.12 与 Ollama，运行 `.\install.ps1`（或手动创建 `.venv` 并 `pip install -r requirements.txt`）。
2. 复制 `.env.example` 为 `.env`，填写模型、联网搜索等密钥；如需把数据放到仓库外，设置 `LUX_DATA_DIR` 等变量。
3. 复制 `deploy\deploy.config.example.psd1` 为 `deploy\deploy.config.psd1` 并按实情修改（该文件已被 Git 忽略）。
4. 确定托管方式：
   - **登录自启 + PID 托管（推荐）**：`.\deploy\install-startup.ps1` 在“启动”文件夹放一个快捷方式，
     登录后自动运行 `service.ps1`；部署脚本用 PID/端口直接起停（`ServiceMode = 'Pid'`）。
     不经过计划任务，不受安全策略拦截，也不需要管理员。
   - **计划任务**：见下方“用计划任务托管”，配置写
     `ServiceMode = 'ScheduledTask'; TaskName = '<任务名>'`。适合无人值守服务器，
     但本机实测计划任务启动会被间歇性拒绝（2147942405）。
   - 如果服务是手工在终端里 `.\start_web.ps1` 起的，部署脚本会拒绝执行（端口仍被占用），
     请先把服务纳入上面两种方式之一。
5. 首次检查：`.\deploy\lux-status.ps1`、`.\deploy\lux-health.ps1`。

### 用登录自启托管（推荐）

```powershell
.\deploy\install-startup.ps1                       # 当前用户登录后自动启动
.\deploy\install-startup.ps1 -Uninstall            # 取消登录自启
```

配置里保持 `ServiceMode = 'Pid'`。登录时如果服务已在运行（比如刚由部署脚本启动），
`service.ps1` 会检测到端口占用并立即退出，不会起第二个实例。
启停由 `lux-restart.ps1` / `lux-deploy.ps1` 通过 PID 与端口占用者完成。

### 用计划任务托管

> 任务名建议用**纯英文**（如 `LUX Web`、`LUX-H-Agent`），避免非 ASCII 名称带来的编码问题。

```powershell
# 新建（当前用户登录后自动启动，无需管理员、无需密码）
.\deploy\install-task.ps1 -TaskName 'LUX-H-Agent'

# 已存在的同名任务：改为指向本仓库的 service.ps1（会打印原目标）
.\deploy\install-task.ps1 -TaskName 'LUX Web' -Force

# 服务器无人值守：注册为开机即运行，需输入该账号密码（计划任务安全保存）
.\deploy\install-task.ps1 -TaskName 'LUX Web' -RunWhetherLoggedOn

# 删除任务
.\deploy\install-task.ps1 -TaskName 'LUX Web' -Uninstall
```

注册后在配置里写 `ServiceMode = 'ScheduledTask'; TaskName = 'LUX Web'`。
切换时先停掉手工运行的服务，再 `Start-ScheduledTask -TaskName 'LUX Web'`，最后跑
`.\deploy\lux-health.ps1` 确认。任务以隐藏窗口运行，输出仍写入 `logs\service.log`。

## 日常发布（手动）

```powershell
# 先看现状
.\deploy\lux-status.ps1

# 预演：只检查版本、迁移与备份设置，不改动任何东西
.\deploy\lux-deploy.ps1 v1.3.0 -DryRun

# 正式发布
.\deploy\lux-deploy.ps1 v1.3.0
```

发布步骤（`lux-deploy.ps1`）：

1. 校验工作区干净、`git fetch --tags`、解析目标版本（默认只接受 tag）；
2. 停止服务，并确认端口真的不再响应（否则中止，避免边写边换代码）；
3. 备份数据与配置到 `backups\`（`-SkipBackup` 可跳过，不推荐）；
4. `git checkout --force <tag>`；
5. `requirements*.txt` 有变化时安装依赖（library/voice 依赖仍需各自的安装脚本）；
6. `python tools/migrate.py up` 应用数据库迁移；
7. 跑测试集（`RunTests = $true` 时；`-SkipTests` 可跳过）；
8. 启动服务，等 Web 端口可用并跑 `healthcheck.py`；
9. 写发布记录 `logs\deploy-state.json`。

任一步失败：退回上一个提交；若本次已应用迁移，并用第 3 步的备份整库恢复；再启动服务并复查健康。
`-NoRollback` 可关闭自动回退（排障时用）。自动回退也失败时会明确报错，按“排障”一节人工处理。

其它命令：

```powershell
.\deploy\lux-restart.ps1                  # 只重启，不动代码
.\deploy\lux-health.ps1                   # 健康检查，退出码 0 = 正常
.\deploy\lux-backup.ps1                   # 立即备份
.\deploy\lux-backup.ps1 -List
.\deploy\lux-backup.ps1 -Restore .\backups\lux-backup-20261031-090000.zip -Force
```

## 版本回退

```powershell
.\deploy\lux-rollback.ps1 -List                     # 上次发布、可用备份、最近 tag
.\deploy\lux-rollback.ps1 -To v1.2.9                # 只退代码，数据不动
.\deploy\lux-rollback.ps1 -To v1.2.9 -RestoreData .\backups\lux-backup-20261031-090000.zip
```

回退前会先备份当前数据。**不做向下迁移**：新版本加过的列/表留在库里通常不影响旧代码；
如果新版本做过破坏性改动（重命名、删除列），必须同时用 `-RestoreData` 恢复对应备份。

## 数据库迁移

- `migrations/<库别名>/NNNN_说明.sql`，每个 SQL 文件只做一件事；
- 新库：`tools/migrate.py up` 执行全部迁移并记录到该库的 `schema_migrations` 表；
- 旧库（已有业务表、但没有迁移记录）：自动“认领”`0001_baseline.sql` 并只登记不执行，
  之后再执行更新的迁移；
- **新增表/列必须写成新的迁移文件**，不要只改本文的基线——旧库不会重跑基线；
- 已应用的迁移文件不要再改，改了会因校验和变化被拒绝执行（确认无误才用 `--allow-drift`）。

```powershell
.\.venv\Scripts\python.exe tools\migrate.py status
.\.venv\Scripts\python.exe tools\migrate.py up --dry-run
.\.venv\Scripts\python.exe tools\migrate.py up
```

## 备份与恢复

- 数据库用 SQLite 备份 API 复制（含 WAL 中未落盘的数据），天然一致；
- 备份内容：数据目录下的所有库与文件（含 PDF 原件）、`config/` 运行配置、清单 `manifest.json`；
- `.env` 默认**不**备份；确认要把密钥一起备份时设置 `IncludeSecretsInBackup = $true` 或加 `-IncludeSecrets`；
- 自动保留最近 `BackupKeep` 份；
- 恢复默认拒绝覆盖已有文件，需显式 `-Force`；恢复前请先停服。

## 排障

| 现象 | 处理 |
| --- | --- |
| 发布提示“端口已被占用（不是本脚本启动的实例）” | 服务是手工启动的：先停止，或改用计划任务托管并在配置里填 `TaskName` |
| 计划任务“无法启动任务…错误值 2147942405（拒绝访问）” | 本机为间歇性问题（与任务名、运行条件无关），重注册可临时恢复。建议改用登录自启：`.\deploy\install-startup.ps1`，把配置改回 `ServiceMode = 'Pid'`，并删除计划任务 |
| 发布回退后健康检查仍失败 | 看 `logs\app.err.log`、`logs\service.log`、`python healthcheck.py` 的 `[FAIL]` 项 |
| 迁移校验和变化 | 有人改过已应用的 SQL；确认后 `tools\migrate.py up --allow-drift` |
| 想手工恢复 | 停服 → `tools\backup_data.py restore <zip> --force` → 起服 → `lux-health.ps1` |
| 数据目录被改到别处 | 检查 `.env` 的 `LUX_DATA_DIR` 与 `lux-status.ps1` 打印的实际路径是否一致 |

## 尚未覆盖（后续接入自动部署时再补）

- 没有 CI/自动发布：目前是手动执行 `lux-deploy.ps1`；
- 没有蓝绿/双实例：发布期间服务会短暂不可用（备份 + 切换 + 启动，通常几十秒）；
- 没有对前端静态资源的独立版本号；浏览器缓存由 Gradio 自身处理；
- 计划任务模式下的“停止”依赖 `Stop-ScheduledTask`，若任务里还有子进程，需确认端口已释放（脚本会检查）。
