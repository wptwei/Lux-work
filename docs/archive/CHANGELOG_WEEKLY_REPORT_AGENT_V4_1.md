# 周报 Agent v4.1 更新记录（2026-10-22）

## 目标

针对实际生成稿出现的指令泄漏、混合素材分类错误、运营/异常图表缺失和模型失败后静默兜底等问题，优先修复模型调用、素材理解与内容质量检查。

## 主要修改

- `app.py`：周报生成改为“结构化策划 + 逐页 AI 正文”；模型失败保留诊断预览，不再自动生成可导出的规则兜底正文；导出前检查生成状态、事实来源和正文质量。
- `core/weekly_report_agent.py`：优化按行/章节/指标/日期拆分事实；识别背景信息和用户写作指令；逐页提示词、页面级事实核验、占位符/指令泄漏检查、图表来源数值校验。
- `core/providers.py`：支持周报任务指定 `num_predict` / `max_tokens`，降低单次长输出截断风险。
- `tests/test_weekly_report_agent.py`：新增混合素材分类、指令过滤、逐页生成失败拦截、图表数据来源和导出质量闸门测试。

## 验证

- `python -m compileall -q app.py core tests`：通过。
- `python -m unittest discover -s tests -q`：128 项测试通过。
- 真实 Windows + Ollama 尚未连接，本次模型流程由结构化响应模拟测试覆盖，不代表真实模型质量已经验收。
