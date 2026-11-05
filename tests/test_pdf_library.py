import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from core.pdf_library import PdfLibrary, LibraryError, extract_pdf
from core.library_chat import library_query, results_html, chat_results
from core.library_routes import library_routes


def pdf_bytes(texts=('Inspection rules',)):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        if text:
            font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            stream.set_data(f'BT /F1 12 Tf 20 250 Td ({escaped}) Tj ET'.encode('ascii'))
            page[NameObject('/Contents')] = writer._add_object(stream)
    buffer = io.BytesIO(); writer.write(buffer)
    return buffer.getvalue()


ADMIN = {'username': 'admin', 'role': 'admin', 'enabled': True}
OWNER = {'username': 'owner', 'role': 'supervisor', 'enabled': True}
VIEWER = {'username': 'viewer', 'role': 'operator', 'enabled': True}
OTHER = {'username': 'other', 'role': 'supervisor', 'enabled': True}


class PdfLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = PdfLibrary(self.root / 'library')
        self.pdf = self.root / '巡检制度.pdf'
        self.pdf.write_bytes(pdf_bytes())

    def upload(self, **kwargs):
        return self.store.upload(self.pdf, OWNER, **kwargs)

    def test_actual_pdf_extraction_and_page_numbers(self):
        pages, status = extract_pdf(pdf_bytes(('First page', 'Inspection rules')))
        self.assertEqual(status, 'indexed')
        self.assertIn('Inspection rules', pages[1])
        self.pdf.write_bytes(pdf_bytes(('First page', 'Inspection rules')))
        self.upload()
        rows = self.store.search('inspection', VIEWER)
        self.assertEqual(rows[0]['match_page'], 2)

    def test_chinese_body_and_metadata_search(self):
        with patch('core.pdf_library.extract_pdf', return_value=(['夜班设备巡检制度'], 'indexed')):
            self.upload(tags='安保 夜班', category='制度')
        self.assertEqual(len(self.store.search('设备 巡检', VIEWER)), 1)
        self.assertEqual(len(self.store.search('夜班', VIEWER)), 1)
        self.assertEqual(len(self.store.search('巡检', VIEWER, category='通知')), 0)

    def test_original_is_persistent_after_source_removed_and_restart(self):
        data = self.pdf.read_bytes(); rec = self.upload()
        self.pdf.unlink()
        reopened = PdfLibrary(self.store.root)
        stored, filename = reopened.download(rec['version_id'], VIEWER)
        self.assertEqual(stored.read_bytes(), data)
        self.assertEqual(filename, '巡检制度.pdf')

    def test_duplicates_and_versioned_originals(self):
        first = self.upload(); same = self.upload()
        self.assertTrue(same['duplicate']); self.assertEqual(same['version'], 1)
        self.pdf.write_bytes(pdf_bytes(('New rules',)))
        second = self.upload()
        self.assertEqual(first['document_id'], second['document_id'])
        self.assertEqual(second['version'], 2)
        self.assertEqual(self.store.search('', VIEWER)[0]['version'], 2)
        self.assertEqual(len(self.store.versions(first['document_id'], VIEWER)), 2)
        old, _ = self.store.download(first['version_id'], VIEWER)
        self.assertIn('Inspection rules', extract_pdf(old.read_bytes())[0][0])

    def test_private_search_and_download_do_not_leak(self):
        rec = self.upload(visibility='private')
        self.assertEqual(self.store.search('巡检', VIEWER), [])
        self.assertEqual(self.store.search('巡检', OTHER), [])
        self.assertEqual(len(self.store.search('巡检', ADMIN)), 1)
        for user in (VIEWER, OTHER):
            with self.assertRaises(LibraryError): self.store.download(rec['version_id'], user)
            with self.assertRaises(LibraryError): self.store.versions(rec['document_id'], user)

    def test_permissions_fail_closed(self):
        for user in (None, {**VIEWER, 'enabled': False}, {**ADMIN, 'username': 'system'}):
            with self.assertRaises(PermissionError): self.store.search('', user)
        with self.assertRaises(PermissionError): self.store.upload(self.pdf, VIEWER)
        rec = self.upload()
        with self.assertRaises(PermissionError): self.store.set_deleted(rec['document_id'], VIEWER, True)
        with self.assertRaises(PermissionError): self.store.search('', VIEWER, deleted=True)

    def test_trash_hides_search_and_all_downloads_restore_works(self):
        rec = self.upload()
        self.store.set_deleted(rec['document_id'], OWNER, True)
        self.assertEqual(self.store.search('', VIEWER), [])
        with self.assertRaises(LibraryError): self.store.download(rec['version_id'], OWNER)
        self.assertEqual(len(self.store.search('', OWNER, deleted=True)), 1)
        self.assertNotIn('href=', results_html(self.store.search('', OWNER, deleted=True)))
        self.store.set_deleted(rec['document_id'], OWNER, False)
        self.assertEqual(len(self.store.search('', VIEWER)), 1)

    def test_scanned_and_mixed_pdf_keep_shared_visibility(self):
        for texts, expected in [(('',), 'needs_ocr'), (('Rules', ''), 'partial')]:
            with self.subTest(expected=expected):
                self.pdf.write_bytes(pdf_bytes(texts))
                rec = self.upload()
                self.assertEqual(rec['status'], expected)
                self.assertEqual(rec['visibility'], 'shared')
                self.assertEqual(len(self.store.search('', VIEWER)), 1)
                original, _ = self.store.download(rec['version_id'], VIEWER)
                self.assertEqual(original.read_bytes(), self.pdf.read_bytes())

    def test_encrypted_invalid_and_over_page_limit_rejected(self):
        with self.assertRaises(LibraryError): extract_pdf(b'not pdf')
        with self.assertRaises(LibraryError): extract_pdf(pdf_bytes([''] * 201))
        writer = PdfWriter(); writer.add_blank_page(width=10, height=10); writer.encrypt('test-only')
        out = io.BytesIO(); writer.write(out)
        with self.assertRaises(LibraryError): extract_pdf(out.getvalue())

    def test_content_review_no_longer_blocks_library_storage(self):
        self.pdf.write_bytes(pdf_bytes(('Ordinary', 'Controlled')))
        seen = []
        def scan(text, **kwargs):
            seen.append(text)
            return 'review', [], ''
        record = self.upload(guard=SimpleNamespace(scan=scan))
        self.assertEqual(seen, [])
        self.assertEqual(len(self.store.search('Controlled', VIEWER)), 1)
        path, _ = self.store.download(record['version_id'], VIEWER)
        self.assertEqual(path.read_bytes(), self.pdf.read_bytes())

    def test_encrypted_pdf_original_can_be_stored_and_downloaded(self):
        writer = PdfWriter(); writer.add_blank_page(width=10, height=10); writer.encrypt('test-only')
        out = io.BytesIO(); writer.write(out); self.pdf.write_bytes(out.getvalue())
        record = self.upload()
        self.assertEqual(record['status'], 'stored_only')
        self.assertEqual(len(self.store.search('巡检', VIEWER)), 1)
        path, _ = self.store.download(record['version_id'], VIEWER)
        self.assertEqual(path.read_bytes(), out.getvalue())
        rendered = results_html(self.store.search('巡检', VIEWER))
        self.assertNotIn('0 页', rendered)

    def test_index_failure_still_preserves_original(self):
        with patch('core.pdf_library.extract_pdf', side_effect=LibraryError('解析组件暂不可用')):
            record = self.upload()
        self.assertEqual(record['status'], 'stored_only')
        path, _ = self.store.download(record['version_id'], VIEWER)
        self.assertEqual(path.read_bytes(), self.pdf.read_bytes())

    def test_non_pdf_is_not_accepted_as_original(self):
        self.pdf.write_bytes(b'not a PDF')
        with self.assertRaises(LibraryError): self.upload()
        self.assertEqual(self.store.search('', ADMIN), [])

    def test_sql_and_html_input_are_data(self):
        self.upload(tags='<img src=x onerror=alert(1)>')
        self.assertEqual(self.store.search("missing' OR 1=1 --", VIEWER), [])
        rows = self.store.search('', VIEWER)
        rows[0]['title'] = '<script>alert(1)</script>'
        rows[0]['snippet'] = '<img onerror=x>'
        self.assertNotIn('<script>', results_html(rows))
        self.assertNotIn('<img', results_html(rows))
        self.assertNotIn('<script>', chat_results(rows))

    def test_path_traversal_cannot_download(self):
        with self.assertRaises(LibraryError): self.store.download('../../secret', ADMIN)


