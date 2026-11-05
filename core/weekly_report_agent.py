"""H区周报文档 Agent：动态页数策划、逐页创作、事实核验与可编辑 Word 输出。

重要原则：不生成不存在的统计数值；规则草稿仅供诊断/测试，生成失败时不自动伪装成 AI 成稿。
"""
from __future__ import annotations

from core.report_data import visible_body

import json
import math
import re
from pathlib import Path

from core.report_data import parse_report, split_title_body
from core.paths import config_path

PAGE_TITLES = (
    "封面", "目录", "厂区分布&人力分布", "安保服务项目| 专区运营看板",
    "安保服务项目| 异常汇总分析", "本周重点工作", "下周重点工作计划", "结束页",
)
_PAGE_LINE = re.compile(
    r"(?mi)^\s*(?:#{1,6}\s*)?(?:P(\d{1,2})|【\s*P(\d{1,2})\s*】|第\s*(\d{1,2})\s*页)(?:\s|$)"
)


# 周报智能创作第一阶段：先提取可追溯事实，再分析和策划页面，最后生成文稿。
_FACT_CATEGORY_RULES = (
    ("next_plan", ("下周", "计划", "待跟进", "后续工作", "下一步", "下阶段", "预计推进", "计划开展")),
    ("staff", ("人数", "人力", "在岗", "人员编制", "岗位配置", "岗位人数", "班次配置")),
    ("operations", ("查获", "检查数量", "安检量", "出入量", "巡检次数", "运营", "总计", "统计", "数量", "合计")),
    ("completed_work", ("完成", "已完成", "组织开展", "开展", "落实", "推进", "交付", "修复", "更新", "协助", "跟进", "沟通", "培训")),
    ("incident", ("异常", "违纪", "未授权", "违规", "隐患", "告警", "故障", "未佩戴", "无人陪同", "报警", "未按规定", "泄露", "丢失", "冲突", "未通过")),
)
_CATEGORY_LABELS = {
    "staff": "人力数据", "operations": "运营数据", "incident": "异常事项",
    "completed_work": "已开展工作", "next_plan": "下周计划", "context": "背景信息", "other": "其他素材",
}
_QUANTITY_RE = re.compile(
    r"(?<![A-Za-z])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>人次|万元|分钟|小时|hours?|count|h|H|pcs|PCS|％|%|人|次|件|个|台|元|天|项|组|条|起|套|份|张|批|×|[xX]|处|分|级|家|班)"
)
_COMPARISON_TERMS = ("环比", "较上周", "比上周", "同比", "增加", "减少", "上升", "下降", "增长", "降低", "提升", "回落")
_CAUSAL_TERMS = ("由于", "因为", "导致", "受……影响", "受…影响", "原因是", "归因于", "源于")


def _normalize_unit(unit: str) -> str:
    unit = (unit or "").lower()
    if unit in {"人次", "人", "次", "件", "个", "台", "天", "项", "组", "条", "起", "套", "份", "张", "批", "×", "处", "家", "班", "pcs", "count"}:
        return "count"
    if unit in {"%", "％"}:
        return "%"
    if unit in {"h", "小时", "hour", "hours"}:
        return "hours"
    if unit == "x":
        return "masked_count"
    return unit


def extract_quantities(text: str) -> list[tuple[float, str]]:
    """只抽取带单位的业务数量，避免把日期/页码误判为统计值。"""
    found = []
    for match in _QUANTITY_RE.finditer(text or ""):
        try:
            found.append((float(match.group("value")), _normalize_unit(match.group("unit"))))
        except (TypeError, ValueError):
            continue
    return found


def restore_masked_quantity_markers(text: str, page_facts: list[dict]) -> str:
    """模型把脱敏的 166x 写成 166次/166count 时，恢复来源中的 x，避免泄露为精确数量。"""
    value = text or ""
    masked_values = {
        float(quantity["value"])
        for fact in page_facts
        for quantity in fact.get("quantities", [])
        if quantity.get("unit") == "masked_count"
    }
    exact_count_values = {
        float(quantity["value"])
        for fact in page_facts
        for quantity in fact.get("quantities", [])
        if quantity.get("unit") == "count"
    }
    # 同一页同一数值同时存在真实数量和脱敏数量时无法安全判断，保持模型原文并交给事实闸门处理。
    for number in sorted(masked_values - exact_count_values, reverse=True):
        token = f"{number:g}"
        pattern = re.compile(
            rf"(?<![\d.]){re.escape(token)}\s*(?:人次|人|次|件|个|台|项|组|条|起|套|份|张|批|处|家|班|pcs|count)(?![A-Za-z])",
            re.I,
        )
        value = pattern.sub(token + "x", value)
    return value


_SECTION_MARKER = re.compile(
    r"(?=[①②③④⑤⑥⑦⑧⑨⑩]\s*(?:岗位|专区运营|运营数据|运营看板|运营|"
    r"异常数据|异常汇总分析|异常汇总|异常|本周重点工作|本周工作|下周计划))"
)
_SECTION_HEADING = re.compile(
    r"^\s*[①②③④⑤⑥⑦⑧⑨⑩]?\s*(?P<heading>岗位\s*[/&和]?\s*人力(?:数据|分布)?|"
    r"(?:专区)?运营(?:数据|看板)?|异常(?:数据|汇总分析|汇总)?|"
    r"本周重点工作|本周工作|重点工作|下周重点工作计划|下周计划)"
    r"(?:[（(][^）)]*[）)])?(?=\s|[：:]|$)[：:]?\s*"
)
_INSTRUCTION_FRAGMENT_RE = re.compile(
    r"(?:(?:下周计划)?的)?(?:具体详细内容|具体内容|详细内容)?\s*(?:你|请)?\s*"
    r"(?:帮我写|帮我补充|帮我完善|帮我生成|帮我规划|帮我整理|请帮我|请生成|请撰写|生成一份周报)[^。；;\n]*",
    re.I,
)
_INSTRUCTION_ONLY_RE = re.compile(
    r"^(?:请|麻烦|你)?\s*(?:帮我|帮忙|协助我|生成|撰写|补充|完善|整理|规划).{0,60}$|"
    r"^(?:(?:下周计划)?的)?(?:具体详细内容|具体内容)(?:你|请)?帮我写.*$",
    re.I,
)
_DATE_ACTION_SPLIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}(?:日|号)?\s*[,，、]?"
    r"(?=.{0,45}(?:完成|开展|视察|参观|接待|陪同|沟通|商讨|巡查|保障|施工|跟进|培训|宣导|安装|处理|复盘|检查)))"
)
_WORK_ACTIONS = (
    "完成", "已完成", "组织开展", "开展", "落实", "推进", "交付", "修复", "更新", "协助", "跟进",
    "沟通", "培训", "视察", "参观", "接待", "陪同", "保障", "施工", "商讨", "巡查", "安装", "处理", "复盘", "宣导",
)


def _heading_hint_and_text(segment: str) -> tuple[str | None, str]:
    match = _SECTION_HEADING.match(segment or "")
    if not match:
        return None, (segment or "").strip()
    heading = re.sub(r"\s+", "", match.group("heading"))
    if "人力" in heading or "岗位" in heading:
        hint = "staff"
    elif "运营" in heading or "看板" in heading:
        hint = "operations"
    elif "异常" in heading:
        hint = "incident"
    elif "下周" in heading:
        hint = "next_plan"
    else:
        hint = "completed_work"
    return hint, segment[match.end():].strip()


def _remove_user_instruction(text: str) -> str:
    """移除混在素材里的写作要求，保留同一段中真实的业务事项。"""
    cleaned = (text or "").strip()
    cleaned = _INSTRUCTION_FRAGMENT_RE.sub("", cleaned).strip(" ，,；;。:：\t")
    if _INSTRUCTION_ONLY_RE.match(cleaned):
        return ""
    return cleaned


def _split_source_line(line: str) -> list[tuple[str | None, str]]:
    """将一行中混写的分区标题、指标和日期事件拆成独立素材。"""
    output: list[tuple[str | None, str]] = []
    for section in _SECTION_MARKER.split(line or ""):
        section = section.strip()
        if not section:
            continue
        hint, section_text = _heading_hint_and_text(section)
        if not section_text:
            continue
        # 仅在逗号后紧跟另一个明确指标字段时拆分，避免拆断普通叙述句。
        metric_fragments = re.split(
            r"(?:(?<=[0-9])|(?<=[xX×])|(?<=[人次项个台元天组条起套份张批处分级家班]))[，,]\s*(?=(?:楼栋|岗位|人力|人数|日均通行数量|违禁品|垃圾检查|检查工时|稽核异常|专区异常|总计|合计))",
            section_text,
        )
        for metric_fragment in metric_fragments:
            for sentence in re.split(r"(?<=[；;。])\s*", metric_fragment):
                sentence = sentence.strip(" ，,；;。\t")
                if not sentence:
                    continue
                date_parts = _DATE_ACTION_SPLIT_RE.split(sentence)
                nonempty_parts = [part.strip(" ，,；;。\t") for part in date_parts if part.strip(" ，,；;。\t")]
                starts_with_date = bool(re.match(r"^(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}(?:日|号)?", sentence))
                for part_index, fragment in enumerate(nonempty_parts):
                    fragment_hint = hint
                    if part_index > 0:
                        fragment_hint = None
                    elif starts_with_date and hint in {"staff", "operations", "incident"}:
                        fragment_hint = None
                    cleaned = _remove_user_instruction(fragment)
                    if not cleaned:
                        continue
                    if re.fullmatch(r"(?:本周重点工作|下周重点工作计划|下周计划|异常汇总分析|异常数据|岗位\s*&?\s*人力|目录|contents|thankyou)", cleaned, re.I):
                        continue
                    output.append((fragment_hint, cleaned))
    return output


