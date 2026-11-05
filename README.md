# LUX · H区安保本地 Agent

面向安保二部日常办公的本地 AI 助手。当前版本定位为“可试点的单机/局域网工具”，不是自动执行业务系统动作的无人值守 Agent。

> 当前功能地图、模块职责、重复实现整合及验证结果见 [本次项目分析报告](docs/LUX_REFACTOR_REPORT.md)。

## 已具备能力

- 岗位助手：周报、通知、巡检、门禁、监控异常、NPI 管控等专项技能。
- 本地模型优先：默认调用本机 Ollama；只有明确设置为 `external` 才使用 DeepSeek。
- 数据红线：输入检查、外网脱敏、输出复检、敏感文件在解析前拦截。
- 本地文件处理：DOCX、XLSX、PPTX、TXT、CSV、Markdown；Windows 可做离线 OCR。
- 部门提示词库：SQLite 共享模板、使用次数与工时估算。
- 输出：H区周报动态页数 PPT/Word、邮件格式整理与二次确认发送。
- 周报智能创作（内容智能化阶段）：事实清单编号、两阶段分析与页面策划、页面来源映射、数字/趋势/原因核验、Word/PPT 导出前二次检查。
- 审计：只记录时间、分类、长度和摘要哈希，不保存业务原文。
- 运行中心：查看模型在线状态、磁盘容量、任务耗时与最近活动。
- 结果反馈：记录“可用/需修改/不可用”、修改原因和实际人工耗时。
- 任务台账：对话与事件处置自动记录状态，但不保存业务输入和模型输出原文。
- 制度匹配引擎：事件事实提取、关键词与本地字符语义候选检索、适用条件校验、条款来源引用、缺失信息追问和低置信度转人工。
- 双维度研判：潜在后果“风险等级”和现场响应“紧急程度”分别计算并写入事件台账。

## 制度知识库

制度条款位于 `knowledge/policies/*.json`，与 Agent 代码完全分离。新增或修订制度时，只需依据受控原文维护制度 JSON，不需要改写 `agents/incident_agent.py`。

当前已登记以下目标制度，项目包已包含对应 PDF 原文，但事件研判使用的 `knowledge/policies/*.json` 仍为待导入状态、条款数为 0：

- `HD-KS-SW-010`《供应商安保管理办法》
- `HD-KS-SW-011`《ICT人员管理作业办法》

在两份受控原文的条款结构化导入并逐条核对前，事件研判会显示“未匹配到可引用条款”，所有通用安全建议均标记为需要人工确认。奖惩参考使用独立的 `knowledge/reward_penalty_rules.json`（本包 146 条规则），不能将其候选匹配视为事件研判已具备完整适用条件。JSON 结构、适用条件字段和维护规则见 `knowledge/policies/README.md`。

## 前端设计与维护规范

前端仍以 Gradio `Blocks` 为运行框架，页面和业务事件在 `app.py`，统一视觉样式维护在 `frontend/lux.css`，由 `core/ui_theme.py` 加载。不要再向 `app.py` 添加第二份 CSS。

- `docs/FRONTEND_DESIGN_SKILL.md`：前端设计系统、组件状态、响应式规则与验收清单。
- `docs/GRADIO_FRONTEND_STANDARD.md`：Gradio CSS、主题与选择器维护约定。
- `docs/archive/FRONTEND_QA_RESULTS.md`：历史前端专项检查记录，仅供参考。
- `docs/WEEKLY_REPORT_AGENT_CONTENT_INTELLIGENCE.md`：周报内容智能化阶段的设计、来源核验机制和限制。

异常事件处置 Agent 是首个前端样板页。修改样式后，先运行单元测试和 CSS 检查，再在实际浏览器中检查桌面与窄屏布局、滚动、遮挡、禁用/错误状态及控制台。

## 推荐部署形态

第一阶段建议安装在一台部门 Windows 专机：

```text
同事浏览器 → Gradio 网页 → 红线守卫 → 本机 Ollama → 结果复检
                         ↘ SQLite / 审计元数据
```