class FileIntentTests(unittest.TestCase):
    def test_screenshot_and_spoken_requests_retrieve_title(self):
        for text in (
            '帮我调去取一下安保体系文件管理规范',
            '帮我调取一下安保体系文件管理规范',
            '能不能帮我调取一下安保体系文件管理规范？',
            '请问可以帮我找一下安保体系文件管理规范吗？',
            '麻烦你帮我把安保体系文件管理规范发我一份',
            '我想找一下安保体系文件管理规范',
        ):
            with self.subTest(text=text):
                self.assertEqual(library_query(text), '安保体系文件管理规范')

    def test_common_file_requests(self):
        cases = {
            '调取安保人员管理考核办法': '安保人员管理考核办法',
            '帮我调用安保体系文件管理规范': '安保体系文件管理规范',
            '请帮我把专家运维管理作业办法调出来': '专家运维管理作业办法',
            '下载一下监控中心管理作业办法.pdf': '监控中心管理作业办法',
            '请问有没有安保人员管理考核办法？': '安保人员管理考核办法',
            '帮我找一下最新的巡检制度给我看看': '巡检制度',
            '我要《巡检制度.pdf》': '巡检制度',
            'HD-KS-SW-003 安保体系文件管理规范.pdf': 'HD-KS-SW-003 安保体系文件管理规范',
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(library_query(message), expected)

    def test_browse_requests(self):
        for message in ('文件库里有哪些文件', '后台文件库有什么资料？', '有哪些文件', '帮我列出所有PDF', '请问文件库里面有哪些文件？'):
            with self.subTest(message=message):
                self.assertEqual(library_query(message), '')

    def test_explicit_and_natural_queries(self):
        for text in ('找文件 巡检制度', '帮我找一下最新的巡检制度', '搜索文件：巡检制度', '/找文件 巡检制度'):
            self.assertEqual(library_query(text), '巡检制度')
        self.assertEqual(library_query('帮我找巡检制度PDF'), '巡检制度')
        self.assertEqual(library_query('文件库'), '')

    def test_regular_conversation_not_routed(self):
        for text in ('帮我写巡检制度', '总结这份制度', '今天巡检正常', '找一个解决办法',
                     '请帮我分析巡检制度.pdf', '请问怎么上传PDF文件', '帮我写一份文件库使用说明'):
            self.assertIsNone(library_query(text))


class DownloadRouteTests(unittest.TestCase):
    def test_cookie_auth_disabled_user_and_document_permissions(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as tmp:
            store = PdfLibrary(Path(tmp) / 'store'); path = Path(tmp) / 'rules.pdf'; path.write_bytes(pdf_bytes())
            shared = store.upload(path, OWNER)
            private = store.upload(path, OWNER, visibility='private')
            users = {'viewer': dict(VIEWER), 'owner': dict(OWNER)}
            app = FastAPI(routes=library_routes(store, SimpleNamespace(get_public=users.get)))
            app.cookie_id = 'test'; app.tokens = {'viewer-token': 'viewer', 'owner-token': 'owner'}
            client = TestClient(app)
            url = '/library-files/' + shared['version_id']
            self.assertEqual(client.get(url).status_code, 401)
            client.cookies.set('access-token-unsecure-test', 'viewer-token')
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, path.read_bytes())
            self.assertIn('attachment', response.headers['content-disposition'])
            self.assertEqual(client.get('/library-files/' + private['version_id']).status_code, 404)
            users['viewer']['enabled'] = False
            self.assertEqual(client.get(url).status_code, 401)