def _classify_weekly_fact(text: str, heading_hint: str | None = None) -> str:
    value = (text or "").strip()
    if re.search(r"(?:所服务\s*BU|服务\s*BU|统计周期|报告周期|周报标题|^By\s*:|^Date\s*[:：])", value, re.I) and not re.search(r"(?:人力\s*\d|人数\s*\d|岗位\s*\d|日均通行数量\s*\d)", value):
        return "context"
    if re.search(r"岗位管控更换", value) and not re.search(r"已完成|完成了|已更换", value):
        return "next_plan"
    if heading_hint == "next_plan":
        return "next_plan"
    if re.search(r"(?:下周|下阶段|后续工作|下一步).{0,12}(?:计划|推进|跟进|完成|开展|检查|复盘|闭环)|^(?:计划落实|计划推进|计划开展|待跟进|后续计划|下一步计划)", value):
        return "next_plan"
    # 仅当片段确实包含相应指标时才沿用标题提示，防止同一行后半段日期事件被误分类。
    if heading_hint == "staff" and re.search(r"(?:人力\s*\d|人数\s*\d|岗位\s*\d|人员编制|岗位配置|岗位人数|班次配置|楼栋\s*\d|在岗人员\s*\d)", value):
        return "staff"
    if heading_hint == "operations" and any(term in value for term in ("违禁品", "垃圾检查", "检查工时", "日均通行数量", "安检量", "出入量", "运营数据", "查获数量", "检查数量", "总计", "数量")):
        return "operations"
    if heading_hint == "incident" and any(term in value for term in ("稽核异常", "专区异常", "异常事件", "异常数量", "异常环", "违规", "隐患", "故障", "未授权", "无人陪同")):
        return "incident"
    if heading_hint == "completed_work":
        return "completed_work"
    if re.search(r"(?:人力\s*\d|人数\s*\d|岗位\s*\d|人员编制|岗位配置|岗位人数|班次配置|楼栋\s*\d|在岗人员\s*\d)", value):
        return "staff"
    if re.search(r"(?:稽核异常|专区异常|异常事件|异常数量|发现异常\s*\d|异常环)", value) and re.search(r"\d", value):
        return "incident"
    if any(term in value for term in ("违禁品", "垃圾检查", "检查工时", "日均通行数量", "安检量", "出入量", "运营数据", "查获数量")):
        return "operations"
    has_date = bool(re.search(r"(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}(?:日|号)?", value))
    if (has_date and any(term in value for term in _WORK_ACTIONS)) or any(term in value for term in ("完成", "组织开展", "开展", "落实", "推进", "交付", "修复", "更新", "协助", "跟进", "培训", "视察", "参观", "接待", "陪同", "保障", "商讨", "巡查", "安装", "处理", "复盘", "宣导")):
        return "completed_work"
    if any(term in value for term in ("异常", "违纪", "未授权", "违规", "隐患", "告警", "故障", "未佩戴", "无人陪同", "报警", "未按规定", "泄露", "丢失", "冲突", "未通过")):
        return "incident"
    if any(term in value for term in ("人数", "人力", "人员编制", "岗位配置", "岗位人数", "班次配置")):
        return "staff"
    if any(term in value for term in ("查获", "检查数量", "安检量", "出入量", "巡检次数", "运营", "总计", "统计", "数量", "合计", "工时")):
        return "operations"
    return "other"


def extract_weekly_facts(source_text: str) -> list[dict]:
    """提取可追溯事实；支持同一行拆分多类素材，并过滤混入的写作指令。"""
    facts = []
    section_hint = None
    for line_no, raw in enumerate((source_text or "").splitlines(), start=1):
        heading_hint, remainder = _heading_hint_and_text(raw)
        if heading_hint and not remainder.strip(" ：:，,；;。\t"):
            # 独立成行的分区标题（如“下周计划：”）不产生素材，但其后的条目要沿用
            # 该分类，否则下周计划会被当成已完成工作写进本周重点工作页。
            section_hint = heading_hint
            continue
        for hint, text in _split_source_line(raw):
            category = _classify_weekly_fact(text, hint or section_hint)
            if category == "other" and not hint and facts and _WEEK_CONTINUATION_RE.match(text):
                # “9wk3 25”这类只含周期的续行没有自身分类线索，跟随上一条指标的分区，
                # 否则它会掉进“其他素材”并被派到重点工作页，图表也就少画一期。
                category = facts[-1]["category"]
            dates = re.findall(
                r"(?<![A-Za-z0-9])(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}(?:日|号)?(?![A-Za-z0-9])|20\d{2}年\d{1,2}月(?:\d{1,2}日)?",
                text,
            )
            quantities = [
                {"value": value, "unit": unit}
                for value, unit in extract_quantities(text)
            ]
            facts.append({
                "id": f"F{len(facts) + 1:03d}",
                "source_line": line_no,
                "text": text,
                "category": category,
                "category_label": _CATEGORY_LABELS.get(category, "其他素材"),
                "needs_confirmation": bool(re.search(r"岗位管控更换", text) and not re.search(r"已完成|完成了|已更换", text)),
                "dates": dates,
                "quantities": quantities,
            })
    return facts


_SUPPORTED_PAGE_TYPES = {
    "cover", "contents", "manpower", "operations", "anomalies", "key_work",
    "photo_evidence", "timeline", "control_core", "next_plan", "summary", "generic", "closing",
}


def _infer_page_type(title: str, index: int = 0, total: int = 0) -> str:
    title = (title or "").strip().lower()
    if index == 0 or any(k in title for k in ("封面", "周报标题")):
        return "cover"
    if any(k in title for k in ("thankyou", "结束页", "感谢观看", "谢谢观看")) and (not total or index == total - 1):
        return "closing"
    if any(k in title for k in ("目录", "contents")):
        return "contents"
    if any(k in title for k in ("人力", "厂区分布", "岗位配置", "人员分布")):
        return "manpower"
    if any(k in title for k in ("异常", "事件分析", "异常汇总", "风险分析")):
        return "anomalies"
    if any(k in title for k in ("运营看板", "运营数据", "违禁品", "指标看板", "数据看板")):
        return "operations"
    if any(k in title for k in ("下周", "计划", "后续工作", "下一步")):
        return "next_plan"
    if any(k in title for k in ("本周重点工作", "重点工作", "工作成果", "本周完成", "关键工作")):
        return "key_work"
    if any(k in title for k in ("现场执勤影像", "现场影像", "现场照片", "影像展示", "照片展示")):
        return "photo_evidence"
    if any(k in title for k in ("关键节点", "里程碑", "节点纪实", "进度时间线")):
        return "timeline"
    if any(k in title for k in ("管控核心", "管控措施", "管控执行", "制度执行", "执行要点")):
        return "control_core"
    if any(k in title for k in ("摘要", "总览", "本期概览", "执行摘要")):
        return "summary"
    return "generic"


def _offline_page_specs(facts: list[dict]) -> list[dict]:
    """按素材实际覆盖范围生成兜底页；不为凑页数制造空白业务页。"""
    by_category = {key: [] for key in _CATEGORY_LABELS}
    for fact in facts:
        by_category.setdefault(fact.get("category", "other"), []).append(fact["id"])
    fact_by_id = {fact["id"]: fact for fact in facts}
    specs = [{"page_type": "cover", "title": "H区安保周报", "purpose": "报告封面", "fact_ids": [], "missing_fields": []}]
    content_specs = []

    if by_category.get("staff"):
        content_specs.append({"page_type": "manpower", "title": "厂区分布&人力分布", "purpose": "汇总岗位与人力信息", "fact_ids": by_category["staff"], "missing_fields": []})
    if by_category.get("operations"):
        content_specs.append({"page_type": "operations", "title": "专区运营看板", "purpose": "呈现有来源的运营数据", "fact_ids": by_category["operations"], "missing_fields": []})
    if by_category.get("incident"):
        content_specs.append({"page_type": "anomalies", "title": "异常汇总分析", "purpose": "归纳本周异常事项", "fact_ids": by_category["incident"], "missing_fields": []})

    control_ids = [
        fid for fid in (by_category.get("completed_work", []) + by_category.get("other", []))
        if any(term in fact_by_id[fid].get("text", "") for term in ("管控", "权限", "物料放行", "制度", "封条", "安检流程"))
    ]
    control_set = set(control_ids)
    if len(control_ids) >= 2:
        content_specs.append({"page_type": "control_core", "title": "管控核心与执行", "purpose": "归纳素材中明确记录的管控事项", "fact_ids": control_ids, "missing_fields": []})

    work_ids = [fid for fid in by_category.get("completed_work", []) + by_category.get("other", []) if fid not in control_set]
    if work_ids:
        content_specs.append({"page_type": "key_work", "title": "本周重点工作", "purpose": "呈现本周已记录的工作事项", "fact_ids": work_ids, "missing_fields": []})
    if by_category.get("next_plan"):
        content_specs.append({"page_type": "next_plan", "title": "下周重点工作计划", "purpose": "整理明确的后续计划", "fact_ids": by_category["next_plan"], "missing_fields": []})
    if not content_specs:
        content_specs.append({"page_type": "summary", "title": "本期工作摘要", "purpose": "集中展示当前可用素材", "fact_ids": [f["id"] for f in facts], "missing_fields": ["建议补充工作成果、运营数据或下周计划"] if not facts else []})

    if len(content_specs) >= 3:
        specs.append({"page_type": "contents", "title": "目录", "purpose": "概览本期汇报结构", "fact_ids": [], "missing_fields": []})
    specs.extend(content_specs)
    specs.append({"page_type": "closing", "title": "Thankyou", "purpose": "结束页", "fact_ids": [], "missing_fields": []})
    for number, spec in enumerate(specs, start=1):
        spec["page"] = number
    return specs


