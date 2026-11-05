# 历史文档归档

这里保存已被取代的版本说明、安装步骤和变更记录，**不代表当前实现**。

| 前缀 | 内容 | 现状 |
|---|---|---|
| `README_v0.4/0.5`、`README_周报*` | 早期版本与周报 v1–v4.1 的说明 | 周报流程已由 `core/weekly_report_engine.py`（V8）取代 |
| `INSTALL_周报Agent_v4*` | 周报 v4 系列安装步骤 | 安装流程见根目录 `README.md` |
| `CHANGELOG_*` | 分阶段变更记录 | 保留用于追溯设计决策 |
| `README_使用说明.md`、`README_奖惩匹配精简版.md`、`README_安装与验证.md` | 单次补丁的随包说明 | 功能已合入 `app.py` |
| `TEST_RESULTS.md`、`FRONTEND_QA_RESULTS.md` | 当时的测试与前端检查记录 | 当前测试以 `python -m unittest discover -s tests` 为准 |

当前仍然有效的文档：根目录 `README.md`（总览与部署）、`docs/`（架构、前端规范、周报设计）。
