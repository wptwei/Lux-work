# LUX 周报 Agent v3：PPT 模板智能映射

本版本在 v2 两阶段内容策划和事实来源核验基础上，增加历史 PPT 模板识别、文本/表格映射、结构化动态图表与导出诊断。

## 启动

在项目根目录执行：

```powershell
python -m unittest discover -s tests -q
python app.py
```

## 建议操作

1. 先备份当前工作台。
2. 上传真实历史周报 `.pptx`，检查识别到的页数、文本框、表格、图表和图片数量。
3. 输入本期素材，生成周报草稿并确认来源。
4. 在 P3/P4/P5 需要结构化渲染时，按 `docs/PPT_TEMPLATE_ENGINE_V2.md` 的格式提供 `表格数据：` 或 `图表数据：` 块；只有源数据充分时才使用。
5. 选择“历史周报底版”生成，并查看导出提示中的更新数量和警告。
6. 在 PowerPoint 中逐页检查字体、溢出、静态图片旧数据及未映射的特殊元素。

## 本轮重点文件

- `core/ppt_template.py`
- `core/ppt_export.py`
- `core/weekly_report_agent.py`
- `app.py`
- `config/weekly_ppt_template_map.json`
- `tests/test_ppt_template_engine.py`

注意：本项目包未包含用户的真实历史 PPT 底版。本轮使用合成模板验证渲染逻辑；真实模板的精确映射仍需基于实际文件配置。
