# 管理制度匹配引擎变更说明

## 本次实现

- 新增 `core/policy_engine.py`：从独立 JSON 制度库加载条款，完成自然语言事实提取、关键词召回、本地字符语义相似度、适用条件校验、来源引用、缺失事实追问和低置信度转人工。
- 新增 `agents/urgency_agent.py`：把“潜在后果风险”与“现场响应紧急程度”分开计算。
- 扩展 `agents/incident_agent.py`：制度匹配成为事件处置主流程；制度没有明确动作时，仅输出明确标注为需要人工确认的通用安全建议。
- 扩展 `core/incident_store.py`：自动迁移旧事件数据库，并写入事实、候选条款、来源引用、缺失信息、制度置信度、人工复核标记和紧急程度。
- 更新 `app.py`：事件结果展示制度来源、条款原文、追问、转人工原因、风险与紧急程度；事件台账同步展示制度依据。
- Gradio Tabs 不再传递版本差异参数，沿用 CSS 控制标签布局；未修改 Gradio 依赖范围。
- 新增制度知识库目录和维护说明：`knowledge/policies/`。

## 制度原文状态

收到的 `lux_v04.zip` 以及相邻项目目录中没有 `HD-KS-SW-010`、`HD-KS-SW-011` 原文。为遵守“严禁虚构条款”，两份制度只登记编号和名称，`clauses` 保持为空，状态为 `awaiting_source_text`。

在逐条核对受控原文并录入前，系统会：

- 不输出虚构条款、条款号或制度动作；
- 显示未匹配到可引用条款；
- 将通用安全建议标记为人工确认；
- 把制度原文缺失和低置信度写入转人工原因。

## 运行

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
.\healthcheck.ps1
.\start_web.ps1
```

测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 测试覆盖

- 关键词与字符语义候选检索；
- 自然语言事实抽取及否定状态；
- 条款适用条件和定向追问；
- 来源制度、条款号和原文引用；
- 无关事件不生成制度动作；
- 制度源缺失时强制转人工；
- 风险与紧急程度分离；
- 旧版事件数据库原位迁移；
- 制度匹配结果写入事件台账且不保存事件原文。
