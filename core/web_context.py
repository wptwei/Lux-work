"""联网证据的标准化后处理：来源去重、证据裁剪、上下文预算、提示词与引用校验。

网页文本属于不可信证据：只作为用户提示词里的数据，不拼入 system 指令；
来源链接只允许 http(s)，标题一律转义，禁止执行原始 HTML。
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from core.web_search import SearchResult

_MAX_URL_LEN = 400
_SCHEMES = {"http", "https"}
# 联网证据用 [S1]，本地正文证据用 [L1]；两者都参与“是否引用了不存在的来源”校验。
_CITATION = re.compile(r"\[([SL]\d+)\]")


def safe_url(url: str) -> str:
    """只允许 http(s) 链接；javascript:、data: 等返回空串，禁止当链接渲染。"""
    url = (url or "").strip()
    if not url or len(url) > _MAX_URL_LEN:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme.lower() not in _SCHEMES or not parts.netloc:
        return ""
    return url


def deduplicate(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for r in results:
        key = (r.url or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def assign_ids(results: list[SearchResult]) -> list[SearchResult]:
    return [
        SearchResult(
            id=f"S{i + 1}", title=r.title, url=r.url, content=r.content,
            published_at=r.published_at, retrieved_at=r.retrieved_at,
        )
        for i, r in enumerate(results)
    ]


def trim_evidence(
    results: list[SearchResult],
    *,
    max_sources: int = 4,
    max_total_chars: int = 2400,
    per_source_chars: int = 600,
) -> list[SearchResult]:
    """保留短证据而非完整网页，避免挤占小上下文里的系统约束与当前问题。"""
    budget = max(0, int(max_total_chars))
    out: list[SearchResult] = []
    for r in results[: max(1, int(max_sources))]:
        content = (r.content or "").strip() or (r.title or "").strip()
        if not content:
            continue
        if len(content) > per_source_chars:
            content = content[:per_source_chars].rstrip() + "…"
        out.append(
            SearchResult(id=r.id, title=r.title, url=r.url, content=content,
                         published_at=r.published_at, retrieved_at=r.retrieved_at)
        )
        budget -= len(content)
        if budget <= 0:
            break
    return out


def prepare_evidence(results: list[SearchResult]) -> list[SearchResult]:
    """去重 → 裁剪 → 分配来源 ID；按顺序串成证据块前调用一次。"""
    return assign_ids(trim_evidence(deduplicate(results)))


def build_web_prompt(question: str, results: list[SearchResult]) -> str:
    """把问题与证据拼成用户提示词；证据只作为数据，不进入 system。"""
    lines = [(question or "").strip()]
    if results:
        lines.append("")
        lines.append("【联网资料（仅作参考证据，可能不完整或过时，先核对再引用）】")
        for r in results:
            title = (r.title or "(无标题)").strip()
            date = f" · 发布时间 {r.published_at}" if r.published_at else ""
            lines.append(f"[{r.id}] {title}{date}")
            lines.append(f"正文：{r.content}")
        lines.append("")
        lines.append("请只依据上述联网资料回答，并在句中用 [S1] 等标注依据；资料不足时明确说明无法确认，不得编造来源或链接。")
    return "\n".join(lines)


def build_evidence_prompt(question, web_results, local_blocks, textless=()) -> str:
    """综合检索提示词：本地正文 + 联网证据同处用户消息，均按数据对待。

    local_blocks 必须来自已读取的正文；只有文件名、没有正文的文件不得参与分析。
    """
    lines = [(question or "").strip()]
    if local_blocks:
        lines += ["", "【本地资料正文（来自已授权文件库，仅作证据）】"]
        for block in local_blocks:
            pages = "、".join(str(p) for p in block.get("pages") or ())
            where = f"（第 {pages} 页）" if pages else ""
            lines.append(f"[{block['id']}] {block.get('title', '(未命名)')}{where}")
            lines.append(f"正文：{block.get('content', '')}")
    if web_results:
        lines += ["", "【联网资料（仅作参考证据，可能不完整或过时，先核对再引用）】"]
        for r in web_results:
            title = (r.title or "(无标题)").strip()
            date = f" · 发布时间 {r.published_at}" if r.published_at else ""
            lines.append(f"[{r.id}] {title}{date}")
            lines.append(f"正文：{r.content}")
    if textless:
        lines += ["", "【未能读取正文的本地文件】"]
        lines += [f"- {name}（无可提取正文，可能需 OCR）" for name in textless]
    lines += [
        "",
        "要求：先核对证据再作答；引用本地资料用 [L1] 形式标注，引用联网资料用 [S1] 形式标注。"
        "未能读取正文的文件不得凭文件名推测其内容，需要时明确说明无法确认。"
        "资料不足时直接说明，不得编造来源、页码或链接。",
    ]
    return "\n".join(lines)


def render_local_sources(blocks) -> str:
    """文末本地来源块：只列真实读取过的文件与页码。"""
    if not blocks:
        return ""
    lines = ["\n\n---\n\n**本地来源**"]
    for block in blocks:
        title = html.escape((block.get("title") or "(未命名)").strip())
        pages = "、".join(str(p) for p in block.get("pages") or ())
        meta = f" · V{block['version']}" if block.get("version") else ""
        if pages:
            meta += f" · 第 {pages} 页"
        lines.append(f"- [{block['id']}] {title}{html.escape(meta)}")
    return "\n".join(lines)


def render_sources(results: list[SearchResult]) -> str:
    """文末来源块：标题转义、链接仅 http(s)、带发布时间与检索时间。"""
    if not results:
        return ""
    lines = ["\n\n---\n\n**联网来源**"]
    for r in results:
        title = html.escape((r.title or "(无标题)").strip())
        url = safe_url(r.url)
        meta = []
        if r.published_at:
            meta.append(html.escape(r.published_at))
        meta.append(f"检索于 {html.escape(r.retrieved_at)}")
        link = f"[{html.escape(url)}]({url})" if url else "（链接不可用）"
        lines.append(f"- [{r.id}] {title} · {' · '.join(meta)} — {link}")
    return "\n".join(lines)


def referenced_ids(text: str) -> set[str]:
    return set(_CITATION.findall(text or ""))


def unknown_citations(text: str, allowed_ids: list[str]) -> set[str]:
    """正文引用了哪些不存在于注入来源里的 ID；只作校验/留痕，不据此改写输出。"""
    allowed = set(allowed_ids)
    return {cid for cid in referenced_ids(text) if cid not in allowed}
