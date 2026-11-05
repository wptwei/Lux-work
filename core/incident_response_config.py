"""事件响应时限 / 责任人 / 首轮动作的配置读取与校验。

这些内容原先硬编码在 agents/incident_agent.py 和 app.py 中，页面看起来像制度规定，
其实没有任何依据。现在统一放在 config/incident_response.json，并带批准状态：
批准人与批准日期未填写时一律视为草稿，页面会标注“系统默认参考值，非制度条款，需部门确认”。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from core.paths import config_path

CONFIG_PATH = config_path("incident_response.json")
LEVELS = ("critical", "high", "medium", "low", "unknown")
DRAFT_NOTE = "系统默认参考值，非制度条款，需部门确认"


@dataclass(frozen=True)
class IncidentResponseConfig:
    plans: dict
    approved: bool
    approved_by: str = ""
    approved_at: str = ""
    source_document: str = ""


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def load_response_config(path: str | Path | None = None) -> IncidentResponseConfig:
    source = Path(path) if path else CONFIG_PATH
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"事件响应配置不可读取：{source}（{exc}）") from exc

    levels = data.get("levels")
    if not isinstance(levels, dict):
        raise ValueError(f"事件响应配置缺少 levels：{source}")
    plans: dict[str, dict] = {}
    for level in LEVELS:
        item = levels.get(level)
        if not isinstance(item, dict):
            raise ValueError(f"事件响应配置缺少等级 {level}：{source}")
        sla, owner = _text(item.get("sla")), _text(item.get("owner"))
        actions = tuple(a.strip() for a in item.get("actions", []) if isinstance(a, str) and a.strip())
        if not sla or not owner or not actions:
            raise ValueError(f"等级 {level} 必须填写 sla、owner 和至少一条 actions：{source}")
        plans[level] = {"sla": sla, "owner": owner, "actions": actions}

    approval = data.get("approval") or {}
    status = _text(approval.get("status")) or "draft"
    if status not in {"draft", "approved"}:
        raise ValueError(f"approval.status 只能是 draft 或 approved：{source}")
    approved_by, approved_at = _text(approval.get("approved_by")), _text(approval.get("approved_at"))
    if status == "approved" and not (approved_by and approved_at):
        raise ValueError("approval.status 为 approved 时必须填写 approved_by 和 approved_at")
    return IncidentResponseConfig(
        plans=plans,
        approved=status == "approved",
        approved_by=approved_by,
        approved_at=approved_at,
        source_document=_text(approval.get("source_document")),
    )


@lru_cache(maxsize=1)
def get_response_config() -> IncidentResponseConfig:
    return load_response_config()
