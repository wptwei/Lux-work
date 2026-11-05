"""原始 PPT 模板映射、内容填充与导出前检查。

优先使用显式占位符（如 {{P4_TITLE}}、{{P4_KEY_MESSAGE}}）；
未配置占位符时使用保守的形状识别规则。固定图片、Logo、页脚默认保留。
静态图片中的图表不会被假装更新；只有原生图表或明确的图表占位区域才能动态更新。
"""
from __future__ import annotations

from core.report_data import (
    _extract_block, _split_rows, _to_number, _parse_chart_data, visible_body,
    _CHART_BLOCK_RE, _TABLE_BLOCK_RE, _KEY_MESSAGE_RE,
)

import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from core.report_data import parse_report, split_title_body
from core.paths import config_path

TOTAL = 8
EMU_PER_INCH = 914400

_FIXED_TEXT = (
    "华东安保二部", "internal use", "confidential", "thankyou", "contents",
    "security service project", "this week's key tasks", "next week's key work plan",
)
_PLACEHOLDER_TERMS = (
    "待补充", "每周更新", "请填写", "具体事项", "数据图表", "图表（", "图表(",
    "[表格", "[图表", "[图片", "placeholder", "{{",
)
_DATE_RE = re.compile(r"\b(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}(?:日|号)?")


def _replace_frame_text(frame, text: str, shape=None, auto_fit: bool = True) -> None:
    """替换文本并尽量保留首段/首个 run 的字体样式。"""
    text = str(text or "")
    paragraph = frame.paragraphs[0] if frame.paragraphs else frame.add_paragraph()
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.text = text
    for extra in list(frame.paragraphs[1:]):
        extra._p.getparent().remove(extra._p)
    if auto_fit:
        try:
            frame.word_wrap = True
            frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
        except Exception:
            pass


def _replace_cell_text(cell, text: str) -> None:
    _replace_frame_text(cell.text_frame, text, auto_fit=True)


def _frame_text(shape) -> str:
    try:
        return shape.text_frame.text.strip() if shape.has_text_frame else ""
    except Exception:
        return ""


def _shape_box(shape) -> tuple[float, float, float, float]:
    return tuple(float(getattr(shape, key, 0) or 0) / EMU_PER_INCH for key in ("left", "top", "width", "height"))


def _is_footer(text: str) -> bool:
    lowered = (text or "").strip().lower()
    return any(token in lowered for token in _FIXED_TEXT) or bool(re.fullmatch(r"\d{1,2}\s*/\s*0?8", lowered))


def _is_chart_placeholder(text: str) -> bool:
    text = (text or "").strip().lower()
    return any(term in text for term in (
        "数据图表（每周更新）", "数据图表(每周更新)", "图表（每周更新）", "图表(每周更新)",
        "趋势图（每周更新）", "趋势图(每周更新)", "[图表", "[数据图表", "chart placeholder",
    ))


def _is_picture_placeholder(text: str) -> bool:
    text = (text or "").strip().lower()
    return any(term in text for term in ("现场照片", "厂区分布图", "图片（每周更新）", "照片（每周更新）"))


def _load_shape_map() -> dict:
    """读取可选的形状名称映射；不存在或损坏时使用自动识别。"""
    path = config_path("weekly_ppt_template_map.json")
    if not path.exists():
        return {"version": 1, "slides": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"version": 1, "slides": {}}
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "slides": {}}


def _configured_names(map_config: dict, page_no: int, role: str) -> list[str]:
    slides = map_config.get("slides", {}) if isinstance(map_config, dict) else {}
    page = slides.get(str(page_no), {}) if isinstance(slides, dict) else {}
    names = page.get(role, []) if isinstance(page, dict) else []
    return [str(name).strip() for name in names if str(name).strip()] if isinstance(names, list) else []


def _find_named_shapes(slide, names: list[str]) -> list[Any]:
    wanted = {name.casefold() for name in names}
    return [shape for shape in slide.shapes if getattr(shape, "name", "").casefold() in wanted]


def _iter_text_frames(shapes):
    """递归遍历形状（含组合、表格）里的文本框架。"""
    for shape in shapes:
        try:
            is_group = shape.shape_type == MSO_SHAPE_TYPE.GROUP
        except Exception:
            is_group = False
        if is_group:
            yield from _iter_text_frames(shape.shapes)
            continue
        if getattr(shape, "has_text_frame", False):
            yield shape.text_frame
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    yield cell.text_frame


