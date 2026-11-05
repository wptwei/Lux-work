"""周报页面与图表数据协议：纯文本/JSON 解析，不依赖 UI 或 Office 渲染。

保留各调用方的数据块显示规则；事实核验仍由 weekly_report_agent 负责。
"""
from __future__ import annotations

import json
import math
import re

CHART_TYPES = frozenset({"column", "bar", "line", "pie"})
_CHART_BLOCK_RE = re.compile(r"(?im)^\s*(?:图表数据|chart\s*data)\s*[：:]?\s*$")
_TABLE_BLOCK_RE = re.compile(r"(?im)^\s*(?:表格数据|table\s*data)\s*[：:]?\s*$")
_KEY_MESSAGE_RE = re.compile(r"(?i)^\s*key\s*message\s*[：:]\s*(.*)$")


def validate_chart(spec):
    if not isinstance(spec, dict):
        raise ValueError("chart 必须是对象")
    if spec.get("type") not in CHART_TYPES:
        raise ValueError("图表类型必须为 column/bar/line/pie")
    cats, series = spec.get("categories"), spec.get("series")
    if not isinstance(cats, list) or not cats or not all(isinstance(x, str) and x.strip() for x in cats):
        raise ValueError("categories 必须包含非空分类名称")
    if not isinstance(spec.get("unit"), str):
        raise ValueError("必须明确 unit（无单位时填空字符串）")
    if not isinstance(series, list) or not series:
        raise ValueError("series 不能为空")
    for s in series:
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not s["name"].strip():
            raise ValueError("系列缺少 name")
        vals = s.get("values")
        if not isinstance(vals, list) or len(vals) != len(cats):
            raise ValueError("数值长度与分类数量不一致")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vals):
            raise ValueError("数值必须为有限数字，不允许缺失值")
    if spec["type"] == "pie" and (len(series) != 1 or any(v < 0 for v in series[0]["values"]) or sum(series[0]["values"]) <= 0):
        raise ValueError("饼图需要一个非负且总和大于零的系列")
    return spec

def parse_json_chart(body):
    match = re.search(r"(?ms)^CHART_JSON:\s*\n(.*?)\nEND_CHART_JSON", body)
    if not match:
        return None
    spec = validate_chart(json.loads(match.group(1)))
    return {"categories": spec["categories"], "series_names": [s["name"] for s in spec["series"]],
            "series": [s["values"] for s in spec["series"]], "type": spec["type"], "unit": spec["unit"]}

def strip_json(body):
    return re.sub(r"(?ms)^CHART_JSON:.*?^END_CHART_JSON\s*", "", body)

def _strip_markdown(text: str) -> str:
    """去掉代码块围栏和 YAML frontmatter，避免模型输出被原样塞进 PPT。"""
    if not text:
        return ""
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n?", "", text, flags=re.S)
    text = text.replace("`", "")
    text = text.replace("~~~", "")
    return text.strip()

def parse_report(text: str) -> list[dict]:
    """动态解析周报页；兼容旧版 P1-P8，也支持显式页面类型与标题。

    新格式：## P3 [type:operations] | 专区运营看板
    旧格式：## P3 专区运营看板\n专区运营看板\n...
    页数不再固定为 8，实际页数由文稿中的页面标记决定。
    """
    import json
    if isinstance(text, str) and text.lstrip().startswith("{"):
        document = json.loads(text)
        pages = []
        for index, page in enumerate(document["pages"], 1):
            body = page.get("body", "")
            if page.get("chart") is not None:
                try:
                    spec = validate_chart(page["chart"])
                except ValueError as exc:
                    raise ValueError(f"P{index}：{exc}") from exc
                body += "\nCHART_JSON:\n" + json.dumps(spec, ensure_ascii=False) + "\nEND_CHART_JSON\n"
            pages.append({"no": index, "title": page.get("title", f"P{index}"), "type": page.get("type", "generic"), "body": body})
        return pages
    text = _strip_markdown(text)
    pattern = re.compile(
        r"(?m)^\s*(?:#{1,6}\s*)?(?:P(?P<p>\d{1,2})|【\s*P(?P<q>\d{1,2})\s*】|第\s*(?P<r>\d{1,2})\s*页)(?P<rest>[^\n]*)\n?"
    )
    markers = list(pattern.finditer(text))
    pages: list[dict] = []
    for idx, match in enumerate(markers):
        number = int(next((g for g in (match.group('p'), match.group('q'), match.group('r')) if g), '0'))
        start = match.end()
        end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
        body = text[start:end].strip()
        rest = match.group('rest') or ''
        type_match = re.search(r"\[type:([a-z_]+)\]", rest, re.I)
        title_match = re.search(r"\|\s*(.*?)\s*$", rest)
        page_type = type_match.group(1).lower() if type_match else ''
        explicit_title = title_match.group(1).strip() if title_match else ''
        first = body.splitlines()[0].lstrip('# ').strip() if body.splitlines() else ''
        title = explicit_title or first or f"P{number}"
        pages.append({"no": number, "title": title, "body": body, "type": page_type})
    return pages

