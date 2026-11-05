"""受控原文链接，仅开放两份已配置的制度 PDF，沿用工作台登录校验。"""
from __future__ import annotations

import html
from pathlib import Path
from urllib.parse import quote

SOURCE_DIR = Path(__file__).resolve().parent.parent / 'knowledge' / 'policy_sources'
DOCUMENTS = {
    'HD-KS-SW-010': ('4', 'HD-KS-SW-010_v4.pdf'),
    'HD-KS-SW-011': ('6', 'HD-KS-SW-011_v6.pdf'),
}


def _document_path(code: str, version: str) -> Path | None:
    spec = DOCUMENTS.get(code)
    if spec is None or str(version) != spec[0]:
        return None
    path = SOURCE_DIR / spec[1]
    # 白名单文件也不能通过链接指向其他目录。
    if path.is_symlink() or not path.is_file():
        return None
    resolved = path.resolve()
    if resolved.parent != SOURCE_DIR.resolve():
        return None
    return resolved


def policy_source_paths() -> list[str]:
    """传给 Gradio allowed_paths：只开放确实存在的两份文件。"""
    return [str(path) for code, (version, _) in DOCUMENTS.items()
            if (path := _document_path(code, version)) is not None]


def render_policy_citation(item: dict, request=None) -> str:
    citation = (f"《{item['document_title']}》 · {item['document_code']} · "
                f"{item['clause_no']} · 第{item['source_page']}页 · 版本{item['document_version']}")
    label = html.escape(citation)
    path = _document_path(item['document_code'], item['document_version'])
    if path is None:
        return label + ' <small>（对应版本原文未配置）</small>'
    try:
        page = int(item['source_page'])
    except (ValueError, TypeError):
        return label
    if page < 1:
        return label
    scope = getattr(getattr(request, 'request', None), 'scope', {})
    root_path = scope.get('root_path', '').rstrip('/')
    url = f"{root_path}/gradio_api/file={quote(path.as_posix(), safe='/:')}#page={page}"
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer" title="打开制度原文，第{page}页" '
            'style="color:inherit;text-decoration:underline;text-underline-offset:3px">'
            f'{label} ↗</a>')