def _iter_shapes_recursive(shapes):
    for shape in shapes:
        yield shape
        try:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from _iter_shapes_recursive(shape.shapes)
        except Exception:
            continue


def _has_markers(prs) -> bool:
    for slide in prs.slides:
        for frame in _iter_text_frames(slide.shapes):
            if re.search(r"\{\{[^{}]+\}\}", frame.text or ""):
                return True
    return False


def _slide_markers(slide) -> list[str]:
    markers = []
    for frame in _iter_text_frames(slide.shapes):
        markers.extend(re.findall(r"\{\{([^{}]+)\}\}", frame.text or ""))
    return markers


def _has_full_page_marker(slide, page_no: int) -> bool:
    markers = _slide_markers(slide)
    return any(marker.upper() in {f"P{page_no}", f"P{page_no}_BODY"} for marker in markers)


def _replace_in_frame(frame, mapping: dict) -> int:
    """支持 marker 被拆分到同一段多个 run 的情况，返回替换次数。"""
    replaced = 0
    for paragraph in frame.paragraphs:
        original = "".join(run.text for run in paragraph.runs)
        if not original:
            continue
        updated = original
        for key, value in mapping.items():
            if key in updated:
                count = updated.count(key)
                updated = updated.replace(key, str(value))
                replaced += count
        if updated == original:
            continue
        if paragraph.runs:
            paragraph.runs[0].text = updated
            for run in paragraph.runs[1:]:
                run.text = ""
        else:
            paragraph.text = updated
    return replaced


def _apply_markers(prs, mapping: dict) -> tuple[int, int]:
    replacements = 0
    frames_changed = 0
    for slide in prs.slides:
        for frame in _iter_text_frames(slide.shapes):
            changed = _replace_in_frame(frame, mapping)
            replacements += changed
            if changed:
                frames_changed += 1
    return replacements, frames_changed


def _extract_key_message(body: str, label: str = "Key Message") -> str:
    lines = (body or "").splitlines()
    for line in lines:
        match = _KEY_MESSAGE_RE.match(line.strip())
        if match:
            return match.group(1).strip() or "【待补充：本页结论】"
    return ""


def _strip_structured_blocks(body: str) -> str:
    """兼容入口：模板额外支持英文数据块标签。"""
    return visible_body(body, include_english=True)


def _body_without_key_message(body: str) -> str:
    output = []
    for line in _strip_structured_blocks(body).splitlines():
        if _KEY_MESSAGE_RE.match(line.strip()):
            continue
        output.append(line)
    return "\n".join(output).strip()


def _build_category_chart_data(parsed: dict) -> CategoryChartData:
    data = CategoryChartData()
    data.categories = parsed["categories"]
    for name, values in zip(parsed["series_names"], parsed["series"]):
        data.add_series(name, values)
    return data


def _fill_table(table, body: str, page_no: int, diagnostics: dict) -> bool:
    """将结构化表格或“字段：值”填入现有表格，保留可识别的表头。"""
    structured = _extract_block(body, "table")
    rows = _split_rows(structured) if structured else []
    if not rows:
        lines = [ln.strip() for ln in _body_without_key_message(body).splitlines() if ln.strip()]
        rows = []
        for line in lines:
            if _KEY_MESSAGE_RE.match(line):
                continue
            if "：" in line or ":" in line:
                key, _, value = line.replace("：", ":").partition(":")
                rows.append([key.strip(), value.strip()])
    if not rows:
        return False

    col_count = len(table.columns)
    header_tokens = {"岗位", "项目", "类别", "指标", "名称", "类型", "区域", "项目名称"}
    first_row = [table.cell(0, c).text.strip() for c in range(col_count)] if len(table.rows) else []
    preserve_header = bool(first_row and any(any(token in cell for token in header_tokens) for cell in first_row if cell))
    start_row = 1 if preserve_header else 0
    available = max(0, len(table.rows) - start_row)
    if len(rows) > available:
        diagnostics["warnings"].append(
            f"P{page_no} 表格容量不足：有 {len(rows)} 行数据，模板仅可容纳 {available} 行；多余内容未静默丢弃。"
        )
    for ri, row in enumerate(rows[:available], start=start_row):
        for ci in range(col_count):
            value = row[ci] if ci < len(row) else ""
            _replace_cell_text(table.cell(ri, ci), value)
    diagnostics["updated_tables"] += 1
    diagnostics["pages"].setdefault(page_no, []).append(f"更新表格 {min(len(rows), available)} 行")
    return True


