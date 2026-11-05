"""Local PDF originals, page index, versions and per-request access control."""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.paths import data_path
from core.sqlite_util import connect_db

ROOT = data_path("file_library")
STATUS_LABELS = {'indexed': '正文可检索', 'partial': '部分正文可检索',
                 'needs_ocr': '可按文件名查找', 'stored_only': '原件已保存，可按文件名查找'}


class LibraryError(ValueError):
    pass


def require_user(user):
    if not user or not user.get('enabled') or user.get('username') == 'system':
        raise PermissionError('请先使用个人账号登录。')
    if user.get('role') not in {'admin', 'supervisor', 'reviewer', 'operator'}:
        raise PermissionError('当前账号无权使用文件库。')


def can_manage(user):
    return bool(user and user.get('enabled') and user.get('role') in {'admin', 'supervisor'})


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def extract_pdf(data: bytes) -> tuple[list[str], str]:
    if not data or len(data) > 25 * 1024 * 1024:
        raise LibraryError('PDF 为空或超过 25MB。')
    if b'%PDF-' not in data[:1024]:
        raise LibraryError('文件内容不是有效 PDF。')
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise LibraryError('PDF 组件未安装，请运行 install_library.ps1。') from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise LibraryError('加密 PDF 无法提取正文。')
        if not 1 <= len(reader.pages) <= 200:
            raise LibraryError('PDF 超出单次正文索引的页数范围。')
        pages, count = [], 0
        for page in reader.pages:
            text = (page.extract_text() or '').replace('\x00', '').strip()
            count += len(text)
            if count > 2_000_000:
                raise LibraryError('PDF 正文过长，请拆分后上传。')
            pages.append(text)
    except LibraryError:
        raise
    except Exception as exc:
        raise LibraryError('PDF 解析失败，请检查文件是否损坏。') from exc
    populated = sum(bool(p.strip()) for p in pages)
    return pages, 'indexed' if populated == len(pages) else 'partial' if populated else 'needs_ocr'