先以 `127.0.0.1` 单机试用。需要局域网共享时改为 `0.0.0.0`，并强制配置登录账号与强密码；同时由 IT 配置主机防火墙，只允许安保办公网段访问。

### 日常启停（统一入口）

日常操作统一走 `.\lux.ps1`，不要再手工跑 `start_web.ps1` 或 `deploy\lux-restart.ps1`：

```powershell
.\lux.ps1 start     # 启动（已运行则直接返回；端口被无关进程占用会报冲突，不换端口、不杀进程）
.\lux.ps1 stop      # 停止（先核实进程归属，绝不按名字批量杀 python）
.\lux.ps1 restart   # 重启
.\lux.ps1 status    # 状态（本机 HTTP / 公网分享 / 版本）
.\lux.ps1 logs      # 看日志（-Which app|err，-N 行数）
.\lux.ps1 doctor    # 健康诊断
.\lux.ps1 adopt     # 接管已在运行的本项目实例（先用 -DryRun 预演）
```

退出码：`0` 成功；`1` 失败；`2` 端口被无关进程占用；`3` 无法确认进程归属/权限不足；`4` 另一个管理命令正在运行（锁超时）。

端口以 `.env` 的 `LUX_HOST` / `LUX_PORT` 为准；`deploy\deploy.config.psd1` 里不要再写 `BaseUrl`，与 `LUX_PORT` 不一致会直接报错。旧脚本（`start_web.ps1`、`service.ps1`、部署/回退/自启）内部已改为复用同一套启动/停止/归属逻辑，不存在互相竞争的多个管理器。设计见 [docs/CLAUDE_UNIFIED_SERVICE_MANAGEMENT.md](docs/CLAUDE_UNIFIED_SERVICE_MANAGEMENT.md)。

### 更新与发布

代码走 Git 发布，业务数据和密钥留在仓库外。发布脚本在 [deploy/](deploy/)：

```powershell
.\deploy\lux-status.ps1            # 当前版本、服务、迁移、备份
.\deploy\lux-deploy.ps1 v1.3.0     # 备份 → 停服 → 切 tag → 迁移 → 起服 → 健康检查
.\deploy\lux-rollback.ps1 -To v1.2.9
.\deploy\lux-health.ps1
```

发布前自动备份、数据库结构变化通过 `tools/migrate.py` 迁移、健康检查失败自动回退。
完整说明见 [deploy/README.md](deploy/README.md)。

## Windows 快速安装

前提：Python 3.11 或 3.12、Ollama 已安装。

1. 在 PowerShell 中运行：

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\install.ps1
   ```

2. 拉取本地模型（示例）：

   ```powershell
   ollama pull qwen2.5:3b
   ```

3. 检查环境并启动：

   ```powershell
   .\healthcheck.ps1
   .\lux.ps1 start
   .\lux.ps1 status    # 会打印实际监听地址
   ```

4. 浏览器打开 `status` 打印的地址（默认 <http://127.0.0.1:7860>，以 `.env` 的 `LUX_PORT` 为准）。

## 配置

首次安装会从 `.env.example` 创建 `.env`。`start_web.ps1` 会自动加载它。

### 默认本地模型（推荐）

```dotenv
LUX_MODEL_MODE=local
OLLAMA_MODEL=qwen2.5:3b
LUX_HOST=127.0.0.1
```

### 公网模型（需部门明确批准）

```dotenv
LUX_MODEL_MODE=external
DEEPSEEK_API_KEY=sk-xxx
```

公网模式下，用户输入和技能系统提示词都会经过红线扫描与脱敏。身份证号、手机号、高敏文件仍直接拒绝。

### 局域网共享与个人账号

```dotenv
LUX_HOST=0.0.0.0
```

账号只保存在 `data/users.json`，密码为 PBKDF2 加盐哈希。旧版 `.env` 里的
`LUX_APP_USERNAME` / `LUX_APP_PASSWORD` 会在首次启动时迁移为 `admin` 角色的初始管理员；
**迁移完成后要把这两行删掉（或注释）**，不要把密码写回 `.env`。
所有同事必须使用个人账号，任务台账会记录发起账号。

防火墙只需放行 TCP 7860（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "LUX 安保助手 7860" -Direction Inbound -Protocol TCP -LocalPort 7860 -Action Allow
```

