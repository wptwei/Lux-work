# LUX V3 异常事件 Agent · 奖惩匹配精简版

## 本次改动

- 仅在「事件中心」展示：一处现场描述、匹配按钮、奖惩引用结果及后台记录。
- 新增 `knowledge/reward_penalty_rules.json`：从用户提供的两份受控制度中抽取 146 条奖惩候选索引，附文件编号、版本及 PDF 页码；未经业务复核不能直接用于执行处分。
- 新增 `core/reward_penalty_engine.py`：本机文本匹配，不调用 LLM 或外部 API。
- 新增 `core/reward_penalty_store.py`：在原 `data/incidents.db` 中追加 `reward_penalty_records` 数据表，兼容原来的事件表。
- `app.py` 中保留旧的事件 Agent 实现，但在前台隐藏其复杂表单，避免破坏历史模块调用。

## 使用方法

PowerShell 在解压根目录执行：

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开显示出的 Gradio 地址，切换「事件中心」，输入现场描述，点击「匹配奖惩并自动记录」。
后台 SQLite 文件：`data/incidents.db`，表：`reward_penalty_records`。

## 重要业务边界

- 匹配结果是 **待核实候选条款**，不是最终处罚决定。
- 供应商金额受违规次数、严重程度和主管核定影响；ICT 处分受累计次数、行为结果和适用对象影响。输入描述不够具体时，须查实后才能下结论。
- **来源文档本身有重号/错号**（如 `6.5.7.2.21`），现索引保留原文编号和 PDF 页码，发布前应由制度责任人核对纠错。
- 数据库仅保存脱敏后的事件描述及本次候选匹配，不储存姓名工号等识别信息；必须遵守红线设置。
- 高风险条款尚需补充人工精校关键词、交叉验证和历史次数查询，不应自动生成实际罚款、处分决定。