def _estimate_overflow(shape, text: str) -> bool:
    """粗略检测长文本是否可能超出形状容量；最终仍需以 PowerPoint 渲染验收。"""
    if not getattr(shape, "has_text_frame", False) or not text:
        return False
    x, y, width, height = _shape_box(shape)
    if width <= 0 or height <= 0:
        return False
    try:
        size = shape.text_frame.paragraphs[0].runs[0].font.size.pt
    except Exception:
        size = None
    size = float(size or 16)
    chars_per_line = max(1.0, width * 72 / max(size * 0.92, 1))
    needed_lines = sum(max(1, math.ceil(len(line) / chars_per_line)) for line in text.splitlines())
    capacity = max(1.0, height * 72 / max(size * 1.22, 1))
    return needed_lines > capacity * 1.25


def _write_shape(shape, text: str, page_no: int, role: str, diagnostics: dict) -> None:
    old = _frame_text(shape)
    if old == str(text or "").strip():
        return
    _replace_frame_text(shape.text_frame, text, shape=shape, auto_fit=True)
    diagnostics["updated_text_frames"] += 1
    diagnostics["pages"].setdefault(page_no, []).append(f"更新{role}：{getattr(shape, 'name', '文本框')}")
    if role != "封面日期" and _estimate_overflow(shape, str(text or "")):
        diagnostics["warnings"].append(f"P{page_no}「{getattr(shape, 'name', '文本框')}」文字较多，已启用适配；建议检查实际排版。")


def _find_title_shape(slide, page_no: int, map_config: dict):
    configured = _find_named_shapes(slide, _configured_names(map_config, page_no, "title"))
    if configured:
        return configured[0]
    try:
        if slide.shapes.title is not None:
            return slide.shapes.title
    except Exception:
        pass
    candidates = []
    for shape in slide.shapes:
        text = _frame_text(shape)
        if not text or _is_footer(text) or _KEY_MESSAGE_RE.search(text):
            continue
        if not getattr(shape, "has_text_frame", False):
            continue
        x, y, width, height = _shape_box(shape)
        if page_no == 1:
            if "周报" in text and width >= 2.0:
                candidates.append((y, -width, shape))
        elif y <= 1.55 and width >= 2.4 and not _is_chart_placeholder(text) and not _is_picture_placeholder(text):
            candidates.append((y, -width, shape))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2] if candidates else None


def _find_key_message_shape(slide, page_no: int | None = None, map_config: dict | None = None):
    if page_no is not None and map_config:
        configured = _find_named_shapes(slide, _configured_names(map_config, page_no, "key_message"))
        if configured:
            return configured[0]
    for shape in slide.shapes:
        text = _frame_text(shape)
        if text and re.search(r"key\s*message", text, re.I):
            return shape
    return None


def _find_body_shape(slide, page_no: int, map_config: dict, exclude: set[int] | None = None):
    exclude = exclude or set()
    configured = _find_named_shapes(slide, _configured_names(map_config, page_no, "body"))
    for shape in configured:
        if id(shape) not in exclude and getattr(shape, "has_text_frame", False):
            return shape
    candidates = []
    for shape in slide.shapes:
        if id(shape) in exclude or not getattr(shape, "has_text_frame", False):
            continue
        text = _frame_text(shape)
        if not text or _is_footer(text) or re.search(r"key\s*message", text, re.I):
            continue
        if _is_chart_placeholder(text) or _is_picture_placeholder(text):
            continue
        x, y, width, height = _shape_box(shape)
        if y < 1.2 or height < 0.25 or width < 1.2:
            continue
        if len(text) >= 18 or any(term in text for term in ("待补充", "具体事项", "每周更新", "已完成", "下周计划")):
            candidates.append((width * height, len(text), shape))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2] if candidates else None


def _find_chart_placeholders(slide):
    return [shape for shape in slide.shapes if getattr(shape, "has_text_frame", False) and _is_chart_placeholder(_frame_text(shape))]


def _remove_shape(shape) -> None:
    element = shape._element
    element.getparent().remove(element)


