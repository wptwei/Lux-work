# LUX H区安保本地Agent v0.5

在 v0.4 单 Agent 网页助手的基础上，新增多 Agent 事件处置框架：

- Supervisor Agent：按领域关键词分类并分发（巡检优先，其次事件）。
- 多 Agent 基础框架：`agents/base.py` 统一注入红线守卫与模型调用，离线时确定性回退。
- Workflow 闭环：`identify → risk → action → archive`。
- 风险 Agent：关键词分级加权评分（critical / high / medium / low / unknown）。
- 审计日志：只写元数据与摘要哈希，不保存业务原文。

## 主要文件

```text
core/orchestrator.py     编排入口：Supervisor + Workflow + 红线 + 审计
core/supervisor.py       任务分类与分发
core/workflow.py         四阶段闭环
agents/base.py           多 Agent 基类
agents/risk_agent.py     风险评分
agents/incident_agent.py 事件处置
agents/patrol_agent.py   巡检处置
agents/report_agent.py   Markdown 报告生成
audit/audit_logger.py    审计日志（JSONL）
demo_agent.py            命令行演示入口
tests/test_v05_agents.py 多 Agent 自动测试
```

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

## 命令行演示

需要本机 Ollama 已启动，且已配置好 `.env`：

```powershell
.\.venv\Scripts\python.exe demo_agent.py "有人闯入H区东门"
```

未装模型时，各 Agent 仍可离线回退：Supervisor 分类、风险评分、动作选择和报告生成均为确定性逻辑。

## 网页入口

启动 `start_web.ps1` 后，在网页里打开「🛡️ 事件处置」标签页即可使用同一闭环。

## 界面与体验

- 流式输出：回答逐字显示，等待期间先显示「⏳ 正在生成…」，不再像卡死。
- 本地模型预热：启动后在后台先加载一次模型，降低首次提问的等待。
- 模型保温：请求带 `OLLAMA_KEEP_ALIVE`（默认 30 分钟），减少反复加载。
- 统一深蓝主题，与工时看板配色一致。
- 对话区带欢迎引导；右侧提示词库折叠收纳；数据红线改为独立安全卡片。
- 事件处置结果结构化展示：风险徽章 + 处理 Agent + 阶段轨迹。
- PPT 导入：支持上传 `.pptx`，按页提取文字与备注，作为素材交给 AI 处理。
- PPT 模板导出：上传 `.pptx` 模板后，可按模板样式导出周报，支持两种方式：
  - 模板里写了 `{{P1}}`～`{{P8}}` / `{{标题}}` 占位符：原地替换，完整保留模板设计；
  - 模板里没有占位符：沿用模板的母版、主题与版式，重排为 8 页。

## 环境提示

- 本机若开了系统代理（如 Clash），需要在 `.env` 里保留
  `NO_PROXY=127.0.0.1,localhost`，否则本机地址会被代理拦断。
- 纯 CPU 推理时首次响应较慢，建议先等模型预热完成再使用。

## 模型选择说明

当前固定使用 `qwen2.5:3b`，不再使用 qwen3 系列，原因：

- qwen3 默认开启「思考模式」，每次回答会先生成 500～950 个隐藏推理 token；
- 本机无独立显卡（约 5.5 token/秒），一条「你好」要跑 3～4 分钟，体验等同于不可用；
- 换成不带思考链的 `qwen2.5:3b` 后，同一句 **4.7 秒**返回，提速约 50 倍。

实测对比（同一台机器、同一句话）：

| 模型 | 首字延迟 | 完整回答 | 生成 token |
|------|---------|---------|-----------|
| qwen3:4b | 246 秒 | 247 秒 | 955 |
| qwen2.5:3b | 2.6 秒 | 4.7 秒 | 约 25 |

以后若换到带独立显卡的机器，可以再考虑 qwen3 系列或更大的模型。
模型相关参数在 `.env`：`OLLAMA_MODEL`、`OLLAMA_NUM_CTX`、`OLLAMA_NUM_PREDICT`、`OLLAMA_KEEP_ALIVE`。

## 局域网共享（让同事也能用）

前提：大家在同一个办公网络；这台主机保持开机、不休眠。

1. 编辑 `.env`，把下面三行取消注释并换成强密码：

   ```dotenv
   LUX_HOST=0.0.0.0
   LUX_APP_USERNAME=security
   LUX_APP_PASSWORD=换成强密码
   ```

2. 重新运行 `start_web.ps1`，终端会打印：

   ```text
   本机访问：http://127.0.0.1:7860
   同事访问：http://192.168.x.x:7860
   登录账号：security
   ```

3. 给同事访问地址放行防火墙（需管理员 PowerShell，执行一次即可）：

   ```powershell
   New-NetFirewallRule -DisplayName "LUX 安保助手 7860" -Direction Inbound `
     -Protocol TCP -LocalPort 7860 -Action Allow
   ```

4. 同事用浏览器打开「同事访问」那个地址，输入账号密码即可使用。

注意事项：

- 电脑一旦休眠或关机，同事就连不上了，建议把电源设置改成「从不睡眠」。
- 所有请求都由这台电脑的模型算力承担，多人同时用会排队变慢，建议错峰使用。
- 同事浏览器若装了代理，请把 `192.168.x.x` 这类内网地址设为直连/加入白名单。
- 只有设置了账号密码才会启用登录；未设置的局域网监听会被程序直接拒绝启动。

## 当前共享配置（已在本机部署）

| 项目 | 值 |
|------|-----|
| 访问地址（有线） | `http://192.168.1.4:7860` |
| 访问地址（无线） | `http://10.145.212.12:7860` |
| 登录账号 | `security` |
| 登录密码 | 见 `.env` 的 `LUX_APP_PASSWORD` |
| 防火墙规则 | `LUX 安保助手 7860`（入站 TCP 7860 允许） |
| 开机自启任务 | `LUX-H区安保助手`（登录时自动启动） |

注意：本机是双网卡（有线 192.168.1.x / 无线 10.145.x），
启动时会打印全部可用地址，**按同事所在网段选对应那个**。
IP 由 DHCP 分配，换网络后可能变化，以启动时打印的地址为准。

维护命令：

```powershell
# 停止服务
Get-Process python | Where-Object { $_.Path -like "*lux_v04*" } | Stop-Process

# 手动重启
cd "D:\vscode project\LUX\LUX_H区安保本地Agent_v0.5\lux_v04"; .\start_web.ps1

# 取消开机自启（如不需要）
Unregister-ScheduledTask -TaskName "LUX-H区安保助手" -Confirm:$false

# 关闭局域网共享（删防火墙规则并把 LUX_HOST 改回 127.0.0.1）
Remove-NetFirewallRule -DisplayName "LUX 安保助手 7860"
```

维护提醒：`start_web.ps1` 读取 `.env` 时必须用 `-Encoding UTF8`。
否则中文系统会把 UTF-8 的中文注释读成乱码并吞掉换行，导致后面那行配置被当成注释跳过
（曾导致 `LUX_HOST=0.0.0.0` 静默失效）。