class PdfLibrary:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.files = self.root / 'originals'
        self.files.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'library.db'
        with connect_db(self.db) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS documents(
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
                  tags TEXT NOT NULL, owner TEXT NOT NULL, visibility TEXT NOT NULL,
                  deleted INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(
                  id TEXT PRIMARY KEY, document_id TEXT NOT NULL, version INTEGER NOT NULL,
                  filename TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL, page_count INTEGER NOT NULL, status TEXT NOT NULL,
                  created TEXT NOT NULL, UNIQUE(document_id,version));
                CREATE TABLE IF NOT EXISTS pages(
                  version_id TEXT NOT NULL, page_number INTEGER NOT NULL, content TEXT NOT NULL,
                  PRIMARY KEY(version_id,page_number));
                CREATE TABLE IF NOT EXISTS audit(
                  id INTEGER PRIMARY KEY, username TEXT NOT NULL, action TEXT NOT NULL,
                  document_id TEXT NOT NULL, version_id TEXT NOT NULL, created TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS versions_doc ON versions(document_id,version);
            ''')

    @staticmethod
    def _allowed(row, user):
        return user['role'] == 'admin' or row['visibility'] == 'shared' or row['owner'] == user['username']

    @staticmethod
    def _audit(c, user, action, document_id, version_id=''):
        c.execute('INSERT INTO audit(username,action,document_id,version_id,created) VALUES(?,?,?,?,?)',
                  (user['username'], action, document_id, version_id, _now()))

    def upload(self, path, user, *, category='通用', tags='', visibility='shared', guard=None, region=None):
        require_user(user)
        if not can_manage(user):
            raise PermissionError('只有主管或管理员可以上传和维护文件。')
        path = Path(path)
        if path.suffix.lower() != '.pdf' or not path.is_file():
            raise LibraryError('请选择 PDF 文件。')
        if path.stat().st_size > 25 * 1024 * 1024:
            raise LibraryError('PDF 超过 25MB。')
        if visibility not in {'shared', 'private'}:
            raise LibraryError('无效的可见范围。')
        name = path.name
        title = path.stem.strip()
        category, tags = (category or '通用').strip(), (tags or '').strip()
        if not title or len(name) > 180 or len(category) > 40 or len(tags) > 300:
            raise LibraryError('文件名、分类或标签过长。')
        data = path.read_bytes()
        if not data or len(data) > 25 * 1024 * 1024 or b'%PDF-' not in data[:1024]:
            raise LibraryError('请选择有效且不超过 25MB 的 PDF 文件。')
        # Storage is independent of content review and optional text extraction.
        # guard/region remain accepted for compatibility with earlier callers.
        try:
            pages, status = extract_pdf(data)
        except LibraryError:
            pages, status = [], 'stored_only'
        digest, now = hashlib.sha256(data).hexdigest(), _now()
        new_path = None
        try:
            with connect_db(self.db) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM documents WHERE title=? AND category=? AND owner=? AND visibility=? AND deleted=0',
                                (title, category, user['username'], visibility)).fetchone()
                doc_id = row['id'] if row else 'PDF-' + uuid.uuid4().hex[:16].upper()
                latest = c.execute('SELECT * FROM versions WHERE document_id=? ORDER BY version DESC LIMIT 1', (doc_id,)).fetchone()
                if latest and latest['sha256'] == digest:
                    return {'document_id': doc_id, 'version_id': latest['id'], 'version': latest['version'],
                            'duplicate': True, 'status': latest['status'], 'visibility': visibility, 'title': title}
                version = latest['version'] + 1 if latest else 1
                version_id = uuid.uuid4().hex
                key = version_id + '.pdf'
                new_path = self.files / key
                with new_path.open('xb') as f:
                    f.write(data)
                if not row:
                    c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,0,?,?)',
                              (doc_id, title, category, tags, user['username'], visibility, now, now))
                else:
                    c.execute('UPDATE documents SET tags=?,updated=? WHERE id=?', (tags, now, doc_id))
                c.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (version_id, doc_id, version, name, key, digest, len(data), len(pages), status, now))
                c.executemany('INSERT INTO pages VALUES(?,?,?)', [(version_id, i + 1, text) for i, text in enumerate(pages)])
                self._audit(c, user, 'upload', doc_id, version_id)
        except Exception:
            if new_path:
                new_path.unlink(missing_ok=True)
            raise
        return {'document_id': doc_id, 'version_id': version_id, 'version': version,
                'duplicate': False, 'status': status, 'visibility': visibility, 'title': title}

    def catalog(self, user):
        """Visible latest metadata for semantic selection; never include PDF bodies."""
        require_user(user)
        with connect_db(self.db) as c:
            sql = '''SELECT d.id,d.title,d.category,d.tags,d.visibility,d.updated,d.owner,
                            v.id AS version_id,v.version,v.status,v.page_count,v.size_bytes,v.created AS uploaded_at
                     FROM documents d JOIN versions v ON d.id=v.document_id
                     WHERE d.deleted=0 AND v.version=(SELECT MAX(version) FROM versions WHERE document_id=d.id)'''
            args = []
            if user['role'] != 'admin':
                sql += " AND (d.visibility='shared' OR d.owner=?)"
                args.append(user['username'])
            sql += ' ORDER BY d.updated DESC,d.id'
            return [dict(row) for row in c.execute(sql, args)]

    def search(self, query, user, *, category='', deleted=False, limit=30):
        require_user(user)
        if deleted and not can_manage(user):
            raise PermissionError('只有主管或管理员可以查看回收站。')
        query = (query or '').strip()[:200]
        terms = re.findall(r'[\w\u4e00-\u9fff]+', query.lower())[:8]
        with connect_db(self.db) as c:
            sql = '''SELECT d.*,v.id AS version_id,v.version,v.filename,v.status,v.page_count,v.size_bytes,v.created AS uploaded_at
                     FROM documents d JOIN versions v ON d.id=v.document_id
                     WHERE d.deleted=? AND v.version=(SELECT MAX(version) FROM versions WHERE document_id=d.id)'''
            args = [int(deleted)]
            if user['role'] != 'admin':
                sql += " AND (d.visibility='shared' OR d.owner=?)"
                args.append(user['username'])
            if category and category != '全部':
                sql += ' AND d.category=?'
                args.append(category)
            # Permission filtering happens in SQL before pages/snippets are inspected.
            for term in terms:
                sql += ''' AND (instr(lower(d.title||' '||d.category||' '||d.tags),?)>0 OR EXISTS
                           (SELECT 1 FROM pages p WHERE p.version_id=v.id AND instr(lower(p.content),?)>0))'''
                args.extend([term, term])
            sql += ' ORDER BY d.updated DESC,d.id LIMIT ?'
            args.append(max(1, min(int(limit), 100)))
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
            for row in rows:
                row['match_page'], row['snippet'] = None, ''
                if terms:
                    for p in c.execute('SELECT page_number,content FROM pages WHERE version_id=? ORDER BY page_number', (row['version_id'],)):
                        locations = [p['content'].lower().find(t) for t in terms]
                        positions = [i for i in locations if i >= 0]
                        if positions:
                            pos = min(positions)
                            row['match_page'] = p['page_number']
                            row['snippet'] = p['content'][max(0, pos - 35):pos + 145].replace('\n', ' ')
                            break
            self._audit(c, user, 'search', '')
        return rows

    def read_text(self, version_id, user, *, max_chars=4000):
        """Permission-checked page text for grounded analysis.

        Returns only extracted text and page numbers; never file bytes or storage
        keys. Callers must treat the text as untrusted data, not instructions.
        """
        require_user(user)
        budget = max(200, int(max_chars))
        with connect_db(self.db) as c:
            row = c.execute('''SELECT d.id,d.title,d.category,d.visibility,d.owner,d.deleted,
                                      v.id AS version_id,v.version,v.status,v.page_count
                               FROM documents d JOIN versions v ON d.id=v.document_id
                               WHERE v.id=?''', (version_id,)).fetchone()
            if not row or row['deleted'] or not self._allowed(row, user):
                raise LibraryError('文件不存在或无权访问。')
            pages, used = [], 0
            for page in c.execute(
                    'SELECT page_number,content FROM pages WHERE version_id=? ORDER BY page_number',
                    (version_id,)):
                text = (page['content'] or '').strip()
                if not text:
                    continue
                room = budget - used
                if room <= 0:
                    break
                pages.append({'page': page['page_number'], 'text': text[:room]})
                used += min(len(text), room)
            self._audit(c, user, 'read', row['id'], version_id)
        return {'version_id': row['version_id'], 'title': row['title'],
                'document_id': row['id'], 'version': row['version'],
                'status': row['status'], 'pages': pages,
                'chars': sum(len(p['text']) for p in pages)}

    def versions(self, doc_id, user):
        require_user(user)
        with connect_db(self.db) as c:
            doc = c.execute('SELECT * FROM documents WHERE id=? AND deleted=0', (doc_id,)).fetchone()
            if not doc or not self._allowed(doc, user):
                raise LibraryError('文件不存在或无权访问。')
            return [dict(r) for r in c.execute('SELECT id,version,filename,status,created FROM versions WHERE document_id=? ORDER BY version DESC', (doc_id,))]

    def set_deleted(self, doc_id, user, deleted):
        require_user(user)
        if not can_manage(user):
            raise PermissionError('只有主管或管理员可以维护文件。')
        with connect_db(self.db) as c:
            doc = c.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
            if not doc or not self._allowed(doc, user):
                raise LibraryError('文件不存在或无权访问。')
            c.execute('UPDATE documents SET deleted=?,updated=? WHERE id=?', (int(bool(deleted)), _now(), doc_id))
            self._audit(c, user, 'trash' if deleted else 'restore', doc_id)

    def download(self, version_id, user):
        require_user(user)
        with connect_db(self.db) as c:
            row = c.execute('''SELECT d.*,v.filename,v.storage_key,v.id AS version_id FROM documents d
                               JOIN versions v ON d.id=v.document_id WHERE v.id=? AND d.deleted=0''', (version_id,)).fetchone()
            if not row or not self._allowed(row, user):
                raise LibraryError('文件不存在或无权访问。')
            path = (self.files / row['storage_key']).resolve()
            if path.parent != self.files.resolve() or not path.is_file():
                raise LibraryError('原文件不存在或路径无效。')
            self._audit(c, user, 'download', row['id'], version_id)
            return path, row['filename']