def _apply_chart_data(slide, body: str, page_no: int, diagnostics: dict, map_config: dict | None = None) -> None:
    from core.ppt_pipeline import render_chart
    try:
        parsed = _parse_chart_data(body)
        charts = [s for s in _iter_shapes_recursive(slide.shapes) if getattr(s, "has_chart", False)]
        placeholders = _find_chart_placeholders(slide)
        if not parsed:
            if charts or placeholders:
                diagnostics["warnings"].append(f"P{page_no} 缺少有效图表数据，原图表/占位区需核对")
            return
        targets = charts or placeholders
        if not targets:
            diagnostics["warnings"].append(f"P{page_no} 缺少明确图表区域，无法安全插入；请配置图表占位框")
            return
        old = targets[0]
        render_chart(slide, parsed, (old.left, old.top, old.width, old.height), old)
        diagnostics["updated_charts" if charts else "created_charts"] += 1
        if len(targets) > 1:
            diagnostics["warnings"].append(f"P{page_no} 仅提供一组数据，其余图表需核对")
    except Exception as exc:
        diagnostics["warnings"].append(f"P{page_no} 图表生成失败：{exc}")


def _page_content_body(body: str) -> str:
    """移除只用于图表/表格渲染的结构化块与 Key Message，保留普通正文。"""
    lines = []
    skipping = False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if _CHART_BLOCK_RE.match(stripped) or _TABLE_BLOCK_RE.match(stripped):
            skipping = True
            continue
        if skipping:
            if not stripped:
                skipping = False
            elif _KEY_MESSAGE_RE.match(stripped):
                skipping = False
                lines.append(line)
            continue
        if _KEY_MESSAGE_RE.match(stripped):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _fill_p6_items(slide, body: str, page_no: int, map_config: dict, diagnostics: dict) -> None:
    items = [line.strip(" •\t") for line in _body_without_key_message(body).splitlines() if line.strip()]
    if not items:
        return
    configured = _find_named_shapes(slide, _configured_names(map_config, page_no, "items"))
    boxes = configured or [
        shape for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
        and (_DATE_RE.search(_frame_text(shape)) or "X/X日" in _frame_text(shape) or "X/X日" in _frame_text(shape).replace(" ", ""))
        and not _is_footer(_frame_text(shape))
    ]
    boxes.sort(key=lambda shape: (_shape_box(shape)[1], _shape_box(shape)[0]))
    if not boxes:
        shape = _find_body_shape(slide, page_no, map_config)
        if shape:
            _write_shape(shape, "\n".join(items), page_no, "本周重点工作", diagnostics)
        else:
            diagnostics["warnings"].append(f"P{page_no} 未找到明确的工作事项文本框，正文未写入，需配置模板元素映射。")
        return
    if len(items) > len(boxes):
        diagnostics["warnings"].append(f"P{page_no} 有 {len(items)} 条工作事项，模板仅识别 {len(boxes)} 个条目框；已合并到最后一框，建议检查排版。")
        items = items[:len(boxes)-1] + ["\n".join(items[len(boxes)-1:])]
    for box, item in zip(boxes, items):
        _write_shape(box, item, page_no, "工作事项", diagnostics)


def _fill_p7_plan(slide, body: str, page_no: int, map_config: dict, diagnostics: dict) -> None:
    plan = _body_without_key_message(body)
    if not plan:
        return
    configured = _find_named_shapes(slide, _configured_names(map_config, page_no, "body"))
    shape = configured[0] if configured else _find_body_shape(slide, page_no, map_config)
    if shape:
        _write_shape(shape, plan, page_no, "下周计划", diagnostics)
    else:
        diagnostics["warnings"].append(f"P{page_no} 未找到明确的计划正文区域，未覆盖固定版式内容。可在模板元素映射中指定 body 形状名称。")


