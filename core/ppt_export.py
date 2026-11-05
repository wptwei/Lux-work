"""
把 AI 生成的 H区周报文字稿，按历史周报风格渲染为可编辑 PPTX。
页数动态决定；规范要点：16:9、深蓝#002060、微软雅黑、页脚「华东安保二部」+ 实际页码。
"""
from __future__ import annotations

from core.report_data import _strip_markdown, parse_report, split_title_body, visible_body

import re
from datetime import date
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE

NAVY = RGBColor(0x00, 0x20, 0x60)
GRAY = RGBColor(0x8C, 0x8C, 0x8C)
GRAY2 = RGBColor(0x7D, 0x7D, 0x7D)
DARK = RGBColor(0x2F, 0x2F, 0x2F)
BLUE = RGBColor(0x1F, 0x3B, 0x73)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
FONT = "微软雅黑"
TOTAL = 8  # 旧版兼容默认值；动态渲染始终显式传入实际页数


def _set_font(run, size, color, bold=False):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.font.bold = bold
    # 中文字体
    rPr = run._r.get_or_add_rPr()
    from pptx.oxml.ns import qn
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        rPr.append(ea)
    ea.set("typeface", FONT)


def _rect(slide, x, y, w, h, fill, line_color=None):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    if line_color is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = line_color
        shape.line.width = Pt(0.5)
    shape.shadow.inherit = False
    return shape


def _rounded(slide, x, y, w, h, fill):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _text(slide, x, y, w, h, text, size=14, color=DARK, bold=False,
          align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    r = p.add_run()
    r.text = text
    _set_font(r, size, color, bold)
    return tb


def _add_body(slide, x, y, w, h, text, size=14, color=DARK):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()] or ["【待补充】"]
    for j, line in enumerate(lines):
        p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
        r = p.add_run()
        r.text = line
        _set_font(r, size, color)
    return tb


def _header(slide, title, subtitle):
    _rect(slide, 0.55, 0.40, 0.14, 0.44, BLUE)
    parts = [p.strip() for p in (title or "").split("|", 1)]
    first = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    tb = slide.shapes.add_textbox(Inches(0.84), Inches(0.37), Inches(11.0), Inches(0.5))
    tf = tb.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    r1 = p.add_run()
    r1.text = first
    _set_font(r1, 24, BLUE, True)
    if rest:
        r2 = p.add_run()
        r2.text = "| " + rest
        _set_font(r2, 24, NAVY, True)
    _text(slide, 0.84, 0.88, 10.0, 0.25, subtitle or "", 11, GRAY)
    _rect(slide, 0.55, 1.22, 12.23, 0.012, GRAY)


def _footer(slide, page_no, total=TOTAL):
    _rect(slide, 0.55, 7.05, 12.23, 0.012, GRAY)
    _text(slide, 0.55, 7.12, 5.0, 0.25, "华东安保二部", 9, GRAY)
    _text(slide, 11.2, 7.12, 1.6, 0.25, f"{page_no:02d} / {total:02d}", 9, GRAY, align=PP_ALIGN.RIGHT)


def _placeholder(slide, x, y, w, h, label="图片（每周更新）"):
    box = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    box.fill.solid()
    box.fill.fore_color.rgb = RGBColor(0xF2, 0xF2, 0xF2)
    box.line.color.rgb = GRAY
    box.line.width = Pt(0.5)
    box.shadow.inherit = False
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run()
    r.text = label
    _set_font(r, 11, GRAY)
    return box


def _table(slide, x, y, w, h, data):
    rows = max(len(data), 1)
    cols = max((len(row) for row in data), default=1)
    graphic = slide.shapes.add_table(rows, cols, Inches(x), Inches(y), Inches(w), Inches(h))
    table = graphic.table
    for ri, row in enumerate(data):
        for ci, cell in enumerate(row):
            table.cell(ri, ci).text = str(cell)
    return table


