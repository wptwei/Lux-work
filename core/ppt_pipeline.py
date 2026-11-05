"""Shared chart rendering, style normalization and export QA."""
from core.report_data import validate_chart, parse_json_chart, strip_json

import json
import re
import subprocess
import os
from pathlib import Path
from pptx.util import Inches, Pt
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.ns import qn

TYPES = {"column": XL_CHART_TYPE.COLUMN_CLUSTERED, "bar": XL_CHART_TYPE.BAR_CLUSTERED,
         "line": XL_CHART_TYPE.LINE, "pie": XL_CHART_TYPE.PIE}
STYLE = {"font": "微软雅黑", "body": 14, "title": 24, "table": 12, "chart": 11}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_STYLE_PATH = _PROJECT_ROOT / "config" / "ppt_style.json"
if _STYLE_PATH.is_file():
    STYLE.update(json.loads(_STYLE_PATH.read_text(encoding="utf-8-sig")))


def render_chart(slide, parsed, box, old=None):
    data = CategoryChartData()
    data.categories = parsed["categories"]
    for name, values in zip(parsed["series_names"], parsed["series"]):
        data.add_series(name, values)
    shape = slide.shapes.add_chart(TYPES[parsed.get("type", "column")], *box, data)
    shape.name = "LUX_DYNAMIC_CHART"
    chart = shape.chart
    chart.has_legend = len(parsed["series_names"]) > 1
    chart.has_title = bool(parsed.get("unit"))
    if chart.has_title:
        chart.chart_title.text_frame.text = "单位：" + parsed["unit"]
    if old is not None:
        el = old._element
        el.getparent().remove(el)
    return shape


def _font(font, size=None):
    font.name = STYLE["font"]
    props = font._element
    ea = props.find(qn("a:ea"))
    if ea is None:
        ea = props.makeelement(qn("a:ea"), {})
        props.append(ea)
    ea.set("typeface", STYLE["font"])
    if size:
        font.size = Pt(size)


def _frame(frame, size=None):
    for paragraph in frame.paragraphs:
        _font(paragraph.font, size)
        for run in paragraph.runs:
            _font(run.font, size)
            props = run._r.get_or_add_rPr()
            ea = props.find(qn("a:ea"))
            if ea is None:
                ea = props.makeelement(qn("a:ea"), {})
                props.append(ea)
            ea.set("typeface", STYLE["font"])


def finalize(prs, pages, out_path):
    from core.ppt_template import _iter_shapes_recursive, _estimate_overflow
    from core.report_data import _parse_chart_data
    report = {"warnings": [], "pages": [], "preview_status": "pending"}
    for i, slide in enumerate(prs.slides, 1):
        shapes = list(_iter_shapes_recursive(slide.shapes))
        info = {"page": i, "charts": sum(bool(getattr(s, "has_chart", False)) for s in shapes), "warnings": []}
        if i <= len(pages):
            try:
                parsed = _parse_chart_data(pages[i-1]["body"])
                if parsed and not info["charts"]:
                    info["warnings"].append("有图表数据但没有生成图表")
            except (ValueError, TypeError) as exc:
                info["warnings"].append(str(exc))
        for s in shapes:
            if getattr(s, "has_text_frame", False):
                size = STYLE["title"] if s == slide.shapes.title else None
                _frame(s.text_frame, size)
                if _estimate_overflow(s, s.text):
                    info["warnings"].append(s.name + "：可能文字溢出")
            if getattr(s, "has_table", False):
                for row in s.table.rows:
                    for cell in row.cells:
                        _frame(cell.text_frame, STYLE["table"])
            if getattr(s, "has_chart", False):
                c = s.chart
                _font(c.font, STYLE["chart"])
                if c.has_title:
                    _frame(c.chart_title.text_frame, STYLE["chart"])
                if c.has_legend:
                    _font(c.legend.font, STYLE["chart"])
                for axis_name in ("category_axis", "value_axis"):
                    try:
                        _font(getattr(c, axis_name).tick_labels.font, STYLE["chart"])
                    except ValueError:
                        pass
                for plot in c.plots:
                    if plot.has_data_labels:
                        _font(plot.data_labels.font, STYLE["chart"])
            if s.left < 0 or s.top < 0 or s.left+s.width > prs.slide_width+12700 or s.top+s.height > prs.slide_height+12700:
                info["warnings"].append(s.name + "：超出页面边界")
        text_shapes = [s for s in shapes if getattr(s, "has_text_frame", False) and s.text.strip()]
        for j, a in enumerate(text_shapes):
            for b in text_shapes[j+1:]:
                width = min(a.left+a.width, b.left+b.width)-max(a.left,b.left)
                height = min(a.top+a.height, b.top+b.height)-max(a.top,b.top)
                if width > 0 and height > 0 and width*height > min(a.width*a.height,b.width*b.height)*.2:
                    info["warnings"].append(a.name + " / " + b.name + "：文本区域可能重叠，请查看预览")
        report["pages"].append(info)
        report["warnings"].extend(f"P{i} {w}" for w in info["warnings"])
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
            names = []
            for index in range(winreg.QueryInfoKey(key)[1]):
                names.append(winreg.EnumValue(key, index)[0].lower())
        if not any(STYLE["font"].lower() in n or (STYLE["font"] == "微软雅黑" and "yahei" in n) for n in names):
            report["warnings"].append("指定字体未在系统字体注册表中找到，渲染时可能替换：" + STYLE["font"])
    except (ImportError, OSError):
        report["warnings"].append("无法自动检查系统字体，请核对预览")
    prs.save(str(out_path))
    out = Path(out_path).resolve()
    preview = out.with_suffix(".preview")
    # Isolated process bounds COM hangs and never closes the user's open presentations.
    script = Path(__file__).with_name("ppt_preview.ps1")
    try:
        if os.environ.get("LUX_SKIP_PPT_PREVIEW") == "1":
            raise RuntimeError("已通过环境变量禁用预览")
        preview.mkdir(exist_ok=True)
        result = subprocess.run(["powershell", "-NoProfile", "-File", str(script), str(out), str(preview)], capture_output=True, text=True, timeout=90)
        images = sorted(preview.glob("slide-*.png"))
        if result.returncode or len(images) != len(prs.slides):
            raise RuntimeError(result.stderr.strip() or "预览页数不完整")
        import html
        preview.joinpath("index.html").write_text("<!doctype html><meta charset=utf-8><title>PPT逐页预览</title>" + "".join(f'<h2>P{i}</h2><img style="max-width:100%" src="{html.escape(p.name)}">' for i, p in enumerate(images, 1)), encoding="utf-8")
        report["preview_status"] = "rendered"
        report["previews"] = [str(p) for p in images]
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        report["preview_status"] = "unavailable"
        report["warnings"].append("逐页预览生成失败：" + str(exc))
    report["warnings"].append("溢出检查为启发式；字体替换和内容遮挡仍需核对逐页预览。")
    out.with_suffix(".qa.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