def _fill_historical_slide(slide, page: dict, page_no: int, map_config: dict, diagnostics: dict) -> None:
    title, body = split_title_body(page)
    if page_no == 1:
        cover_title = title
        if "周报" not in cover_title:
            for line in body.splitlines():
                if "周报" in line:
                    cover_title = line.strip()
                    break
        title_shape = _find_title_shape(slide, page_no, map_config)
        if title_shape and cover_title:
            _write_shape(title_shape, cover_title, page_no, "封面标题", diagnostics)
        today = date.today()
        date_str = f"{today.year}/{today.month}/{today.day}"
        date_shapes = _find_named_shapes(slide, _configured_names(map_config, page_no, "date"))
        date_shapes += [shape for shape in slide.shapes if _frame_text(shape) and "Rev." in _frame_text(shape) and shape not in date_shapes]
        for shape in date_shapes:
            _write_shape(shape, f"By : Security team   Date： {date_str} Rev. 01", page_no, "封面日期", diagnostics)
        return

    if page_no == 2:
        # 目录通常为固定内容，默认不改动；如果模板本身有 title marker 则由 marker 模式处理。
        return
    if page_no == 8:
        # 固定结束页默认保留原样。
        return

    title_shape = _find_title_shape(slide, page_no, map_config)
    if title_shape and title:
        _write_shape(title_shape, title, page_no, "页面标题", diagnostics)

    key_message = _extract_key_message(body)
    key_shape = _find_key_message_shape(slide, page_no, map_config)
    if key_shape and key_message:
        old = _frame_text(key_shape)
        if re.search(r"key\s*message", old, re.I):
            # 保留大小写标签，仅更新冒号后的正文。
            label = "Key message" if re.search(r"Key message", old) else "Key Message"
            new_text = f"{label}：{key_message}"
        else:
            new_text = key_message
        _write_shape(key_shape, new_text, page_no, "Key Message", diagnostics)

    if page_no == 3:
        configured_tables = _find_named_shapes(slide, _configured_names(map_config, page_no, "table"))
        table_shapes = configured_tables or [shape for shape in list(slide.shapes) if getattr(shape, "has_table", False)]
        for shape in table_shapes:
            if not getattr(shape, "has_table", False):
                continue
            table = shape.table
            table_text = " ".join(cell.text for row in table.rows for cell in row.cells).lower()
            if key_message and "key message" in table_text:
                if len(table.rows) >= 1 and len(table.columns) == 1:
                    _replace_cell_text(table.cell(0, 0), "Key Message：")
                if len(table.rows) >= 2 and len(table.columns) == 1:
                    _replace_cell_text(table.cell(1, 0), key_message)
                diagnostics["updated_tables"] += 1
            elif len(table.columns) >= 2:
                _fill_table(table, body, page_no, diagnostics)
            elif key_message:
                if len(table.rows) >= 1:
                    _replace_cell_text(table.cell(0, 0), "Key Message：")
                if len(table.rows) >= 2:
                    _replace_cell_text(table.cell(1, 0), key_message)
                diagnostics["updated_tables"] += 1
        # 只替换明确的文本占位符，不覆盖静态图片和固定标签。
        for shape in slide.shapes:
            text = _frame_text(shape)
            if text and any(term in text for term in ("岗位/人数", "岗位/人力数据", "【待补充：岗位")):
                _write_shape(shape, _body_without_key_message(body), page_no, "人力数据", diagnostics)

    elif page_no in (4, 5):
        content = _page_content_body(body)
        # 对明确的文本占位符进行替换；不把普通叙述猜测为 KPI 数字。
        configured_body = [shape for shape in _find_named_shapes(slide, _configured_names(map_config, page_no, "body")) if getattr(shape, "has_text_frame", False)]
        placeholders = configured_body or [
            shape for shape in slide.shapes
            if getattr(shape, "has_text_frame", False)
            and any(term in _frame_text(shape) for term in ("【待补充", "[数据", "[异常", "指标内容待更新", "运营数据待更新"))
            and not _is_chart_placeholder(_frame_text(shape))
        ]
        if placeholders and content:
            _write_shape(placeholders[0], content, page_no, "运营/异常数据文本", diagnostics)
        elif content and not any(getattr(shape, "has_chart", False) for shape in slide.shapes):
            # 只填明确的正文框；静态图片图表保留，并提示不能自动更新。
            body_shape = _find_body_shape(slide, page_no, map_config)
            if body_shape and any(term in _frame_text(body_shape) for term in _PLACEHOLDER_TERMS):
                _write_shape(body_shape, content, page_no, "数据正文", diagnostics)
        if any(getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE for shape in slide.shapes):
            diagnostics["warnings"].append(f"P{page_no} 含图片；图片内嵌的图表/数字无法通过文本映射更新，请人工核对是否为旧数据。")

    elif page_no == 6:
        _fill_p6_items(slide, body, page_no, map_config, diagnostics)
    elif page_no == 7:
        _fill_p7_plan(slide, body, page_no, map_config, diagnostics)


