"""Explicit file retrieval intents; results originate from the store, never the LLM."""
import html
import re
from urllib.parse import quote

from core.pdf_library import STATUS_LABELS


_DOCUMENT = r'pdf|文件|资料|制度|规定|规章|手册|预案|规程|通知|报告|规范|管理办法|作业办法|考核办法'


def _file_keywords(query):
    query = query.strip(' ：:，,。？！?!')
    query = re.sub(r'^(?:(?:从|在)?(?:后台)?文件库(?:中|里)?(?:的)?|文件[：:\s]+|一下|一份|最新(?:版)?的?|有关|关于|那份|这份|的|\s)+', '', query)
    query = re.sub(r'(?:给我|给我们|看看|看一下|看下|一下|吗|么|吧|呢)+$', '', query)
    query = query.strip(' 《》“”\"\'')
    query = re.sub(r'(?:的)?(?:\.pdf|PDF|文件|资料)$', '', query, flags=re.I)
    return query.strip(' 《》“”\"\'')


def library_query(message):
    """Recognize retrieval requests without treating writing/QA as file lookup.

    None means ordinary assistant work; an empty string lists recent files.
    Matching is independent of the selected job skill and never calls a model.
    """
    text = (message or '').strip().rstrip('。？！?!').strip()
    text = re.sub(r'^(?:你好|您好|嗨)[，,！!\s]*', '', text)
    text = re.sub(r'^(?:(?:请问|麻烦你?|请|能不能|能否|可不可以|可以|你能|帮我们|帮我|给我们|给我)\s*)+', '', text)
    text = re.sub(r'^(?:从|在)(?:后台)?文件库(?:中|里)?\s*', '', text)
    if re.fullmatch(r'(?:(?:后台)?文件库(?:中|里)?(?:面)?(?:都)?(?:有)?(?:哪些|什么)(?:文件|资料)|(?:有哪些|有什么|查看所有|列出所有|列出全部)(?:PDF|文件|资料))', text, re.I):
        return ''
    match = re.match(r'^(?:/找文件|找文件|查文件|搜索文件|文件库)[：:\s]*(.*)$', text)
    if match:
        return _file_keywords(match.group(1))
    match = re.fullmatch(r'(?:把|将)(.+?)(?:调出来|调出|找出来|找出|发给我|发我一份|发我|下载下来|打开)(?:给我|看看|看一下|一下)?', text)
    if match:
        return _file_keywords(match.group(1))
    # Tolerate the observed input/ASR insertion “调去取” only in the
    # command, without rewriting the user's document title.
    match = re.match(r'^(?:调去取|调取|调出来|调出)\s*(.+)$', text)
    if match:
        return _file_keywords(match.group(1))
    match = re.match(r'^(?:调用|下载|打开|查找|搜索|找一下|查一下|找|有没有|我要|我想要|我想找一下|我想找|我需要)\s*(.+)$', text)
    if match and re.search(_DOCUMENT, match.group(1), re.I):
        return _file_keywords(match.group(1))
    # Pasting a filename is also a retrieval request; sentences about it are not.
    if re.fullmatch(r'[^\n。？！?!]+\.pdf', text, re.I) and not re.search(r'总结|分析|写|翻译|解释', text):
        return _file_keywords(text)
    return None


def file_url(version_id):
    return 'library-files/' + quote(version_id, safe='')


