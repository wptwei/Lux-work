"""
本地读取 Word/Excel/PPT/TXT 文件内容 —— 文件不出本机，只把文字内容取出来。
用途：上传巡检台账、表格，AI 读取整理。取出的文字仍会过红线后才发模型。
"""
from __future__ import annotations

from pathlib import Path


def read_docx(path: str) -> str:
    import docx
    doc = docx.Document(path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for t in doc.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append(" | ".join(cells))
    return "\n".join(parts)


def read_xlsx(path: str) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"【工作表：{ws.title}】")
        for row in ws.iter_rows(values_only=True):
            if any(v is not None and str(v).strip() for v in row):
                out.append(" | ".join("" if v is None else str(v) for v in row))
    return "\n".join(out)


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _pptx_shape_lines(shape) -> list[str]:
    """递归提取形状（含组合）里的文字与表格。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    lines: list[str] = []
    try:
        is_group = shape.shape_type == MSO_SHAPE_TYPE.GROUP
    except Exception:
        is_group = False
    if is_group:
        for sub in shape.shapes:
            lines.extend(_pptx_shape_lines(sub))
        return lines
    if getattr(shape, "has_text_frame", False):
        text = shape.text_frame.text.strip()
        if text:
            lines.append(text)
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    return lines


def read_pptx(path: str) -> str:
    from pptx import Presentation

    prs = Presentation(path)
    out: list[str] = []
    for index, slide in enumerate(prs.slides, start=1):
        out.append(f"【第 {index} 页】")
        for shape in slide.shapes:
            out.extend(_pptx_shape_lines(shape))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                out.append(f"【备注】{notes}")
    return "\n".join(out)


def read_file(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".docx":
        return read_docx(path)
    if ext == ".pptx":
        return read_pptx(path)
    if ext in (".xlsx", ".xlsm"):
        return read_xlsx(path)
    if ext in (".txt", ".csv", ".md"):
        return read_text(path)
    raise ValueError(f"暂不支持的文件类型：{ext}（支持 .docx / .xlsx / .pptx / .txt / .csv）")