只要能连上端口就能看到登录页；**页面内容和登录密码在内网是明文 HTTP 传输**。
只应在安保办公网段内开放；需要加密时按 [docs/DEPLOY_HTTPS.md](docs/DEPLOY_HTTPS.md)
用 Caddy 反向代理，并把 `LUX_HOST` 改回 `127.0.0.1`、只放行 443。
服务启动后实际可用的局域网地址会打印在 `logs\service.log` 里。

管理员在服务器 PowerShell 中管理账号（密码采用隐藏式交互输入，不进入命令历史）：

```powershell
# 新增普通用户
.\.venv\Scripts\python.exe .\manage_users.py add zhangsan --name "张三/工号" --role operator

# 新增审核员（可查看并处理人工审核原件）
.\.venv\Scripts\python.exe .\manage_users.py add lisi --name "李四/工号" --role reviewer

# 列出账号、重置密码、停用离职账号
.\.venv\Scripts\python.exe .\manage_users.py list
.\.venv\Scripts\python.exe .\manage_users.py reset-password zhangsan
.\.venv\Scripts\python.exe .\manage_users.py set-enabled zhangsan false
```

角色权限：`operator` 使用助手与事件处置；`reviewer` 增加受控原件审核；`supervisor` 增加共享模板管理、团队看板和邮件外发；`admin` 拥有全部权限。程序会拒绝“局域网监听但没有任何启用账号”的危险配置。

### 邮件

在 `config/email_config.json` 填写 SMTP 地址和发件邮箱；授权码只放 `.env`：

```dotenv
LUX_SMTP_PASSWORD=邮箱授权码
```

邮件按钮采用内容指纹确认：收件人、抄送、主题或正文任一项发生变化，都必须重新确认。

### 知识来源：本地资料 / 联网搜索 / 综合检索

智能工作台输入框下方有一个来源按钮，点击在三种模式间循环：

| 模式 | 行为 |
| --- | --- |
| 本地资料（默认） | 只查已授权的文件库，不产生任何公网请求。 |
| 联网搜索 | 本轮提问发往搜索服务；如果意图是找本地文件，本地检索照常执行（联网不再排斥本地）。 |
| 综合检索 | 同时取本地文件正文和联网证据，合成一次回答；本地侧必须真正读到正文才参与分析。 |

综合检索与联网模式下的本地检索，都会对匹配到的文件调用权限校验后读取正文（`PdfLibrary.read_text`）。**只拿到文件名或目录卡片时不会分析**：读不到正文的文件会在提示词与回答里明确标注“无可提取正文”，回答也不会据此推测内容。本地来源以 `[L1]`、`[L2]` 标注并列出文件名与页码，联网来源以 `[S1]`、`[S2]` 标注并列出链接；两类编号都会做“是否引用了不存在的来源”校验。

服务端需先开启并配置密钥（密钥只放 `.env`，不进代码库）：

```dotenv
LUX_WEB_SEARCH_ENABLED=true
LUX_WEB_SEARCH_PROVIDER=tavily
TAVILY_API_KEY=你的 Tavily 密钥
LUX_WEB_SEARCH_TIMEOUT=10
LUX_WEB_SEARCH_MAX_RESULTS=4
```

开启后，打开联网搜索开关的提问会先经过红线外发扫描（`guard.check_outbound`）；区域、客户、单位等脱敏级专名会先替换成占位符再发往搜索服务；返回的网页内容按条数、字数截断后作为“未经验证的外部证据”注入模型，并在回答末尾以 `[S1]`、`[S2]` 形式列出真实来源链接。回答中出现的引文编号会在服务端校验，未检索到的编号会被标注提示，防止模型编造来源。运行 `healthcheck.py --web-search` 可发起一次真实连通性探测（会产生付费请求，仅验收时使用）；不带参数则只打印配置状态，不产生请求。

## 红线规则维护

编辑 `config/redline_rules.json`：