def results_html(rows):
    esc = html.escape
    if not rows:
        return '<p class="pdf-empty">没有找到可访问的文件。可缩短关键词，或到资源中心上传 PDF。</p>'
    cards = []
    for row in rows:
        page = f" · 命中第 {row['match_page']} 页" if row.get('match_page') else ''
        scope = '' if row['visibility'] == 'shared' else ' · 私有'
        pages = f"{row['page_count']} 页" if row['page_count'] else '原件'
        action = ('<p>已移入回收站，恢复后可下载。</p>' if row.get('deleted') else
                  f'<a href="{file_url(row["version_id"])}" target="_blank" rel="noopener">下载 PDF</a>')
        cards.append(f'''<article class="pdf-result"><strong>{esc(row['title'])}</strong>
          <p>{esc(row['category'])} · V{row['version']} · {pages}{esc(scope)}{page}</p>
          <p>{esc(STATUS_LABELS[row['status']])} · 更新于 {esc(row['updated'][:10])}</p>
          <p>{esc(row.get('snippet',''))}</p>{action}
          <small>文件编号：{esc(row['id'])}</small></article>''')
    return '<div class="pdf-results">' + ''.join(cards) + '</div>'


def chat_results(rows, *, semantic=False):
    if not rows:
        return '已查询后台文件库，没有找到匹配的 PDF。可以换一个文件名关键词，或检查文件是否已经入库。'
    lines = ['根据你的描述，以下文件可能相关：' if semantic else '已在文件库找到以下 PDF：']
    if len(rows) > 1:
        lines.append('请选择要打开的文件（最多显示 8 份，均为最新入库版本）。')
    for row in rows:
        title = html.escape(row['title']).replace('[', '&#91;').replace(']', '&#93;')
        basis = 'AI 根据文件名、分类与标签选出候选，尚未核验正文相关性。' if semantic else (
            f"关键词命中原文件第 {int(row['match_page'])} 页。" if row.get('match_page') else '关键词匹配文件元数据或正文；未指定关键词时按最近更新列出。')
        basis = row.get('match_reason') or basis
        lines.append(
            f'<article class="pdf-result ds-assistant-file" aria-label="PDF 候选文件">'
            f'<div class="assistant-file-meta"><span>PDF</span><span>V{int(row["version"])}</span></div>'
            f'<strong>{title}</strong><p class="assistant-match"><span>匹配依据</span>{html.escape(basis)}</p>'
            f'<a href="{file_url(row["version_id"])}" target="_blank" rel="noopener">下载这份 PDF</a></article>'
        )
    return '\n\n'.join(lines)


def library_list_html(rows, semantic=False):
    """Plain library rows. Download still uses the existing permission-checked route."""
    if not rows:
        return '<p class="library-empty">未找到可访问的 PDF。请调整关键词或上传文件。</p>'
    items=[]
    for row in rows:
        size=int(row.get('size_bytes') or 0)
        size_label=f'{size / 1048576:.1f} MB' if size >= 1048576 else f'{size / 1024:.1f} KB'
        reason=row.get('match_reason') or ('AI 根据文件名、分类和标签选出的候选；请打开核对。' if semantic else '')
        title=html.escape(row['title'])
        action=('<span>已在回收站</span>' if row.get('deleted') else
                f'<button type="button" class="ds-preview-btn">打开</button><details><summary aria-label="更多文件操作">更多</summary><a href="{file_url(row["version_id"])}" target="_blank" rel="noopener">下载 PDF</a></details>')
        items.append(f'<div class="library-file-row pdf-result" role="row"><div role="cell" class="library-name"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M6 3h8l4 4v14H6V3Zm8 0v5h4M9 12h6m-6 4h6"/></svg><div><strong>{title}</strong>'
                     + (f'<small class="library-match">{html.escape(reason)}</small>' if reason else '')
                     + f'</div></div><span role="cell">PDF</span><span role="cell">{size_label}</span><time role="cell">{html.escape(str(row.get("uploaded_at") or row.get("updated") or "")[:10])}</time><div role="cell" class="library-row-actions">{action}</div></div>')
    return '<div class="library-list" role="table" aria-label="文件列表"><div role="row" class="library-list-head"><span role="columnheader">文件名称</span><span role="columnheader">类型</span><span role="columnheader">大小</span><span role="columnheader">上传日期</span><span role="columnheader">操作</span></div>'+''.join(items)+'</div>'
