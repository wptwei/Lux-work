# 制度知识库

每份制度使用一个 UTF-8 JSON 文件，Agent 代码不保存业务条款。生产条款必须逐条核对受控原文后录入；没有原文时保持 `clauses` 为空，并将 `source_status` 设为 `awaiting_source_text`。

最小结构：

```json
{
  "document": {
    "code": "制度编号",
    "title": "制度名称",
    "version": "受控版本",
    "source_file": "原文文件名",
    "source_status": "ready"
  },
  "clauses": [
    {
      "id": "唯一条款标识",
      "clause_no": "原文条款号",
      "title": "原文条款标题",
      "text": "经核对的原文条款内容",
      "keywords": ["用于候选召回的词"],
      "semantic_terms": ["同义表达"],
      "required_facts": ["approved"],
      "conditions": [
        {"fact": "subject_types", "operator": "contains", "value": "supplier"}
      ],
      "questions": {"approved": "是否已经完成审批？"},
      "actions": ["原文明确规定的动作"]
    }
  ]
}
```

支持的条件操作符：`equals`、`not_equals`、`contains`、`intersects`、`in`。未匹配、缺少适用事实、低置信度或制度源缺失时，系统只会追问或转人工确认。