def _structured_block_lines(body: str, block_label: str) -> list[str]:
    lines = (body or "").splitlines()
    start = None
    marker = re.compile(rf"(?i)^\s*{re.escape(block_label)}\s*[：:]?\s*$")
    for index, line in enumerate(lines):
        if marker.match(line.strip()):
            start = index + 1
            break
    if start is None:
        return []
    output = []
    for line in lines[start:]:
        value = line.strip()
        if not value:
            if output:
                break
            continue
        if re.match(r"(?i)^\s*(?:key\s*message|图表数据|表格数据)\s*[：:]", value):
            break
        output.append(value)
    return output


_MASK_MARK = re.compile(r"(?<=\d)[xX](?![A-Za-z0-9])")


def _unmask_numbers(text: str) -> str:
    """去掉素材里的脱敏标记（楼栋3x → 楼栋3、166x → 166）。

    图表本来就用真实数值绘图，成稿正文继续带 x 看起来像笔误。核验仍针对原始
    素材做，这里只影响 PPT 上的显示文本。
    """
    return _MASK_MARK.sub("", text or "")


def _clean_bullets(text: str) -> str:
    """把素材里的“- 条目”整理成成稿行首不带符号的短句。"""
    return "\n".join(re.sub(r"^\s*[-—–·•*]\s+", "", line).strip() for line in (text or "").splitlines())