def make_offline_analysis(facts: list[dict], period: str = "") -> dict:
    """模型不可用时生成动态页数规划；不为了凑固定页数补造业务内容。"""
    page_plan = _offline_page_specs(facts)
    assigned = {fid for page in page_plan for fid in page.get("fact_ids", [])}
    content_titles = [p["title"] for p in page_plan if p["page_type"] not in {"cover", "contents", "closing"}]
    if not any(p["page_type"] == "contents" for p in page_plan) and len(content_titles) >= 3:
        # 规则规划已决定不插目录时，不在这里擅自改动页码；模型创作可自行判断是否需要目录。
        pass
    return {
        "mode": "rule_fallback",
        "executive_summary": {"text": f"已提取 {len(facts)} 条原始素材；当前为本地规则分类，未进行 AI 语义分析。", "fact_ids": [f["id"] for f in facts]},
        "highlights": [], "data_insights": [], "risks": [],
        "next_week": [
            {
                "text": ("待确认候选计划：" if f.get("needs_confirmation") else "") + f["text"],
                "fact_ids": [f["id"]],
                **({"needs_confirmation": True} if f.get("needs_confirmation") else {}),
            }
            for f in facts if f.get("category") == "next_plan"
        ],
        "page_plan": page_plan,
        "missing_info": list(dict.fromkeys(m for p in page_plan for m in p.get("missing_fields", []))),
        "unassigned_fact_ids": [f["id"] for f in facts if f["id"] not in assigned and f.get("category") != "context"],
        "warnings": ["模型不可用或结构化分析失败；页面分类仅为本地规则结果。"],
    }