def build_marker_map(pages: list[dict]) -> dict:
    """构建细粒度模板占位符映射。支持通用页标记与页面级内容槽。"""
    mapping = {"{{标题}}": pages[0].get("title", "H区安保周报") if pages else "H区安保周报", "{{TITLE}}": pages[0].get("title", "H区安保周报") if pages else "H区安保周报"}
    for index, page in enumerate(pages, start=1):
        page_title, page_body = split_title_body(page)
        body = page_body or "【待补充】"
        mapping[f"{{{{P{index}}}}}"] = "\n".join(part for part in (page_title, body) if part).strip() or "【待补充】"
        mapping[f"{{{{P{index}_TITLE}}}}"] = "Thankyou" if index == 8 else page_title or f"P{index}"
        mapping[f"{{{{P{index}_BODY}}}}"] = body
        mapping[f"{{{{P{index}_FACTS}}}}"] = _page_content_body(body) or "【待补充】"
        mapping[f"{{{{P{index}_KEY_MESSAGE}}}}"] = _extract_key_message(body) or "【待补充：本页结论】"
        chart_rows = _extract_block(body, "chart")
        table_rows = _extract_block(body, "table")
        mapping[f"{{{{P{index}_CHART_DATA}}}}"] = "\n".join(chart_rows) or "【未提供可更新的图表数据】"
        mapping[f"{{{{P{index}_TABLE_DATA}}}}"] = "\n".join(table_rows) or _page_content_body(body) or "【待补充】"
        mapping[f"{{{{P{index}_ITEMS}}}}"] = _body_without_key_message(body)
        mapping[f"{{{{P{index}_PLAN}}}}"] = _body_without_key_message(body)
    return mapping


def _collect_slide_map(slide, page_no: int, map_config: dict) -> dict:
    shapes = list(_iter_shapes_recursive(slide.shapes))
    text_shapes = [shape for shape in shapes if getattr(shape, "has_text_frame", False)]
    tables = [shape for shape in shapes if getattr(shape, "has_table", False)]
    charts = [shape for shape in shapes if getattr(shape, "has_chart", False)]
    pictures = [shape for shape in shapes if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE]
    placeholders = [shape for shape in text_shapes if any(term in _frame_text(shape) for term in _PLACEHOLDER_TERMS)]
    title_shape = _find_title_shape(slide, page_no, map_config)
    return {
        "page": page_no,
        "shape_count": len(shapes),
        "text_shape_count": len(text_shapes),
        "table_count": len(tables),
        "native_chart_count": len(charts),
        "picture_count": len(pictures),
        "placeholder_count": len(placeholders),
        "suggested_title_shape": getattr(title_shape, "name", None) if title_shape else None,
        "shapes": [
            {
                "id": getattr(shape, "shape_id", None),
                "name": getattr(shape, "name", ""),
                "type": str(getattr(shape, "shape_type", "")),
                "bbox_inches": [round(v, 2) for v in _shape_box(shape)],
                "text": _frame_text(shape)[:240],
                "has_table": bool(getattr(shape, "has_table", False)),
                "has_chart": bool(getattr(shape, "has_chart", False)),
                "is_picture": getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE,
            }
            for shape in shapes
        ],
    }


def inspect_template(path: str | Path) -> dict:
    """分析模板页面、形状、原生图表、表格、图片和可配置映射点。"""
    prs = Presentation(str(path))
    map_config = _load_shape_map()
    layouts = []
    for index, layout in enumerate(prs.slide_layouts):
        layouts.append({
            "index": index,
            "name": layout.name,
            "placeholders": [str(p.placeholder_format.type) for p in layout.placeholders],
        })
    slide_maps = [_collect_slide_map(slide, index, map_config) for index, slide in enumerate(prs.slides, start=1)]
    return {
        "layouts": layouts,
        "slide_count": len(prs.slides),
        "native_chart_count": sum(item["native_chart_count"] for item in slide_maps),
        "table_count": sum(item["table_count"] for item in slide_maps),
        "picture_count": sum(item["picture_count"] for item in slide_maps),
        "placeholder_count": sum(item["placeholder_count"] for item in slide_maps),
        "has_markers": _has_markers(prs),
        "slides": slide_maps,
        "mapping_mode": "explicit_markers" if _has_markers(prs) else "heuristic_with_shape_name_overrides",
    }