def _num_text(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _restates_chart(line: str, categories: list, series_values: list) -> bool:
    """判断一行是否只是在复述图表里的周期与数值（如“违禁品携带 9wk1 21 …”）。"""
    numbers = {float(token) for token in re.findall(r"\d+(?:\.\d+)?", line)}
    if not numbers:
        return False
    chart_numbers = {float(token) for name in categories for token in re.findall(r"\d+(?:\.\d+)?", str(name))}
    chart_numbers |= {float(value) for values in series_values for value in values}
    return numbers <= chart_numbers


def _visible_body(body: str) -> str:
    """兼容入口：新建 PPT 的可见正文。"""
    return visible_body(body)


def _split_key_message(body: str) -> tuple[str, str]:
    """提取 Key Message，并排除只用于生成图表/表格的数据块。"""
    facts, summaries = [], []
    is_summary = False
    for ln in _visible_body(body).splitlines():
        match = re.match(r"(?i)^\s*key\s*message\s*[：:]\s*(.*)$", ln)
        if match:
            is_summary = True
            if match.group(1).strip():
                summaries.append(match.group(1).strip())
            continue
        if is_summary:
            summaries.append(ln.strip())
        elif ln.strip():
            facts.append(ln.strip())
    return "\n".join(facts).strip(), "\n".join(summaries).strip() or "【待补充：数据分析】"


def _body_kv(body):
    """优先解析显式表格数据块，其次解析“字段：值”行。"""
    rows = _structured_block_lines(body, "表格数据")
    data = []
    if rows:
        for line in rows:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if "|" in line else [cell.strip() for cell in line.split("\t")]
            if cells:
                data.append(cells)
        return data or [["项目", "数量"]]
    for ln in _visible_body(body).splitlines():
        line = ln.strip()
        if not line or re.match(r"(?i)^\s*key\s*message\s*[：:]", line):
            continue
        if "：" in line or ":" in line:
            key, _, val = line.replace("：", ":").partition(":")
            data.append([key.strip(), val.strip()])
        else:
            data.append(["", line])
    return data or [["项目", "数量"]]


def _replace_placeholder_with_chart(slide, placeholder, body: str) -> bool:
    """当正文含显式、可校验的图表数据块时，在占位区域创建可编辑柱状图。"""
    try:
        from core.report_data import _parse_chart_data
        parsed = _parse_chart_data(body)
        if not parsed:
            return False
        from core.ppt_pipeline import render_chart
        render_chart(slide, parsed, tuple(getattr(placeholder, k) for k in ("left", "top", "width", "height")), placeholder)
        return True
    except Exception:
        return False


def _infer_page_type(page: dict, index: int, total: int) -> str:
    """从模型显式类型或标题/正文推断页面用途。"""
    known = {
        "cover", "contents", "manpower", "operations", "anomalies", "key_work",
        "photo_evidence", "timeline", "control_core", "next_plan", "summary", "generic", "closing",
    }
    explicit = str(page.get("type") or "").strip().lower()
    if explicit in known:
        return explicit
    title = str(page.get("title") or "").lower()
    body = str(page.get("body") or "").lower()
    text = title + " " + body[:180]
    if index == 0 or any(k in title for k in ("封面", "周报标题")):
        return "cover"
    if any(k in text for k in ("thankyou", "结束页", "感谢观看", "谢谢观看")) and index == total - 1:
        return "closing"
    if any(k in title for k in ("目录", "contents", "contents")):
        return "contents"
    if any(k in title for k in ("人力", "厂区分布", "岗位配置", "人员分布")):
        return "manpower"
    if any(k in title for k in ("异常", "事件分析", "异常汇总", "风险分析")):
        return "anomalies"
    if any(k in title for k in ("运营看板", "运营数据", "违禁品", "指标看板", "数据看板")):
        return "operations"
    if any(k in title for k in ("本周重点工作", "重点工作", "工作成果", "本周完成", "关键工作")):
        return "key_work"
    if any(k in title for k in ("现场执勤影像", "现场影像", "现场照片", "影像展示", "照片展示")):
        return "photo_evidence"
    if any(k in title for k in ("关键节点", "里程碑", "节点纪实", "进度时间线")):
        return "timeline"
    if any(k in title for k in ("管控核心", "管控措施", "管控执行", "制度执行", "执行要点")):
        return "control_core"
    if any(k in title for k in ("下周", "计划", "后续工作", "下一步")):
        return "next_plan"
    if any(k in title for k in ("摘要", "总览", "本期概览", "执行摘要")):
        return "summary"
    return "generic"


def _body_lines(body: str) -> list[str]:
    visible = _visible_body(body)
    return [line.strip(" •\t") for line in visible.splitlines() if line.strip() and not re.match(r"(?i)^key\s*message\s*[：:]", line.strip())]


def _add_key_message(slide, text: str, x=0.75, y=5.95, w=11.8, h=0.72, label="Key Message"):
    if not text:
        return
    _rounded(slide, x, y, w, h, RGBColor(0xF5, 0xF7, 0xFA))
    _text(slide, x + 0.18, y + 0.12, w - 0.36, h - 0.18, f"{label}：{text}", 11, DARK)


def _add_generic_content(slide, title: str, body: str, page_no: int, total: int, subtitle="Weekly security update"):
    _rect(slide, 0, 0, 13.333, 7.5, WHITE)
    _header(slide, title or "本周工作进展", subtitle)
    facts, key_message = _split_key_message(body)
    chart_placeholder = _placeholder(slide, 0.75, 3.40, 11.8, 2.95, "提供‘图表数据：’后自动生成可编辑图表")
    if _replace_placeholder_with_chart(slide, chart_placeholder, body):
        # 有图表的页面用上下两段：事实在上、图表占满下段，文字短时也不会整页留白。
        _add_body(slide, 0.75, 1.62, 11.8, 1.7, _clean_bullets(facts) or "【待补充：本页内容】", 14, DARK)
        if key_message and key_message != "【待补充：数据分析】":
            _add_key_message(slide, key_message, y=6.45, h=0.42, label="Key Message")
    else:
        from core.ppt_template import _remove_shape
        _remove_shape(chart_placeholder)
        _add_body(slide, 0.75, 1.65, 6.0, 4.1, facts or "【待补充：本页内容】", 14, DARK)
        _placeholder(slide, 7.35, 1.65, 5.2, 3.9, "本页未提供可验证的图表数据")
    _footer(slide, page_no, total)


def _draw_page(prs, page: dict, index: int, total: int, date_str: str, all_pages: list[dict] | None = None, cover_image_blob: bytes | None = None):
    n = index + 1
    title, body = split_title_body(page)
    body = _unmask_numbers(body)
    page_type = _infer_page_type(page, index, total)
    blank = next((layout for layout in prs.slide_layouts if str(getattr(layout, "name", "")).strip().lower() in {"blank", "空白"}), None)
    if blank is None:
        blank = next((layout for layout in prs.slide_layouts if len(getattr(layout, "placeholders", [])) == 0), prs.slide_layouts[-1])
    slide = prs.slides.add_slide(blank)
    lines = _body_lines(body)
    facts, key_message = _split_key_message(body)
    chart_data = None
    try:
        from core.report_data import _parse_chart_data
        chart_data = _parse_chart_data(body)
    except Exception:
        chart_data = None

    if chart_data and page_type not in {"operations", "anomalies", "generic", "cover", "closing"}:
        _add_generic_content(slide, title, body, n, total)
        return slide

    if page_type == "cover":
        cover_title = title or next((ln for ln in lines if "周报" in ln), "H区安保周报")
        subtitle = next((ln for ln in lines if any(k in ln for k in ("统计周期", "周期：", "日期："))), "")
        if not subtitle and body and body.strip() != cover_title:
            subtitle = body.strip().splitlines()[0]
        if cover_image_blob:
            # 只复用历史报告封面中的固定厂区背景/品牌图；不复用旧数据页和现场证据照片。
            from io import BytesIO
            slide.shapes.add_picture(BytesIO(cover_image_blob), 0, 0, width=prs.slide_width, height=prs.slide_height)
            _text(slide, 0.42, 0.15, 6.2, 0.78, cover_title, 26, NAVY, True)
            date_text = f"By : Security team\nDate： {date_str} Rev. 01"
            _text(slide, 5.15, 5.75, 2.6, 0.72, date_text, 9, GRAY, False)
            if subtitle:
                _text(slide, 0.45, 1.0, 6.5, 0.35, subtitle, 11, NAVY, True)
        else:
            _rect(slide, 0, 0, 13.333, 7.5, NAVY)
            _rect(slide, 0, 2.72, 13.333, 1.52, BLUE)
            _rect(slide, 0, 2.72, 0.16, 1.52, RGBColor(0x00, 0xB0, 0xF0))
            _text(slide, 0.72, 3.03, 11.9, 0.85, cover_title, 34, WHITE, True)
            _text(slide, 0.75, 4.65, 11.8, 0.48, subtitle[:100], 16, RGBColor(0xD9, 0xE7, 0xF5))
            _text(slide, 0.75, 6.55, 6.0, 0.3, "By : Security team", 11, WHITE)
            _text(slide, 8.2, 6.55, 4.35, 0.3, f"Date： {date_str}  Rev. 01", 11, WHITE, align=PP_ALIGN.RIGHT)
        return slide

    if page_type == "closing":
        _rect(slide, 0, 0, 13.333, 7.5, BLUE)
        _rect(slide, 0, 0, 0.22, 7.5, RGBColor(0x00, 0xB0, 0xF0))
        _text(slide, 0.5, 2.8, 12.3, 1.2, "Thankyou", 42, WHITE, True, PP_ALIGN.CENTER)
        _text(slide, 0.5, 4.2, 12.3, 0.5, "华东安保二部 · H区", 14, WHITE, False, PP_ALIGN.CENTER)
        return slide

    if page_type == "contents":
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        _header(slide, title or "目录", "CONTENTS")
        entries = [ln for ln in lines if ln.lower() not in {"contents", "internal use - confidential"}]
        if not entries:
            entries = [p.get("title", "") for j, p in enumerate(all_pages or []) if j not in (0, index) and _infer_page_type(p, j, total) != "closing"]
        y = 1.65
        for i, entry in enumerate(entries[:12], 1):
            _rounded(slide, 0.8, y, 11.6, 0.43, RGBColor(0xF5, 0xF7, 0xFA))
            _text(slide, 1.0, y + 0.08, 0.55, 0.25, f"{i:02d}", 11, RGBColor(0x00, 0xB0, 0xF0), True)
            _text(slide, 1.75, y + 0.07, 10.2, 0.28, entry, 14, NAVY, i <= 4)
            y += 0.48
        _text(slide, 0.6, 6.72, 5.0, 0.22, "Internal use - confidential", 9, GRAY2)
        _footer(slide, n, total)
        return slide

    if page_type == "manpower":
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        _header(slide, title or "厂区分布&人力分布", "FACTORY DISTRIBUTION & MANPOWER")
        data = _body_kv(body)
        if len(data) > 9:
            data = data[:9]
        _rounded(slide, 0.65, 1.55, 5.8, 0.46, BLUE)
        _text(slide, 0.85, 1.64, 5.2, 0.24, "岗位 & 人力", 14, WHITE, True)
        _table(slide, 0.65, 2.08, 5.8, 2.75, data)
        _rounded(slide, 6.85, 1.55, 5.8, 0.46, BLUE)
        _text(slide, 7.05, 1.64, 5.2, 0.24, "本页摘要", 14, WHITE, True)
        summary_lines = lines[:7] or ["未提供结构化人力信息"]
        _add_body(slide, 7.05, 2.2, 5.3, 2.5, "\n".join(summary_lines), 13, DARK)
        _placeholder(slide, 0.65, 5.1, 5.8, 1.35, "厂区分布图（请补充最新图片）")
        _placeholder(slide, 6.85, 5.1, 5.8, 1.35, "岗位现场照片（请补充本期图片）")
        if key_message and key_message != "【待补充：数据分析】":
            _add_key_message(slide, key_message, y=6.5, h=0.42)
        _footer(slide, n, total)
        return slide

    if page_type in {"operations", "anomalies"}:
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        default_title = "安保服务项目 | 专区运营看板" if page_type == "operations" else "安保服务项目 | 异常汇总分析"
        subtitle = "SECURITY SERVICE PROJECT · OPERATION DASHBOARD" if page_type == "operations" else "SECURITY SERVICE PROJECT · ABNORMALITY ANALYSIS"
        _header(slide, title or default_title, subtitle)
        left_x, left_y, left_w, left_h = 0.75, 1.62, 7.1, 4.15
        placeholder = _placeholder(slide, left_x, left_y, left_w, left_h, "提供‘图表数据：’后自动生成可编辑图表")
        chart_made = _replace_placeholder_with_chart(slide, placeholder, body)
        if not chart_made:
            # Do not create synthetic charts or reuse old data when no structured data exists.
            from core.ppt_template import _remove_shape
            _remove_shape(placeholder)
            _add_body(slide, left_x + 0.2, left_y + 0.25, left_w - 0.4, left_h - 0.45, facts or "未提供可绘图的结构化数据。", 13, DARK)
        _rounded(slide, 8.2, 1.62, 4.35, 4.15, RGBColor(0xF5, 0xF7, 0xFA))
        _text(slide, 8.5, 1.88, 3.75, 0.36, "核心发现" if page_type == "operations" else "异常摘要", 16, NAVY, True)
        if chart_data:
            categories = chart_data.get("categories", [])
            series_names = chart_data.get("series_names", [])
            series_values = chart_data.get("series", [])
            if len(series_names) == 1:
                # 单系列时逐条复述每根柱子没有信息量，右栏改放本页其他事实；
                # 没有其他事实才退回收首末周期读数（数值仍原样取自图表数据）。
                others = [
                    line for line in lines
                    if not line.endswith(("：", ":"))
                    and not _restates_chart(line, categories, series_values)
                ]
                if others:
                    metric_text = "\n".join(others[:6])
                elif series_values and series_values[0]:
                    metric_text = (
                        f"{series_names[0]}：{categories[0]} {_num_text(series_values[0][0])}"
                        f" → {categories[-1]} {_num_text(series_values[0][-1])}"
                    )
                else:
                    metric_text = facts or "【待补充：请提供本期指标及可比数据】"
            else:
                metric_lines = []
                for row_index, category in enumerate(categories[:6]):
                    values = [f"{series_names[j]} {series_values[j][row_index]}" for j in range(min(len(series_names), len(series_values)))]
                    metric_lines.append(f"{category}：" + " / ".join(values))
                metric_text = "\n".join(metric_lines)
        else:
            metric_text = facts or "【待补充：请提供本期指标及可比数据】"
        _add_body(slide, 8.5, 2.45, 3.75, 2.7, _clean_bullets(metric_text), 13, DARK)
        if key_message and key_message != "【待补充：数据分析】":
            _add_key_message(slide, key_message, x=0.75, y=5.98, w=11.8, h=0.72,
                             label="Key message" if page_type == "operations" else "Key Message")
        _footer(slide, n, total)
        return slide

    if page_type == "key_work":
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        _header(slide, title or "本周重点工作", "THIS WEEK'S KEY TASKS")
        items = lines or ["【待补充：本周重点工作】"]
        # A two-column card grid supports more than the historical three-item layout.
        cols = 2 if len(items) > 4 else 1
        rows = (len(items) + cols - 1) // cols
        card_h = min(1.18, 4.9 / max(rows, 1))
        for idx, item in enumerate(items[:10]):
            col = idx // rows if cols == 2 else 0
            row = idx % rows if cols == 2 else idx
            x = 0.65 + col * 6.15
            y = 1.52 + row * card_h
            w = 5.75 if cols == 2 else 12.0
            _rounded(slide, x, y, w, max(0.62, card_h - 0.08), RGBColor(0xF5, 0xF7, 0xFA))
            _rect(slide, x, y, 0.08, max(0.62, card_h - 0.08), BLUE)
            _text(slide, x + 0.22, y + 0.12, 0.55, 0.28, f"{idx + 1:02d}", 11, RGBColor(0x00, 0xB0, 0xF0), True)
            _add_body(slide, x + 0.82, y + 0.12, w - 1.0, max(0.38, card_h - 0.28), item, 12 if len(item) > 65 else 13, DARK)
        if len(items) > 10:
            _text(slide, 0.75, 6.55, 11.7, 0.3, f"另有 {len(items)-10} 项内容，建议拆分为后续工作成果页。", 10, GRAY)
        _footer(slide, n, total)
        return slide

    if page_type == "photo_evidence":
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        _header(slide, title or "现场执勤影像", "ON-SITE EVIDENCE")
        captions = lines[:6] or ["请上传本期现场照片并补充说明"]
        cols = 3 if len(captions) > 2 else len(captions)
        cols = max(cols, 1)
        rows = (len(captions) + cols - 1) // cols
        cell_w = 11.9 / cols
        cell_h = 4.9 / rows
        for idx, caption in enumerate(captions):
            col, row = idx % cols, idx // cols
            x, y = 0.7 + col * cell_w, 1.55 + row * cell_h
            _placeholder(slide, x, y, cell_w - 0.25, cell_h - 0.62, "本期现场照片待补充")
            _text(slide, x, y + cell_h - 0.52, cell_w - 0.25, 0.38, caption[:120], 10, DARK)
        _footer(slide, n, total)
        return slide

    if page_type == "timeline":
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        _header(slide, title or "关键节点纪实", "KEY MILESTONES")
        items = lines[:8] or ["【待补充：关键节点】"]
        _rect(slide, 0.95, 3.45, 11.45, 0.035, BLUE)
        positions = [1.0 + i * (10.8 / max(len(items) - 1, 1)) for i in range(len(items))]
        for idx, (x, item) in enumerate(zip(positions, items)):
            _rounded(slide, x - 0.1, 3.28, 0.25, 0.35, RGBColor(0x00, 0xB0, 0xF0))
            y = 1.65 if idx % 2 == 0 else 4.0
            _rounded(slide, max(0.65, min(x - 1.0, 9.9)), y, 2.2, 1.2, RGBColor(0xF5, 0xF7, 0xFA))
            _text(slide, max(0.82, min(x - 0.82, 10.08)), y + 0.12, 1.86, 0.95, item[:100], 10, DARK, idx % 2 == 0)
        _footer(slide, n, total)
        return slide

    if page_type == "control_core":
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        _header(slide, title or "管控核心与执行", "CONTROL FRAMEWORK & EXECUTION")
        items = lines[:9] or ["【待补充：管控要求】"]
        cols = 3
        rows = (len(items) + cols - 1) // cols
        card_w = 3.85
        card_h = min(1.75, 4.9 / max(rows, 1))
        for idx, item in enumerate(items):
            row, col = divmod(idx, cols)
            x, y = 0.7 + col * 4.12, 1.55 + row * (card_h + 0.2)
            _rounded(slide, x, y, card_w, card_h, RGBColor(0xF8, 0xF9, 0xFB))
            _rect(slide, x, y, card_w, 0.08, BLUE)
            _text(slide, x + 0.18, y + 0.18, card_w - 0.36, card_h - 0.32, item[:240], 11, DARK, idx < 3)
        _footer(slide, n, total)
        return slide

    if page_type == "next_plan":
        _rect(slide, 0, 0, 13.333, 7.5, WHITE)
        _header(slide, title or "下周重点工作计划", "NEXT WEEK'S KEY WORK PLAN")
        items = lines or ["【待补充：下周计划】"]
        groups, current = [], []
        for line in items:
            if re.match(r"^\s*0?\d+[、.．:：]", line) and current:
                groups.append(current); current = [line]
            else:
                current.append(line)
        if current:
            groups.append(current)
        if len(groups) == 1 and len(items) > 4:
            groups = [items[i:i+3] for i in range(0, len(items), 3)]
        cols = min(3, max(1, len(groups)))
        rows = (len(groups) + cols - 1) // cols
        card_w = 11.9 / cols
        card_h = 4.8 / rows
        for idx, group in enumerate(groups):
            row, col = divmod(idx, cols)
            x, y = 0.7 + col * card_w, 1.62 + row * card_h
            _rounded(slide, x, y, card_w - 0.18, card_h - 0.15, RGBColor(0xF5, 0xF7, 0xFA))
            heading = group[0][:40]
            _text(slide, x + 0.18, y + 0.16, card_w - 0.5, 0.42, heading, 13, NAVY, True)
            rest = group[1:] if len(group) > 1 else group
            _add_body(slide, x + 0.18, y + 0.68, card_w - 0.5, card_h - 0.9, "\n".join(rest), 11, DARK)
        _footer(slide, n, total)
        return slide

    # Generic and summary pages remain fully editable and use the same visual system.
    _add_generic_content(slide, title or ("本期工作摘要" if page_type == "summary" else "工作进展"), body, n, total,
                         "WEEKLY EXECUTIVE SUMMARY" if page_type == "summary" else "WEEKLY SECURITY UPDATE")
    return slide


def _clear_slides(prs):
    slide_ids = prs.slides._sldIdLst
    for slide_id in list(slide_ids):
        rel_id = slide_id.rId
        prs.part.drop_rel(rel_id)
        slide_ids.remove(slide_id)


def build_ppt(report_text: str, out_path: str | Path, reference_template: str | Path | None = None) -> str:
    """按实际页数创作可编辑 PPT；可选用历史周报作为主题/母版参考，不复用旧数据页。"""
    pages = parse_report(report_text)
    if not pages:
        raise ValueError("没有识别到页面标记，请至少提供 ## P1 标题 格式的页面结构。")
    if len(pages) > 20:
        raise ValueError("周报页数超过 20 页，请检查页面规划是否异常。")
    cover_image_blob = None
    if reference_template:
        prs = Presentation(str(reference_template))
        # 从历史报告第一页提取全页背景图作为封面固定视觉资产，避免复用任何旧内容页。
        try:
            from pptx.enum.shapes import MSO_SHAPE_TYPE
            first_slide = prs.slides[0] if prs.slides else None
            if first_slide:
                candidates = [shape for shape in first_slide.shapes
                              if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
                              and shape.width >= prs.slide_width * 0.85
                              and shape.height >= prs.slide_height * 0.75]
                if candidates:
                    cover_image_blob = max(candidates, key=lambda shape: shape.width * shape.height).image.blob
        except Exception:
            cover_image_blob = None
        _clear_slides(prs)
    else:
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        # 默认创作模式使用从用户提供的历史周报提取的固定封面背景；不含旧日期/业务数据。
        try:
            from pathlib import Path
            default_cover = Path(__file__).resolve().parent.parent / "assets" / "weekly_report_cover_background.png"
            if default_cover.is_file():
                cover_image_blob = default_cover.read_bytes()
        except OSError:
            cover_image_blob = None
    global _CURRENT_RENDER_PAGES
    today = date.today()
    date_str = f"{today.year}/{today.month}/{today.day}"
    total = len(pages)
    for index, page in enumerate(pages):
        _draw_page(prs, page, index, total, date_str, pages, cover_image_blob)
    from core.ppt_pipeline import finalize
    finalize(prs, pages, out_path)
    return str(out_path)


if __name__ == "__main__":
    demo = """# H区周报

## P1 封面
H区9月第3周周报

## P4 专区运营看板
违禁品查获 5×
Key message：本周查获5×，环比+2×
"""
    print(build_ppt(demo, "demo_周报.pptx"))
