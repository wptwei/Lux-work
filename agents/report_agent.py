"""报告生成 Agent：把结构化结果整理为 Markdown 报告。"""
from __future__ import annotations


class ReportAgent:
    def generate(self, data) -> str:
        if not isinstance(data, dict):
            return f"# 安保事件报告\n\n{data}"

        title = data.get("title") or "安保处置报告"
        lines = [f"# {title}", ""]

        event = data.get("event")
        if event:
            lines.append(f"**事件**：{event}")
            lines.append("")

        risk = data.get("risk") or {}
        if risk:
            lines.append(f"**风险等级**：{risk.get('level', 'unknown')}（评分 {risk.get('score', 0)}）")

        agent = data.get("agent")
        if agent:
            lines.append(f"**处理 Agent**：{agent}")

        action = data.get("action")
        if action:
            lines.append(f"**处置动作**：{action}")

        detail = data.get("detail")
        if detail:
            lines.append("")
            lines.append(str(detail))

        status = data.get("status")
        if status:
            lines.append("")
            lines.append(f"**状态**：{status}")

        if status and "待" in str(status):
            lines.append("")
            lines.append("> 本报告为辅助研判草案，须由现场负责人或值班主管确认后执行与归档。")

        return "\n".join(lines)