def split_title_body(page: dict) -> tuple[str, str]:
    """分开页面标题与正文，避免正文首行与标题重复显示。"""
    title = (page.get("title") or "").strip()
    body = (page.get("body") or "").strip()
    lines = body.splitlines()
    if lines and lines[0].lstrip("# ").strip() == title:
        body = "\n".join(lines[1:]).strip()
    return title, body

def _extract_block(body: str, block: str) -> list[str]:
    """提取以“图表数据：”或“表格数据：”开头的结构化数据块。"""
    pattern = _CHART_BLOCK_RE if block == "chart" else _TABLE_BLOCK_RE
    lines = (body or "").splitlines()
    start = None
    for idx, line in enumerate(lines):
        if pattern.match(line.strip()):
            start = idx + 1
            break
    if start is None:
        return []
    output = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            if output:
                break
            continue
        if _KEY_MESSAGE_RE.match(stripped) or _CHART_BLOCK_RE.match(stripped) or _TABLE_BLOCK_RE.match(stripped):
            break
        if stripped.startswith("## P"):
            break
        output.append(stripped)
    return output

def _split_rows(lines: list[str]) -> list[list[str]]:
    rows = []
    for line in lines:
        if not line.strip():
            continue
        if "|" in line:
            cells = [part.strip() for part in line.strip().strip("|").split("|")]
        elif "\t" in line:
            cells = [part.strip() for part in line.split("\t")]
        elif "," in line:
            cells = [part.strip() for part in line.split(",")]
        else:
            cells = [part.strip() for part in re.split(r"\s{2,}", line.strip())]
        rows.append(cells)
    return rows

def _to_number(value: str):
    cleaned = str(value).strip().replace(",", "").replace("，", "")
    cleaned = re.sub(r"(?:次|人次|人|件|个|台|项|条|起|元|万元|pcs|PCS|×|％|%)$", "", cleaned)
    try:
        return float(cleaned) if "." in cleaned else int(cleaned)
    except (ValueError, TypeError):
        return None

def _parse_chart_data(body: str) -> dict | None:
    """解析安全、明确的结构化图表数据，不从普通叙述中猜测图表数值。

    推荐格式：
      图表数据：
      类别|本周|上周
      违禁品|27|21
      异常事件|5|8
    """
    structured = parse_json_chart(body)
    if structured is not None:
        return structured
    rows = _split_rows(_extract_block(body, "chart"))
    if len(rows) < 2:
        return None
    width = len(rows[0])
    if width < 2 or any(len(row) != width for row in rows):
        return None
    first = rows[0][0].strip().lower()
    header_words = {"类别", "项目", "指标", "分类", "类型", "category", "item", "metric"}
    has_header = first in header_words or all(_to_number(cell) is None for cell in rows[0][1:])
    if has_header:
        series_names = rows[0][1:]
        data_rows = rows[1:]
    else:
        series_names = ["数值" if width == 2 else f"系列{i}" for i in range(1, width)]
        data_rows = rows
    if not data_rows or not series_names or len(series_names) > 6:
        return None
    categories, series = [], [[] for _ in series_names]
    for row in data_rows:
        if len(row) != width:
            return None
        category = row[0].strip()
        values = [_to_number(value) for value in row[1:]]
        if not category or any(value is None for value in values):
            return None
        categories.append(category)
        for idx, value in enumerate(values):
            series[idx].append(value)
    if not categories:
        return None
    return {"categories": categories, "series_names": series_names, "series": series}

def visible_body(body: str, *, include_english: bool = False, remove_json: bool = True) -> str:
    """过滤呈现层数据块；参数保留模板、新建 PPT 和长度检查的原有差异。"""
    if remove_json:
        body = strip_json(body)
    output = []
    skipping = False
    for line in (body or "").splitlines():
        value = line.strip()
        is_block = (
            (_CHART_BLOCK_RE.match(value) or _TABLE_BLOCK_RE.match(value))
            if include_english else
            re.match(r"(?i)^\s*(?:图表数据|表格数据)\s*[：:]?\s*$", value)
        )
        if is_block:
            skipping = True
            continue
        if skipping:
            if not value:
                skipping = False
            elif _KEY_MESSAGE_RE.match(value):
                skipping = False
                output.append(line)
            continue
        output.append(line)
    return "\n".join(output).strip()