def parse_json_object(raw: str) -> dict:
    """解析模型 JSON，兼容外层 Markdown 代码围栏。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("模型返回的 JSON 顶层必须是对象")
    return payload


def _valid_fact_ids(value, known_ids: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip() in known_ids))


_DATA_BLOCK_HEAD = re.compile(r"(?i)^\s*(?:图表数据|表格数据|chart_json)\s*[：:]?\s*$")


def _without_data_blocks(text: str) -> str:
    """去掉只供渲染的数据块：其中的数值单独由 _chart_data_block_issues 核验。"""
    kept, skipping, in_json = [], False, False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if re.match(r"(?i)^chart_json\s*[：:]", stripped):
            in_json = True
            continue
        if in_json:
            if re.match(r"(?i)^end_chart_json", stripped):
                in_json = False
            continue
        if _DATA_BLOCK_HEAD.match(line):
            skipping = True
            continue
        if skipping:
            if not stripped:
                skipping = False
            continue
        kept.append(line)
    return "\n".join(kept)


def _validate_claim_text(text: str, fact_ids: list[str], facts_by_id: dict[str, dict], derived: list[dict] | None = None) -> list[str]:
    """检查分析句是否有引用事实支撑；保守拦截无来源数量、因果和环比结论。"""
    # 占位符中的“环比/原因”等说明是待补充提示，不属于已经作出的事实结论。
    claim_text = re.sub(r"【待补充[^】]*】", "", _without_data_blocks(text))
    refs = [facts_by_id[fid] for fid in fact_ids if fid in facts_by_id]
    source_text = " ".join(f.get("text", "") for f in refs)
    source_quantities = {q for fact in refs for q in extract_quantities(fact.get("text", ""))}
    allowed_quantities = set(source_quantities)
    for item in derived or []:
        if item.get("verified"):
            allowed_quantities.add((float(item["value"]), _normalize_unit(item.get("unit", "count"))))
    issues = []
    for quantity in extract_quantities(claim_text):
        if quantity not in allowed_quantities:
            issues.append(f"数量 {quantity[0]:g}{quantity[1]} 未被引用素材直接支持")
    explicit_comparison_terms = ("环比", "较上周", "比上周", "同比", "较上期", "比上期")
    trend_terms = ("增加", "减少", "上升", "下降", "增长", "降低", "回落")
    metric_terms = ("数量", "次数", "异常", "查获", "人力", "人数", "运营", "指标", "事件", "故障", "隐患", "数据")
    has_current = any(term in source_text for term in ("本周", "本期", "当前", "本次"))
    has_previous = any(term in source_text for term in ("上周", "上期", "前期", "较上周"))
    source_explicit_comparison = any(term in source_text for term in explicit_comparison_terms)
    source_trend_is_explicit = any(term in source_text for term in trend_terms)
    claims_comparison = any(term in claim_text for term in explicit_comparison_terms)
    claims_data_trend = any(term in claim_text for term in trend_terms) and any(term in claim_text for term in metric_terms)
    if claims_comparison and not (source_explicit_comparison or (has_current and has_previous)):
        issues.append("趋势/环比结论缺少本期与上期的对照依据")
    elif claims_data_trend and not (source_trend_is_explicit or (has_current and has_previous)):
        issues.append("数据趋势结论缺少本期与上期的对照依据")
    if any(term in claim_text for term in _CAUSAL_TERMS) and not any(term in source_text for term in _CAUSAL_TERMS):
        issues.append("原因归因没有在引用素材中明确出现")
    return issues


def _metric_signature(text: str) -> set[str]:
    """提取用于保守比较的指标词，避免把人数与异常次数等不同指标相减。"""
    value = _QUANTITY_RE.sub(" ", text or "")
    value = re.sub(r"(?<!\d)(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}(?:日|号)?", " ", value)
    for term in ("本周", "上周", "本期", "上期", "本次", "上次", "当前", "较上周", "比上周", "环比", "同比", "总计", "合计", "数量", "共计", "查获", "发生", "增加", "减少", "上升", "下降", "增长", "降低", "提升", "回落", "本月", "上月", "统计"):
        value = value.replace(term, " ")
    return {token for token in re.findall(r"[\u4e00-\u9fff]{2,}", value) if token not in {"本期", "上期", "工作", "数据"}}


def _verify_delta_calculation(calculation: str, fact_ids: list[str], facts_by_id: dict[str, dict]) -> dict | None:
    """只验证简单减法：数值与单位有来源，且两条事实描述的是同一指标。"""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-−]\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)\s*", calculation or "")
    if not match:
        return None
    a, b, c = map(float, match.groups())
    source_facts = [facts_by_id[fid] for fid in fact_ids if fid in facts_by_id]
    facts_a = [(fact, unit) for fact in source_facts for value, unit in extract_quantities(fact.get("text", "")) if math.isclose(value, a)]
    facts_b = [(fact, unit) for fact in source_facts for value, unit in extract_quantities(fact.get("text", "")) if math.isclose(value, b)]
    valid_unit = None
    for fact_a, unit_a in facts_a:
        for fact_b, unit_b in facts_b:
            if unit_a != unit_b or unit_a == "masked_count":
                continue
            metric_a = _metric_signature(fact_a.get("text", ""))
            metric_b = _metric_signature(fact_b.get("text", ""))
            if metric_a and metric_b and metric_a.intersection(metric_b):
                valid_unit = unit_a
                break
        if valid_unit:
            break
    if not valid_unit or not math.isclose(abs(a - b), c):
        return None
    return {"value": c, "unit": valid_unit, "verified": True}


def normalize_weekly_analysis(payload: dict, facts: list[dict]) -> tuple[dict, list[str]]:
    """校验动态页数策划、来源 ID 与关键结论；页数由素材和模型策划决定。"""
    known_ids = {fact["id"] for fact in facts}
    facts_by_id = {fact["id"]: fact for fact in facts}
    errors: list[str] = []
    raw_pages = payload.get("page_plan")
    if not isinstance(raw_pages, list):
        return {}, ["缺少 page_plan 页面规划"]
    if not 3 <= len(raw_pages) <= 20:
        return {}, ["页面规划页数必须在 3—20 页之间；请按素材重要性规划，不要为了凑页数添加空白页"]
    page_plan = []
    seen_pages = set()
    for item in raw_pages:
        if not isinstance(item, dict):
            continue
        try:
            page_no = int(str(item.get("page", "")).upper().replace("P", ""))
        except (TypeError, ValueError):
            continue
        if not 1 <= page_no <= 20 or page_no in seen_pages:
            errors.append(f"页面规划编号无效或重复：{page_no}")
            continue
        seen_pages.add(page_no)
        raw_ids = item.get("fact_ids", [])
        ids = _valid_fact_ids(raw_ids, known_ids)
        unknown = [str(fid) for fid in raw_ids if str(fid) not in known_ids] if isinstance(raw_ids, list) else []
        if unknown:
            errors.append(f"P{page_no} 引用了不存在的事实 ID：{', '.join(unknown[:3])}")
        title = str(item.get("title") or f"P{page_no}")[:100]
        raw_type = str(item.get("page_type") or item.get("type") or "").strip().lower()
        page_type = raw_type if raw_type in _SUPPORTED_PAGE_TYPES else _infer_page_type(title, page_no - 1, len(raw_pages))
        page_plan.append({
            "page": page_no,
            "page_type": page_type,
            "title": title,
            "purpose": str(item.get("purpose") or "按本期汇报需要组织内容")[:240],
            "fact_ids": ids,
            "missing_fields": [str(x)[:100] for x in item.get("missing_fields", []) if str(x).strip()][:8] if isinstance(item.get("missing_fields", []), list) else [],
            "visual_plan": str(item.get("visual_plan") or item.get("visual") or "")[:240],
            "asset_needs": [str(x)[:100] for x in item.get("asset_needs", []) if str(x).strip()][:6] if isinstance(item.get("asset_needs", []), list) else [],
        })
        if page_type not in {"cover", "contents", "closing"} and not ids:
            # 页面可以有缺项，但不能没有任何来源或明确缺项说明。
            if not page_plan[-1]["missing_fields"]:
                page_plan[-1]["missing_fields"] = ["该页缺少可引用的事实素材"]
    expected = set(range(1, len(raw_pages) + 1))
    if seen_pages != expected:
        errors.append(f"页面规划编号必须从 P1 连续编号至 P{len(raw_pages)}，不能缺页或重复")
    if page_plan and page_plan[0]["page_type"] != "cover":
        errors.append("页面规划第一张必须是封面")
    if errors:
        return {}, errors
    page_plan.sort(key=lambda x: x["page"])

    warnings: list[str] = []
    verified_calculations: list[dict] = []

    def clean_claims(key: str, default_page: int | None = None) -> list[dict]:
        output = []
        raw_items = payload.get(key, [])
        if not isinstance(raw_items, list):
            warnings.append(f"{key} 格式不正确，已忽略")
            return output
        for raw in raw_items[:30]:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text", "")).strip()[:500]
            ids = _valid_fact_ids(raw.get("fact_ids", []), known_ids)
            if not text or not ids:
                warnings.append(f"{key} 中有缺少事实依据的条目，已忽略")
                continue
            page_no = raw.get("page", default_page)
            try:
                page_no = int(str(page_no).upper().replace("P", "")) if page_no is not None else default_page
            except (TypeError, ValueError):
                page_no = default_page
            calculation = str(raw.get("calculation", "")).strip()
            verified_calc = _verify_delta_calculation(calculation, ids, facts_by_id) if calculation else None
            derived = []
            if verified_calc:
                verified_calc["page"] = page_no
                derived.append(verified_calc)
            issues = _validate_claim_text(text, ids, facts_by_id, derived)
            if issues:
                warnings.extend(f"{key} 条目已因依据不足而忽略：{issue}" for issue in issues[:2])
                continue
            if verified_calc:
                verified_calculations.append(verified_calc)
            row = {"text": text, "fact_ids": ids}
            if page_no is not None:
                row["page"] = page_no
            if calculation:
                row["calculation"] = calculation
                row["calculation_verified"] = bool(verified_calc)
            if raw.get("needs_confirmation"):
                row["needs_confirmation"] = True
            output.append(row)
        return output

    summary_raw = payload.get("executive_summary", {})
    if isinstance(summary_raw, str):
        summary_raw = {"text": summary_raw, "fact_ids": []}
    summary_text = str(summary_raw.get("text", "")).strip()[:600] if isinstance(summary_raw, dict) else ""
    summary_ids = _valid_fact_ids(summary_raw.get("fact_ids", []), known_ids) if isinstance(summary_raw, dict) else []
    if summary_text and (not summary_ids or _validate_claim_text(summary_text, summary_ids, facts_by_id)):
        warnings.append("总览摘要缺少可验证的来源支持，已隐藏")
        summary_text, summary_ids = "", []
    highlights = clean_claims("highlights")
    data_insights = clean_claims("data_insights")
    risks = clean_claims("risks")
    next_week = clean_claims("next_week", default_page=7)

    # 仅修正明确的来源分配；不生成正文，也不把此步骤算作 AI 创作。
    routing = {
        "staff": ("manpower", {"manpower"}),
        "operations": ("operations", {"operations", "anomalies"}),
        "incident": ("anomalies", {"anomalies"}),
        "completed_work": (
            "key_work",
            {"key_work", "control_core", "timeline", "photo_evidence", "summary", "generic"},
        ),
        "next_plan": ("next_plan", {"next_plan"}),
    }
    repairs = []
    for fact in facts:
        route = routing.get(fact.get("category"))
        if not route:
            continue
        preferred_type, allowed = route
        targets = [p for p in page_plan if p["page_type"] in allowed]
        if not targets:
            continue
        if not any(fact["id"] in p["fact_ids"] for p in targets):
            preferred = next(
                (p for p in targets if p["page_type"] == preferred_type),
                targets[0],
            )
            preferred["fact_ids"].append(fact["id"])
            repairs.append(f"{fact['id']} → P{preferred['page']}")
        for p in page_plan:
            if p["page_type"] not in allowed and fact["id"] in p["fact_ids"]:
                p["fact_ids"].remove(fact["id"])
        if fact.get("needs_confirmation"):
            for row in next_week:
                if fact["id"] in row["fact_ids"]:
                    row["text"] = "待确认候选计划：" + fact["text"]
                    row["needs_confirmation"] = True
            if not any(fact["id"] in row["fact_ids"] for row in next_week):
                next_week.append({"text": "待确认候选计划：" + fact["text"], "fact_ids": [fact["id"]], "needs_confirmation": True})
    if repairs:
        warnings.append("来源分配校正（非模型创作）：" + "；".join(repairs))
    assigned = {fid for page in page_plan for fid in page["fact_ids"]}
    raw_missing_info = payload.get("missing_info", [])
    missing_info = []
    if isinstance(raw_missing_info, list):
        for value in raw_missing_info:
            item = str(value).strip()[:180]
            if not item:
                continue
            # 小模型有时把尚未分配的现有 F 编号误写成“缺失材料”。这类编号已有来源，
            # 应由上面的确定性路由处理，不能提示用户重复补充。
            if re.fullmatch(r"F\d{3}(?:[\s、,，;；]+F\d{3})*", item, re.I):
                continue
            missing_info.append(item)
    analysis = {
        "mode": "ai_structured",
        "executive_summary": {"text": summary_text, "fact_ids": summary_ids},
        "highlights": highlights,
        "data_insights": data_insights,
        "risks": risks,
        "next_week": next_week,
        "page_plan": page_plan,
        "missing_info": missing_info[:30],
        "unassigned_fact_ids": [fact["id"] for fact in facts if fact["id"] not in assigned and fact.get("category") != "context"],
        "verified_calculations": verified_calculations,
        "warnings": warnings,
    }
    return analysis, []


def _chart_data_block_issues(body: str, page_facts: list[dict], page_no: int) -> list[str]:
    """校验图表数值确实来自本页事实，且图表标签与事实指标相符。"""
    from core.report_data import parse_json_chart
    try:
        parsed = parse_json_chart(body)
    except (ValueError, TypeError) as exc:
        return [f"P{page_no} 图表数据错误：{exc}"]
    if parsed:
        rows = ["类别|" + "|".join(parsed["series_names"])]
        rows.extend(str(cat) + "|" + "|".join(str(series[i]) for series in parsed["series"]) for i, cat in enumerate(parsed["categories"]))
        body = "图表数据：\n" + "\n".join(rows)
    lines = (body or "").splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"(?i)^\s*图表数据\s*[：:]?\s*$", line)), None)
    if start is None:
        return []
    block = []
    for line in lines[start + 1:]:
        value = line.strip()
        if not value:
            if block:
                break
            continue
        if re.match(r"(?i)^key\s*message\s*[：:]", value):
            break
        block.append(value)
    if len(block) < 2:
        return [f"P{page_no} 图表数据块缺少列名或数据行"]
    # 单系列（类别|数值）允许用于分类占比图；两组以上的对比仍必须有本期/上期原始值。
    columns = len([cell for cell in block[0].strip("|").split("|") if cell.strip()])
    if columns > 2 and not _has_comparable_period_values(page_facts):
        return [f"P{page_no} 缺少同一指标的本期/上期原始值，不允许生成对比图表"]
    source_text = " ".join(f.get("text", "") for f in page_facts)
    # 删除日期，避免把 9/15 的日期数字当成数据来源。
    source_without_dates = re.sub(
        r"(?<![A-Za-z0-9])(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}(?:日|号)?(?![A-Za-z0-9])", " ", source_text
    )
    source_numbers = {
        float(token) for token in re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])", source_without_dates)
    }
    source_numbers.update(
        value for fact in page_facts for value, unit in extract_quantities(fact.get("text", ""))
        if unit != "masked_count"
    )
    # 单系列分类图常取自“楼栋3x、岗位3x”这类脱敏数字，它们同样是原文数值；
    # 多系列对比仍只认未脱敏的明确数值。
    category_numbers = set(source_numbers)
    category_numbers.update(
        value for fact in page_facts for value, _unit in extract_quantities(fact.get("text", ""))
    )
    allowed_numbers = category_numbers if columns == 2 else source_numbers
    source_metric_tokens = set().union(*(_metric_signature(f.get("text", "")) for f in page_facts)) if page_facts else set()
    issues = []
    for row in block[1:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 2:
            issues.append(f"P{page_no} 图表数据行格式错误")
            continue
        label_tokens = _metric_signature(cells[0])
        if label_tokens and not label_tokens.intersection(source_metric_tokens):
            issues.append(f"P{page_no} 图表指标‘{cells[0]}’无法在来源素材中找到依据")
        for cell in cells[1:]:
            token = cell.strip().replace(",", "")
            if re.fullmatch(r"-?\d+(?:\.\d+)?", token):
                if float(token) not in allowed_numbers:
                    issues.append(f"P{page_no} 图表数值 {token} 未出现在本页来源素材中")
    return list(dict.fromkeys(issues))

def validate_report_grounding(report_text: str, facts: list[dict], analysis: dict) -> list[str]:
    """检查动态内容页的数字、趋势和原因是否能回溯到页面策划引用的素材。"""
    checked = validate_weekly_report(report_text)
    if not checked["valid"]:
        return ["周报结构不完整，无法进行事实来源核验"]
    facts_by_id = {fact["id"]: fact for fact in facts}
    page_map = {int(p["page"]): p for p in analysis.get("page_plan", []) if str(p.get("page", "")).isdigit()}
    errors = []
    for page in checked["pages"]:
        page_no = int(page["no"])
        planned = page_map.get(page_no, {})
        page_type = planned.get("page_type") or page.get("type") or _infer_page_type(page.get("title", ""), page_no - 1, len(checked["pages"]))
        if page_type in {"cover", "contents", "closing"}:
            continue
        fact_ids = [fid for fid in planned.get("fact_ids", []) if fid in facts_by_id]
        derived = [item for item in analysis.get("verified_calculations", []) if item.get("verified") and item.get("page") == page_no]
        title = page.get("title") or f"P{page_no}"
        page_facts = [facts_by_id[fid] for fid in fact_ids]
        for issue in _validate_claim_text(page.get("body", ""), fact_ids, facts_by_id, derived):
            errors.append(f"P{page_no}「{title}」：{issue}")
        for issue in _chart_data_block_issues(page.get("body", ""), page_facts, page_no):
            errors.append(f"P{page_no}「{title}」：{issue}")
    return errors


CRITICAL_CATEGORIES = {"staff", "operations", "incident", "completed_work", "next_plan"}


def critical_fact_coverage_issues(facts: list[dict], analysis: dict) -> list[str]:
    """关键业务事实必须落到具体内容页；封面、目录、结束页不算分配。"""
    assigned = {
        fid for page in analysis.get("page_plan", [])
        if page.get("page_type") not in {"cover", "contents", "closing"}
        for fid in page.get("fact_ids", [])
    }
    missing = [
        fact["id"] for fact in facts
        if fact.get("category") in CRITICAL_CATEGORIES and fact["id"] not in assigned
    ]
    return (["关键素材未分配到页面：" + "、".join(missing[:12])] if missing else [])


def _compact_prompt_facts(facts: list[dict]) -> list[dict]:
    """发送给模型时只带必要字段，减少本地小模型上下文消耗；完整事实仍保留在会话状态。"""
    return [
        {
            "id": fact["id"],
            "category": fact.get("category", "other"),
            "category_label": fact.get("category_label", "其他素材"),
            "source_line": fact.get("source_line"),
            "needs_confirmation": fact.get("needs_confirmation", False),
            "dates": fact.get("dates", []),
            # 内部标准单位 count/hours 不发给模型，展示单位以 text 中的来源原文为准。
            "text": fact.get("text", ""),
        }
        for fact in facts
    ]


def _load_weekly_style_profile() -> dict:
    """读取基于历史周报提炼的可配置视觉与页面类型规范。"""
    path = config_path("weekly_report_style_profiles.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_weekly_analysis_prompt(facts: list[dict], period: str = "") -> str:
    """第一阶段模型提示词：根据素材覆盖面自由规划页数与页面类型。"""
    profile = _load_weekly_style_profile()
    source_reports = profile.get("source_reports", []) if isinstance(profile.get("source_reports", []), list) else []
    visual = profile.get("visual_system", {}) if isinstance(profile.get("visual_system", {}), dict) else {}
    colors = visual.get("colors", {}) if isinstance(visual.get("colors", {}), dict) else {}
    page_policy = visual.get("page_count_policy", {}) if isinstance(visual.get("page_count_policy", {}), dict) else {}
    return json.dumps({
        "task": "分析 H 区安保周报素材，提炼管理重点并规划一份页数自适应的汇报，不要撰写最终正文",
        "period": period,
        "reference_style": {
            "based_on": [f"{item.get('file', '历史周报')}（{item.get('slide_count', '动态')}页）" for item in source_reports],
            "visual_style": "16:9、白底、深蓝/蓝色与青色点缀、微软雅黑、简洁商务汇报、图表与现场图片结合；配色 " + json.dumps(colors, ensure_ascii=False),
            "recurring_sections": visual.get("recurring_sections", ["封面", "人力", "运营看板", "异常分析", "本周重点工作", "下周计划"]),
            "optional_sections": visual.get("optional_sections", ["现场执勤影像", "关键节点纪实", "管控核心与执行", "专题分析"]),
            "page_count": f"不固定；{page_policy.get('rule', '按本期素材决定页数')} 建议 {page_policy.get('recommended_min', 6)}—{page_policy.get('recommended_max', 12)} 页但不强制。",
        },
        "facts": _compact_prompt_facts(facts),
        "required_json_schema": {
            "executive_summary": {"text": "简短总览；没有足够事实时留空", "fact_ids": ["F001"]},
            "highlights": [{"text": "有事实支持的工作亮点", "fact_ids": ["F001"]}],
            "data_insights": [{"page": 3, "text": "有事实支持的数据结论", "fact_ids": ["F002", "F003"], "calculation": "27 - 21 = 6", "needs_confirmation": False}],
            "risks": [{"text": "已记录的问题或风险，不要推断", "fact_ids": ["F004"]}],
            "next_week": [{"text": "下周计划原文或忠实精简", "fact_ids": ["F005"]}],
            "page_plan": [{"page": 1, "page_type": "cover", "title": "H区安保周报", "purpose": "封面", "fact_ids": [], "missing_fields": [], "visual_plan": "大标题与周期", "asset_needs": []}],
            "missing_info": ["尚缺少的材料字段"],
        },
        "allowed_page_types": sorted(_SUPPORTED_PAGE_TYPES),
        "rules": [
            "页面数由本期素材决定，建议 6—12 页但不强制；最少 3 页，最多 20 页。不要为了凑页数制造空白页或重复内容。",
            "page_plan 必须从 page=1 开始连续编号；第一张必须是 cover。每页需提供 page_type、title、purpose、fact_ids、visual_plan。",
            "参考历史周报的深蓝/蓝/青色、微软雅黑、简洁商务视觉；结构可以调整，允许新增专题页、照片页、时间线页或管控执行页。",
            "常见页面类型包括 cover、contents、manpower、operations、anomalies、key_work、photo_evidence、timeline、control_core、next_plan、summary、generic、closing。仅在有素材支持时加入相应业务页。",
            "有照片需求时在 asset_needs 中写清楚需要哪类现场照片；没有照片素材时不能声称已插入真实照片。",
            "每条结论必须列出 fact_ids，且只能使用输入中存在的 F 编号。所有人力、运营、异常、已完成工作和下周计划事实都必须分配到合适页面，不能遗漏。",
            "category_label=背景信息的内容只用于封面、页眉或服务范围说明，绝不能当作本周工作事项。",
            "只把已经发生的事情放进本周重点工作；下周计划只放明确未来事项。原始素材里的‘帮我写/请补充/具体详细内容’等写作指令不得进入正文。",
            "同一行可能混有多类信息；根据每条事实的 category_label 分开策划，不得把工作事件塞入人力表格，也不得把背景信息当作工作成果。",
            "不允许编造人数、次数、日期、金额、结果、责任人或原因。没有本期与上期可比数据时，不得写环比/趋势；原因未在原文出现时不得归因。",
            "只有同一指标的两个来源数值明确可比时，才可写简单减法，calculation 用 a - b = c 表达，并引用两个来源事实。",
            "结构化图表或表格应在 visual_plan 中说明；只有来源中存在明确、可比的数字时才规划数据图表。",
            "返回 JSON 对象，不要代码围栏，不要附加解释。",
        ],
    }, ensure_ascii=False)


def build_weekly_compact_analysis_prompt(
    facts: list[dict],
    page_plan: list[dict],
    period: str = "",
) -> str:
    """本地 CPU 模型使用精简策划：固定可靠页骨架，只让 AI 做语义分析。"""
    compact_plan = [
        {
            "page": page.get("page"),
            "page_type": page.get("page_type"),
            "title": page.get("title"),
            "fact_ids": page.get("fact_ids", []),
        }
        for page in page_plan
    ]
    return json.dumps({
        "task": "在给定页面与事实分配下，完成简洁的 H 区安保周报语义分析；不要重新规划页面",
        "period": period or "未提供",
        "facts": _compact_prompt_facts(facts),
        "fixed_page_plan": compact_plan,
        "required_json_schema": {
            "executive_summary": {"text": "不超过80字的事实总览", "fact_ids": ["F001"]},
            "highlights": [{"text": "工作亮点，不超过60字", "fact_ids": ["F001"]}],
            "data_insights": [{"page": 3, "text": "数据结论，不超过60字", "fact_ids": ["F002"]}],
            "risks": [{"text": "已记录的风险，不超过60字", "fact_ids": ["F003"]}],
            "next_week": [{"text": "明确的后续计划，不超过60字", "fact_ids": ["F004"], "needs_confirmation": False}],
            "missing_info": ["确实缺少且无法从任何 F 编号获得的字段"],
        },
        "rules": [
            "只返回一个紧凑 JSON 对象，不要代码围栏、解释、page_plan 或 fixed_page_plan。",
            "每个数组最多4条；没有可靠内容时返回空数组，不要为了填满而编造。",
            "每条结论必须引用现有 fact_ids；不得把现有 F 编号写进 missing_info。",
            "不得编造数字、日期、趋势、原因、结果或责任人。",
            "来源中以 x/× 脱敏的数量必须原样保留，禁止改写成‘次’‘个’‘人’或 count；h 可以写为小时。",
            "needs_confirmation=true 的事实只能表述为待确认候选计划。",
        ],
    }, ensure_ascii=False, separators=(",", ":"))


def build_weekly_report_prompt(facts: list[dict], analysis: dict, period: str = "") -> str:
    """第二阶段提示词：按动态页面策划输出周报正文。"""
    prompt_analysis = {
        key: analysis.get(key)
        for key in ("executive_summary", "highlights", "data_insights", "risks", "next_week", "page_plan", "missing_info")
        if key in analysis
    }
    return json.dumps({
        "task": "根据已提取事实和动态页面策划，创作 H 区安保周报正文",
        "period": period,
        "facts": _compact_prompt_facts(facts),
        "analysis_and_page_plan": prompt_analysis,
        "output_format": "只输出 Markdown 周报正文。每页用独立行 ## P1 [type:cover] | H区安保周报 这样的格式开头；P 编号必须连续，页数与 page_plan 完全一致；不要输出 JSON、代码围栏或分析过程。",
        "writing_rules": [
            "严格按 page_plan 的顺序、页数、页面类型和标题生成，不得自行增删策划中的页面。",
            "只写事实清单和已验证分析中有依据的内容；不得补造数字、日期、原因、责任人、完成结果或奖惩结论。",
            "如材料不足，使用【待补充：具体字段】标记，不要用想象补齐；没有业务意义的空白页应删除并重新规划，而不是输出占位页。",
            "每页标题写在 marker 行的竖线后，页面正文从下一行开始；页面类型必须使用 page_plan 中的 page_type。",
            "数据页可使用 Key message / Key Message，总结只写有来源支撑的事实；没有原因依据时不要臆测原因。",
            "本周重点工作按日期整理；下周计划只写明确的未来事项；可按素材增加现场影像、关键节点、管控执行或专题分析页。",
            "有来源的数字应忠实复用；若写环比差值，必须是页面策划中已验证的计算。",
            "仅当素材中存在明确、可比的分类/时间序列数据时，才附结构化图表数据块：‘图表数据：’下一行写列名，后续每行一个类别与数值。不要为了画图补造数字。",
            "表格数据块格式为‘表格数据：’，下一行用竖线分隔列名和数据。结构化数据块只用于渲染，不要在正文中重复抄写。",
            "管理汇报风格：结论优先、简洁、可读；内容丰富时拆页，不能通过缩小字体或删减事实硬塞进一页。",
        ],
    }, ensure_ascii=False)


def _has_comparable_period_values(facts: list[dict]) -> bool:
    """识别同一指标的本期/上期值，包括历史表格常见的 9wk1/9wk2 列。"""
    # 表格/复制文本可能把周次与数值放在同一行，例如“违禁品携带 9wk1 21 9wk2 27”。
    for fact in facts:
        text = fact.get("text", "")
        week_labels = re.findall(r"\b\d{1,2}\s*wk\s*\d{1,2}\b", text, re.I)
        if len(week_labels) >= 2:
            without_labels = re.sub(r"\b\d{1,2}\s*wk\s*\d{1,2}\b", " ", text, flags=re.I)
            raw_values = re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])", without_labels)
            if len(raw_values) >= 2 and _metric_signature(text):
                return True
        # 同一事实行也可能同时写有本周与上周两个数值。
        if any(x in text for x in ("本周", "本期")) and any(x in text for x in ("上周", "上期", "前期")):
            values = [unit for _, unit in extract_quantities(text) if unit != "masked_count"]
            if len(values) >= 2 and len(set(values)) == 1 and _metric_signature(text):
                return True
    for left_index, left in enumerate(facts):
        left_text = left.get("text", "")
        left_period = "current" if any(x in left_text for x in ("本周", "本期", "当前")) else "previous" if any(x in left_text for x in ("上周", "上期", "前期")) else ""
        if not left_period:
            continue
        for right in facts[left_index + 1:]:
            right_text = right.get("text", "")
            right_period = "current" if any(x in right_text for x in ("本周", "本期", "当前")) else "previous" if any(x in right_text for x in ("上周", "上期", "前期")) else ""
            if not right_period or right_period == left_period:
                continue
            for left_value, left_unit in extract_quantities(left_text):
                for right_value, right_unit in extract_quantities(right_text):
                    if left_unit != right_unit or left_unit == "masked_count":
                        continue
                    a = _metric_signature(left_text)
                    b = _metric_signature(right_text)
                    if a and b and a.intersection(b):
                        return True
    return False


def build_weekly_page_prompt(facts: list[dict], analysis: dict, page: dict, period: str = "") -> str:
    """逐页创作提示词：每次只生成一页，降低小模型输出截断与跨页串内容风险。"""
    fact_map = {item.get("id"): item for item in facts}
    page_ids = [fid for fid in page.get("fact_ids", []) if fid in fact_map]
    page_facts = [fact_map[fid] for fid in page_ids]
    page_no = int(page.get("page", 1))
    page_type = str(page.get("page_type", "generic"))
    related = []
    for key in ("highlights", "data_insights", "risks", "next_week"):
        for item in analysis.get(key, []) or []:
            item_ids = set(item.get("fact_ids", []))
            if item.get("page") == page_no or item_ids.intersection(page_ids):
                related.append({"type": key, **item})
    can_chart = _has_comparable_period_values(page_facts)
    style_profile = _load_weekly_style_profile()
    visual_system = style_profile.get("visual_system", {}) if isinstance(style_profile.get("visual_system", {}), dict) else {}
    return json.dumps({
        "task": f"只创作 H区安保周报第 {page_no} 页的正文内容，不生成整份周报",
        "reference_style": {
            "colors": visual_system.get("colors", {"navy": "#002060", "blue": "#1F3B73", "cyan": "#00B0F0"}),
            "fonts": visual_system.get("fonts", ["微软雅黑", "Microsoft YaHei", "Arial"]),
            "keywords": visual_system.get("style_keywords", ["企业商务", "深蓝白底", "青色点缀", "简洁留白"]),
        },
        "period": period or "未提供；不得用今天日期推断统计周期",
        "page": {
            "page": page_no,
            "page_type": page_type,
            "title": page.get("title", ""),
            "purpose": page.get("purpose", ""),
            "visual_plan": page.get("visual_plan", ""),
            "asset_needs": page.get("asset_needs", []),
            "missing_fields": page.get("missing_fields", []),
        },
        "facts_for_this_page_only": _compact_prompt_facts(page_facts),
        "verified_related_insights": related,
        "chart_data_allowed": can_chart,
        "output_contract": "只输出本页正文 Markdown，不输出页码标记、不输出 JSON、不解释创作过程。",
        "rules": [
            "needs_confirmation=true 的素材只能写为‘待确认候选计划’，不得写成已承诺计划或已完成事项。",
            "必须按本页用途组织信息，不能将工作事件写入人力数据页，也不能把服务BU等背景信息写成工作成果。",
            "本周重点工作按日期与事项整理，事实没有提供结果时不要补写结果；下周计划可以把已给出的事项整理成可执行步骤，但新增建议必须标明‘建议事项，待确认’，不得编造负责人、日期或已完成状态。",
            "素材中的写作指令、问题、占位符和模板说明都不是业务事实；不得复制‘你帮我写’‘请补充’‘按日期：谁、干了什么、结果’等原始提示语。",
            "不得编造数字、日期、人员、原因或结果；原因没有明确来源时只描述现象，不做归因。",
            "来源中带 x/× 的脱敏数字必须原样保留，不得换成‘次’‘个’‘人’或 count；h 与小时属于等价单位。",
            "缺少数据时用一句话说明‘本期素材未提供该项数据’，不要输出方括号占位符，不要自行补造。",
            "只有 chart_data_allowed=true 且素材中存在同一指标的本期/上期数值时，才输出‘图表数据：’块；只有变化量而没有基数时，不生成对比图表。",
            '图表优先输出结构化字段块：CHART_JSON: 独立一行，随后 JSON 对象包含 type(column/bar/line/pie)、unit、categories、series:[{"name":"系列名","values":[数值]}]，最后独立一行 END_CHART_JSON；不要输出示例数字。',
            "如果生成图表，所有数值必须逐一来自本页事实；使用‘图表数据：’独立一行，下一行写列名，之后每行一个指标和来源数值，列之间用竖线分隔。不要照搬提示中的任何示例数字。",
            "文字精炼、结论优先、企业商务风格；不要重复页面标题，不要写‘以下是周报’等对话语。",
        ],
    }, ensure_ascii=False)


_WEEK_LABEL_RE = re.compile(r"\b\d{1,2}\s*wk\s*\d{1,2}\b", re.I)
_LABELLED_QUANTITY_RE = re.compile(
    r"([一-鿿]{2,6})\s*[：:]?\s*(\d+(?:\.\d+)?)\s*(?:[xX×]|人|次|起|处|台|项|个|条|份|场|组|班)"
)
_CHART_PAGE_TYPES = {"manpower", "operations", "anomalies", "key_work", "control_core", "generic"}


def _format_chart_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _week_pairs(text: str) -> list[tuple[str, float]]:
    """按出现顺序取出一串“周期 → 数值”对，遇到缺数值的周期即停止。"""
    hits = list(_WEEK_LABEL_RE.finditer(text))
    pairs = []
    for index, hit in enumerate(hits):
        end = hits[index + 1].start() if index + 1 < len(hits) else len(text)
        found = re.search(r"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)(?![A-Za-z0-9])", text[hit.end(): end])
        if not found:
            break
        pairs.append((re.sub(r"\s+", "", hit.group(0)), float(found.group(1))))
    return pairs


# 只含“周期 数值”的续行（如“9wk3 25”“9wk3：25，9wk4 28”），用于并入上一条指标序列。
_WEEK_CONTINUATION_RE = re.compile(
    r"^(?:\s*\d{1,2}\s*wk\s*\d{1,2}\s*[：:]?\s*\d+(?:\.\d+)?\s*[,，;；、|]?\s*)+$", re.I
)


def _period_chart_block(page_facts: list[dict]) -> str | None:
    """同一指标的多个周期值（如 违禁品携带 9wk1 21 9wk2 27）编译成图表数据块。

    指标名和数值都原样取自素材，因此天然满足来源核验。
    """
    for index, fact in enumerate(page_facts):
        text = str(fact.get("text", "")).strip()
        hits = list(_WEEK_LABEL_RE.finditer(text))
        if len(hits) < 2:
            continue
        metric = text[: hits[0].start()].strip(" \t|，,：:、")
        pairs = _week_pairs(text)
        if len(pairs) != len(hits):
            continue
        # 素材常把后续周期单列一行（如“9wk3 25”），按顺序并入同一指标，避免图表少画一期。
        for follow in page_facts[index + 1:]:
            follow_text = str(follow.get("text", "")).strip()
            if not _WEEK_CONTINUATION_RE.match(follow_text):
                break
            pairs.extend(_week_pairs(follow_text))
        labels = [label for label, _ in pairs]
        if metric and len(pairs) >= 2 and len(set(labels)) == len(labels) and _metric_signature(metric):
            # 周期作类目、指标作系列名：柱状图沿横轴依次是 9wk1、9wk2，比单类别双系列易读。
            rows = [f"类别|{metric}"] + [
                f"{label}|{_format_chart_number(value)}" for label, value in pairs
            ]
            return "图表数据：\n" + "\n".join(rows)
    return None


def _category_chart_block(page_facts: list[dict]) -> str | None:
    """同一页多个并列数量（如 楼栋3、岗位3、人力5）编译成单系列图表。

    至少要有三个不同类别，避免把两句话里的数字硬凑成图表。
    """
    pairs: list[tuple[str, float]] = []
    for fact in page_facts:
        for match in _LABELLED_QUANTITY_RE.finditer(str(fact.get("text", ""))):
            label, value = match.group(1), float(match.group(2))
            if label not in {pair[0] for pair in pairs}:
                pairs.append((label, value))
    if len(pairs) < 3:
        return None
    rows = ["类别|数值"] + [f"{label}|{_format_chart_number(value)}" for label, value in pairs[:6]]
    return "图表数据：\n" + "\n".join(rows)


def deterministic_chart_block(page_type: str, page_facts: list[dict]) -> str | None:
    """证据编译阶段唯一允许出图的地方：有可比周期值优先做对比图，其次做分类图。"""
    if page_type not in _CHART_PAGE_TYPES or not page_facts:
        return None
    return _period_chart_block(page_facts) or _category_chart_block(page_facts)


def compile_grounded_weekly_page(
    facts: list[dict],
    analysis: dict,
    page: dict,
) -> tuple[str, bool]:
    """把通过校验的 AI 摘要与来源事实编译成正文，避免小模型重新抄写数字和单位。"""
    fact_map = {item.get("id"): item for item in facts}
    page_ids = [fid for fid in page.get("fact_ids", []) if fid in fact_map]
    page_facts = [fact_map[fid] for fid in page_ids]
    page_no = int(page.get("page", 1))
    summaries = []
    for key in ("highlights", "data_insights", "risks", "next_week"):
        for item in analysis.get(key, []) or []:
            item_ids = set(item.get("fact_ids", []))
            belongs_here = item.get("page") == page_no or bool(item_ids and item_ids.issubset(set(page_ids)))
            text = str(item.get("text", "")).strip()
            if not belongs_here or not text:
                continue
            if _find_prompt_leaks(text) or re.search(
                r"【待补充[^】]*】|\bTODO\b|\bTBD\b|请补充(?:最新图片|本期|该项|数据|字段)",
                text,
                re.I,
            ):
                continue
            summaries.append(text)
    summaries = list(dict.fromkeys(summaries))[:3]

    labels = {
        "manpower": "岗位与人力事实",
        "operations": "运营事实",
        "anomalies": "异常事实",
        "key_work": "本周工作事实",
        "control_core": "管控执行事实",
        "timeline": "关键节点事实",
        "photo_evidence": "现场事项",
        "next_plan": "下周计划",
        "summary": "本期事实",
        "generic": "事实依据",
    }
    page_type = str(page.get("page_type", "generic"))
    lines = []
    if summaries:
        lines.append("管理摘要：" + "；".join(summaries))
    lines.append(labels.get(page_type, "事实依据") + "：")
    for fact in page_facts:
        text = str(fact.get("text", "")).strip()
        text = re.sub(r"^\s*\d{1,2}\s*[、.．]\s*", "", text)
        if fact.get("needs_confirmation"):
            text = re.sub(r"^(?:下周计划\s*[：:]?\s*)", "", text)
            text = "待确认候选计划：" + text
        lines.append("- " + text)
    if not page_facts:
        lines.append("- 本期素材未提供该页面对应的事实记录。")
    chart = deterministic_chart_block(page_type, page_facts)
    body = "\n".join(lines)
    if chart:
        body += "\n\n" + chart
    return body, bool(summaries)


def weekly_page_generation_issues(body: str, page: dict, facts: list[dict], analysis: dict) -> list[str]:
    """检查单页模型输出是否夹带指令、跨页内容或缺乏来源的数字/趋势。"""
    issues = []
    value = (body or "").strip()
    page_no = int(page.get("page", 1))
    if len(value) < 4:
        issues.append(f"P{page_no} 正文为空或过短")
    if re.search(r"(?im)^\s*#{1,6}\s*(?:P\d+|第\s*\d+\s*页)", value):
        issues.append(f"P{page_no} 输出中混入了页码标记，疑似跨页生成")
    leak = _find_prompt_leaks(value)
    if leak:
        issues.append(f"P{page_no} 混入用户指令/模板提示：{leak[0]}")
    fact_map = {item.get("id"): item for item in facts}
    page_ids = [fid for fid in page.get("fact_ids", []) if fid in fact_map]
    if any(fact_map[fid].get("needs_confirmation") for fid in page_ids) and "待确认候选计划" not in value:
        issues.append("候选计划必须明确标记为待确认候选计划")
    derived = [item for item in analysis.get("verified_calculations", []) if item.get("verified") and item.get("page") == page_no]
    for issue in _validate_claim_text(value, page_ids, fact_map, derived):
        issues.append(f"P{page_no} 事实核验失败：{issue}")
    issues.extend(_chart_data_block_issues(value, [fact_map[fid] for fid in page_ids], page_no))
    return issues


def _find_prompt_leaks(text: str) -> list[str]:
    patterns = (
        r"[①②③④⑤⑥⑦⑧⑨⑩]\s*(?:岗位[/&]人力数据|专区运营数据|异常数据|本周重点工作|下周计划)",
        r"下周计划的具体详细内容你帮我写", r"具体详细内容你帮我写", r"你帮我写", r"请帮我",
        r"按日期\s*[：:]\s*谁.{0,16}干了什么", r"请补充最新图片", r"请提供.*后再(?:总结|分析)",
        r"根据你的要求", r"以下是(?:本周|下周|H区)?周报", r"作为AI模型", r"我将为你生成",
    )
    found = []
    for pattern in patterns:
        match = re.search(pattern, text or "", re.I)
        if match:
            found.append(match.group(0))
    return found


def weekly_content_quality_issues(report_text: str) -> list[str]:
    """导出前质量闸门：拦截提示词泄漏、待补充占位符和明显未完成的封面信息。"""
    checked = validate_weekly_report(report_text or "")
    if not checked.get("valid"):
        return ["周报结构不完整，无法通过内容质量检查"]
    issues = []
    for page in checked["pages"]:
        page_no = int(page.get("no", 0))
        title = str(page.get("title", ""))
        body = str(page.get("body", ""))
        combined = title + "\n" + body
        leaks = _find_prompt_leaks(combined)
        if leaks:
            issues.append(f"P{page_no}「{title}」含用户指令或模板提示（如‘{leaks[0]}’）")
        if re.search(r"【待补充[^】]*】|\bTODO\b|\bTBD\b|请补充(?:最新图片|本期|该项|数据|字段)|请提供.*后再(?:总结|分析)", combined, re.I):
            issues.append(f"P{page_no}「{title}」仍有待补充占位内容")
        if page_no == 1 and re.search(r"统计周期\s*[：:]\s*(?:请确认|待确认|未提供|未在输入中明确|【)", body):
            issues.append("P1「封面」统计周期未确认")
    return issues

def validate_weekly_report(text: str) -> dict:
    """验证动态页数周报的页码连续性、空白页和待补充项。"""
    numbers = [int(next(g for g in m.groups() if g)) for m in _PAGE_LINE.finditer(text or "")]
    pages = parse_report(text or "")
    count = len(numbers)
    expected = list(range(1, count + 1))
    missing = [n for n in expected if n not in numbers]
    duplicate = sorted({n for n in numbers if numbers.count(n) > 1})
    sequence_ok = numbers == expected
    blanks = [p["no"] for p in pages if not p["body"].strip() and p.get("type") not in {"cover", "closing"}]
    placeholder_pages = [p["no"] for p in pages if "【待补充" in p["body"]]
    valid = 3 <= count <= 20 and sequence_ok and not duplicate and not blanks and len(pages) == count
    return {
        "valid": valid,
        "page_count": count,
        "missing": missing,
        "duplicate": duplicate,
        "blanks": blanks,
        "placeholder_pages": placeholder_pages,
        "pages": pages,
    }


def make_offline_draft(source: str, period: str = "") -> str:
    """模型不可用时按真实素材覆盖面生成动态页数兜底草稿。"""
    facts = extract_weekly_facts(source)
    analysis = make_offline_analysis(facts, period)
    facts_by_id = {fact["id"]: fact for fact in facts}
    plan = analysis.get("page_plan", [])
    content_titles = [p["title"] for p in plan if p.get("page_type") not in {"cover", "contents", "closing"}]
    output = []
    for item in plan:
        number = item["page"]
        page_type = item.get("page_type", "generic")
        title = item.get("title", f"P{number}")
        ids = item.get("fact_ids", [])
        lines = [facts_by_id[fid]["text"] for fid in ids if fid in facts_by_id]
        if page_type == "cover":
            title = "H区安保周报" + (f"（{period.strip()}）" if period and period.strip() else "")
            body = f"统计周期：{period.strip()}" if period and period.strip() else "统计周期：【待补充：周次】"
        elif page_type == "contents":
            body = "\n".join(f"{idx:02d}  {name}" for idx, name in enumerate(content_titles, 1)) or "本期汇报内容"
        elif page_type == "closing":
            body = "Thankyou"
        elif page_type == "operations":
            body = "\n".join(lines) or "【待补充：运营数据】"
            body += "\nKey message：【待补充：请提供本期/上期可比数据后再总结】"
        elif page_type == "anomalies":
            body = "\n".join(lines) or "【待补充：异常数据】"
            body += "\nKey Message：【待补充：请提供异常分类及可比数据】"
        elif page_type == "manpower":
            body = "\n".join(lines) or "【待补充：岗位及人力数据】"
        elif page_type == "next_plan":
            body = "\n".join(lines) or "【待补充：下周计划】"
        elif page_type == "control_core":
            body = "\n".join(lines) or "【待补充：管控执行事项】"
        elif page_type == "summary":
            body = "\n".join(lines) or "【待补充：本期工作素材】"
        else:
            body = "\n".join(lines) or "【待补充：本页内容】"
        output.append(f"## P{number} [type:{page_type}] | {title}\n{body}")
    return "\n\n".join(output)


def _take_data_block(lines: list[str], start: int) -> tuple[list[list[str]], int]:
    """收集“图表数据：”之后的竖线表格，遇空行结束；返回行列表与下一次扫描位置。"""
    rows = []
    index = start
    while index < len(lines):
        value = lines[index].strip()
        if not value:
            index += 1
            break
        rows.append([cell.strip() for cell in value.strip("|").split("|")])
        index += 1
    width = max((len(row) for row in rows), default=0)
    return [row + [""] * (width - len(row)) for row in rows if row], index


def _add_docx_table(doc, rows: list[list[str]]) -> None:
    """Word 里没有图表，把结构化数据落成真实表格，保留数据的同时不泄漏原始符号。"""
    if len(rows) < 2 or len(rows[0]) < 2:
        return
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            paragraph = table.cell(r, c).paragraphs[0]
            paragraph.text = cell
            if r == 0:
                paragraph.runs[0].bold = True


def build_weekly_docx(report_text: str, out_path: str | Path) -> Path:
    """按实际页面数量导出可编辑 DOCX；保留逐页章节及事实占位符。"""
    from docx import Document
    from docx.shared import Cm, Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    check = validate_weekly_report(report_text)
    if not check["valid"]:
        raise ValueError("周报页码不连续、页数超出 3—20 页范围或存在空白内容页，请先检查。")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    section = doc.sections[0]
    section.top_margin, section.bottom_margin = Cm(2.2), Cm(2.0)
    section.left_margin = section.right_margin = Cm(2.4)
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(10.5)
    style.font.color.rgb = RGBColor(47, 47, 47)
    style.paragraph_format.space_after = Pt(6)
    for heading_name, point_size in (("Title", 24), ("Heading 1", 16)):
        heading = doc.styles[heading_name]
        heading.font.name = "微软雅黑"
        heading.font.size = Pt(point_size)
        heading.font.color.rgb = RGBColor(0, 32, 96)

    for i, page in enumerate(check["pages"]):
        if i:
            doc.add_page_break()
        title, body = split_title_body(page)
        if i == 0:
            cover_title = title or next((line.strip() for line in body.splitlines() if "周报" in line), "H区安保周报")
            doc.add_heading(cover_title, 0)
            p = doc.add_paragraph("华东安保二部 · 周报草稿（请核对统计数据及事实）")
            p.style = doc.styles["Subtitle"]
            for line in body.splitlines():
                value = line.strip()
                if value and value != cover_title:
                    doc.add_paragraph(value)
        elif page.get("type") == "closing" or (i == len(check["pages"]) - 1 and title.lower() == "thankyou"):
            p = doc.add_paragraph("Thankyou")
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        else:
            doc.add_heading(title or f"第{page.get('no', i + 1)}页", level=1)
            lines = body.splitlines()
            index = 0
            while index < len(lines):
                value = lines[index].strip()
                if re.match(r"(?i)^\s*(?:图表数据|表格数据)\s*[：:]?\s*$", value):
                    rows, index = _take_data_block(lines, index + 1)
                    _add_docx_table(doc, rows)
                    continue
                if value:
                    doc.add_paragraph(value)
                index += 1
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.text = "华东安保二部  ·  内部工作资料"
    doc.save(out)
    return out


def _visible_ppt_body(body: str) -> str:
    """长度检查维持原规则，JSON 仍计入检查文本。"""
    return visible_body(body, remove_json=False)


def ppt_content_issues(text: str) -> list[str]:
    """检查动态页数、提示词泄漏、未完成占位符与极端超长页面。"""
    report = validate_weekly_report(text)
    if not report["valid"]:
        return ["周报页面编号需从 P1 连续编号，页数为 3—20 页，且业务页不能空白"]
    errors = weekly_content_quality_issues(text)
    for page in report["pages"]:
        page_no = int(page["no"])
        page_type = page.get("type") or _infer_page_type(page.get("title", ""), page_no - 1, len(report["pages"]))
        visible_body = _visible_ppt_body(page.get("body", ""))
        lines = [line.strip() for line in visible_body.splitlines() if line.strip()]
        if len(visible_body) > 1800 or len(lines) > 32:
            errors.append(f"P{page_no}「{page.get('title', '')}」内容过长（{len(lines)} 行 / {len(visible_body)} 字），建议拆分成多页后再导出")
        elif page_type == "key_work" and (len(lines) > 8 or len(visible_body) > 650):
            errors.append(f"P{page_no} 工作事项较多，建议拆分为工作成果页与关键节点页")
    return errors