- `sensitive_terms`：外网调用前自动替换的客户、区域、单位代号；
- `regex_patterns`：身份证、手机号、工号、邮箱等格式规则；
- `block_terms`：粘贴文本中出现即拒绝的高敏描述；
- `file_rules.block_filename_keywords`：在文件解析前即拒绝的文件名关键词。

规则变更后运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

## 目录

```text
app.py                 网页入口
core/providers.py      Ollama / DeepSeek 模型路由
core/redline_guard.py  输入、脱敏、输出、审计
core/ingest.py         文件先检查后解析
core/skills.py         岗位技能
core/db.py             提示词库与用量统计
core/policy_engine.py  制度知识库、事实提取、候选检索与适用条件校验
core/incident_store.py 事件闭环台账（含风险、紧急程度与条款引用）
agents/urgency_agent.py 现场紧急程度研判
knowledge/policies/    与代码分离的制度条款数据
config/                红线、邮件格式配置（不放密钥）
templates/             周报规范
tests/                 核心自动测试
docs/                  架构与上线建议
deploy/                发布、回退、健康检查脚本（见 deploy/README.md）
migrations/            数据库迁移（按库分目录，0001_baseline.sql 为基线）
tools/                 迁移与备份恢复命令行
```

## 边界

- 目前没有对接门禁、监控、排班等生产系统，不会自动开门、锁岗或下发指令。
- 当前项目包包含 `HD-KS-SW-010/011` PDF；事件研判知识库仅登记制度元数据，不含结构化生产条款。奖惩参考库独立存在，两者尚未统一来源治理。
- 词表和正则不能替代企业 DLP；试点期必须保留人工复核。
- 工时节省是估算值，不应直接作为绩效结论。
- 正式部门级部署前，应由 IT/信息安全完成终端加固、备份、账号权限、漏洞扫描和日志留存周期审批。

## 周报 Agent V8：一次语义分析 + 证据编译

周报入口是 `app.py::_structure_ppt_source`，实际流程由 `core/weekly_report_engine.py` 的 `WeeklyReportEngine` 执行，分三段：

1. **素材理解**：`extract_weekly_facts` 把原文切成带来源行号、分类和数量的事实台账。
2. **AI 语义分析**：整份周报只调用一次模型，产出页面策划与语义摘要；本地模式下页面骨架按素材覆盖的事实类别生成，页数随素材变化（封面/内容页/结束页）。
3. **事实证据编译**：`compile_grounded_weekly_page` 用事实台账生成正文，最后做结构、来源与质量三重核验。任何未出现在素材里的数字、指标或结论都会被拦下。

模型调用从早期“逐页生成”改为整份语义分析；正常调用一次，结构或覆盖校验失败时允许一次修复重试。模型失败时只保留诊断预览，不会把本地规则草稿当作正式成品；导出前还会检查提示词泄漏、待补充占位符、统计周期和图表数据来源。

历史版本说明（v4、v4.1 及更早的逐页生成流程）已归档在 `docs/archive/`，不再代表当前实现。设计细节见 `docs/WEEKLY_REPORT_AGENT_CONTENT_INTELLIGENCE.md`。

## PPT 导出与逐页预览

每页只有一个图表区，图表由 `core/ppt_pipeline.py` 生成原生可编辑图表对象（column/bar/line/pie），数值不会被从正文里猜出来。每次导出会在 PPT 同目录额外产出：

- `NAME.qa.json`：页数、图表数、字体是否安装、文字溢出与区域重叠提醒、预览状态
- `NAME.preview/`：`slide-NNN.png` 逐页截图与 `index.html`

预览通过独立的 PowerShell 进程调用本机 PowerPoint，避免 COM 卡死或占用用户已打开的演示文稿。机器上没有 PowerPoint 时 PPT 仍会正常保存，`qa.json` 标明 `preview_status: unavailable` 并给出原因。批量测试或无 Office 环境设 `LUX_SKIP_PPT_PREVIEW=1` 跳过预览（单元测试已默认关闭）。

样式集中在 `config/ppt_style.json`（字体、标题/正文/表格/图表字号），改样式不需要动代码；字体需在导出和查看的机器上都已安装。溢出与重叠检查是启发式的，不能替代人工查看逐页预览。