def fill_real_template_with_diagnostics(template_path: str | Path, report_text: str, out_path: str | Path) -> dict:
    """将周报内容映射到历史 PPT 底版，返回输出路径与可审计的更新/告警报告。"""
    pages = parse_report(report_text)
    prs = Presentation(str(template_path))
    map_config = _load_shape_map()
    diagnostics = {
        "path": str(out_path),
        "mode": "explicit_markers" if _has_markers(prs) else "heuristic_with_shape_name_overrides",
        "updated_text_frames": 0,
        "updated_tables": 0,
        "updated_charts": 0,
        "created_charts": 0,
        "preserved_pictures": 0,
        "warnings": [],
        "pages": {},
    }
    diagnostics["preserved_pictures"] = sum(
        1 for slide in prs.slides for shape in _iter_shapes_recursive(slide.shapes)
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
    )

    if _has_markers(prs):
        # 混合模式：带完整页占位符的页面以显式标记为准；其他页面继续自动映射，避免单页标记导致整份模板不更新。
        full_marker_pages = {
            page_no for page_no, slide in enumerate(prs.slides, start=1)
            if page_no <= len(pages) and _has_full_page_marker(slide, page_no)
        }
        replaced, frames_changed = _apply_markers(prs, build_marker_map(pages))
        diagnostics["updated_text_frames"] += frames_changed
        diagnostics["marker_replacements"] = replaced
        diagnostics["pages"][0] = [f"显式占位符替换 {replaced} 处（涉及 {frames_changed} 个文本框）"]
        if len(prs.slides) != len(pages):
            diagnostics["warnings"].append(f"模板含显式占位符且共 {len(prs.slides)} 页，本期策划 {len(pages)} 页；原页替换模式只处理共有页面。若需增减页数，请使用‘历史周报参考创作’。")
        for page_no, slide in enumerate(prs.slides, start=1):
            if page_no > len(pages):
                break
            body = pages[page_no - 1]["body"]
            if page_no not in full_marker_pages:
                _fill_historical_slide(slide, pages[page_no - 1], page_no, map_config, diagnostics)
            _apply_chart_data(slide, body, page_no, diagnostics, map_config)
    else:
        if len(prs.slides) != len(pages):
            diagnostics["warnings"].append(f"历史底版当前为 {len(prs.slides)} 页，周报内容为 {len(pages)} 页；仅处理两者共有页面。若需增减页数，请使用‘历史周报参考创作’。")
        for page_no, slide in enumerate(prs.slides, start=1):
            if page_no > len(pages):
                break
            _fill_historical_slide(slide, pages[page_no - 1], page_no, map_config, diagnostics)
            _apply_chart_data(slide, pages[page_no - 1]["body"], page_no, diagnostics, map_config)

    # 最终检查：未替换 marker、通用占位文字、明显遗留的示例内容。
    residual_markers = []
    residual_placeholders = []
    for page_no, slide in enumerate(prs.slides, start=1):
        for shape in _iter_shapes_recursive(slide.shapes):
            text = _frame_text(shape)
            if re.search(r"\{\{[^{}]+\}\}", text):
                residual_markers.append(f"P{page_no}:{shape.name}")
            if text and any(term in text for term in ("【待补充", "每周更新", "具体事项", "数据图表（")):
                # 图片、照片占位符可能是有意保留，不把它们当成未更新的数据文本。
                if not _is_picture_placeholder(text) and not _is_chart_placeholder(text):
                    residual_placeholders.append(f"P{page_no}:{shape.name}")
    if residual_markers:
        diagnostics["warnings"].append("仍有未替换的模板占位符：" + ", ".join(residual_markers[:8]))
    if residual_placeholders:
        diagnostics["warnings"].append("仍有待补充文本：" + ", ".join(residual_placeholders[:8]))
    remaining_chart_placeholders = sum(len(_find_chart_placeholders(slide)) for slide in prs.slides)
    if remaining_chart_placeholders:
        diagnostics["warnings"].append(f"仍有 {remaining_chart_placeholders} 个图表占位区未转为动态图表，可能是缺少对应数据或同页仅提供一组数据。")
    if diagnostics["preserved_pictures"]:
        diagnostics["warnings"].append(
            f"已保留 {diagnostics['preserved_pictures']} 张原模板图片；图片内部的文字/图表不会自动改写，请核对是否含上期数据。"
        )
    if diagnostics["updated_charts"] == 0 and diagnostics["created_charts"] == 0:
        chart_placeholder_count = sum(len(_find_chart_placeholders(slide)) for slide in prs.slides)
        if chart_placeholder_count:
            diagnostics["warnings"].append(f"检测到 {chart_placeholder_count} 个图表占位区域，但未成功生成动态图表；请确认正文是否提供“图表数据”结构块。")

    from core.ppt_pipeline import finalize
    qa = finalize(prs, pages, out_path)
    diagnostics["warnings"].extend(qa["warnings"])
    diagnostics["preview_status"] = qa["preview_status"]
    return diagnostics


def fill_real_template(template_path: str | Path, report_text: str, out_path: str | Path) -> str:
    """兼容旧接口：填充历史周报模板并返回输出文件路径。"""
    return fill_real_template_with_diagnostics(template_path, report_text, out_path)["path"]


def _placeholder_types(layout) -> set:
    types = set()
    for placeholder in layout.placeholders:
        try:
            types.add(placeholder.placeholder_format.type)
        except Exception:
            continue
    return types


def _pick_cover_layout(prs):
    """封面版式：优先「居中标题」，其次「标题 + 副标题」。"""
    for layout in prs.slide_layouts:
        if PP_PLACEHOLDER.CENTER_TITLE in _placeholder_types(layout):
            return layout
    for layout in prs.slide_layouts:
        types = _placeholder_types(layout)
        if PP_PLACEHOLDER.TITLE in types and PP_PLACEHOLDER.SUBTITLE in types:
            return layout
    return prs.slide_layouts[0]


def _pick_content_layout(prs):
    """内容版式：优先「标题 + 正文/内容」占位符。"""
    for layout in prs.slide_layouts:
        types = _placeholder_types(layout)
        if PP_PLACEHOLDER.TITLE in types and (PP_PLACEHOLDER.BODY in types or PP_PLACEHOLDER.OBJECT in types):
            return layout
    for layout in prs.slide_layouts:
        if PP_PLACEHOLDER.TITLE in _placeholder_types(layout):
            return layout
    return prs.slide_layouts[min(1, len(prs.slide_layouts) - 1)]


def _clear_slides(prs) -> None:
    """清空示例页，只保留母版与版式。"""
    slide_ids = prs.slides._sldIdLst
    for slide_id in list(slide_ids):
        rel_id = slide_id.get(qn("r:id"))
        if rel_id:
            prs.part.drop_rel(rel_id)
        slide_ids.remove(slide_id)


def _fill_slide(slide, title: str, body: str) -> None:
    title_shape = slide.shapes.title
    title_idx = None
    if title_shape is not None:
        title_shape.text = title
        title_idx = title_shape.placeholder_format.idx
    else:
        box = slide.shapes.add_textbox(Inches(0.7), Inches(0.45), Inches(11.9), Inches(0.95))
        frame = box.text_frame
        frame.word_wrap = True
        frame.text = title
        for paragraph in frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(26)
                run.font.bold = True

    body_shape = None
    for shape in slide.placeholders:
        if title_idx is not None and shape.placeholder_format.idx == title_idx:
            continue
        if shape.has_text_frame:
            body_shape = shape
            break

    frame = None
    if body_shape is not None:
        frame = body_shape.text_frame
        frame.word_wrap = True
        frame.text = body
    elif body:
        box = slide.shapes.add_textbox(Inches(0.7), Inches(1.65), Inches(11.9), Inches(5.1))
        frame = box.text_frame
        frame.word_wrap = True
        frame.text = body

    if frame is not None:
        for paragraph in frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(12)


def build_ppt_from_template(template_path: str | Path, report_text: str, out_path: str | Path) -> str:
    """空白模板模式：保留母版/主题/版式，按策划的实际页数新建内容页。"""
    pages = parse_report(report_text)
    prs = Presentation(str(template_path))
    if _has_markers(prs):
        # 显式占位符模板仍可原页替换；如需增减页数，交由参考创作模式重建。
        return fill_real_template(template_path, report_text, out_path)

    cover_layout = _pick_cover_layout(prs)
    content_layout = _pick_content_layout(prs)
    _clear_slides(prs)
    try:
        from core.ppt_export import _infer_page_type
    except Exception:
        _infer_page_type = None
    total = len(pages)
    for index, page in enumerate(pages):
        page_no = index + 1
        page_type = page.get("type") or (_infer_page_type(page, index, total) if _infer_page_type else "generic")
        layout = cover_layout if page_type in {"cover", "closing"} else content_layout
        slide = prs.slides.add_slide(layout)
        page_title, page_body = split_title_body(page)
        if page_type == "closing":
            title, body = "Thankyou", ""
        elif page_type == "cover":
            title, body = page_title or "H区安保周报", page_body
        else:
            title, body = page_title or f"P{page_no}", page_body or "【待补充】"
        body = _strip_structured_blocks(body)
        _fill_slide(slide, title, body)
        parsed = _parse_chart_data(page_body)
        if parsed:
            from core.ppt_pipeline import render_chart
            # Reserve right half for charts instead of covering the body.
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape != slide.shapes.title and shape.top > Inches(1):
                    shape.width = int(prs.slide_width * 0.45)
            render_chart(slide, parsed, (int(prs.slide_width*.52), int(prs.slide_height*.23), int(prs.slide_width*.44), int(prs.slide_height*.50)))
    from core.ppt_pipeline import finalize
    finalize(prs, pages, out_path)
    return str(out_path)

