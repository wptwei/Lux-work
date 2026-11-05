import gc
import html
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from core import policy_source_links as links
from core.reward_penalty_engine import RewardPenaltyMatcher


class PolicySourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.matcher = RewardPenaltyMatcher()

    def test_pdf_links_and_pages(self):
        for event, code, page in (
            ('供应商在NPI区域内无人陪同独自游走', 'HD-KS-SW-010', 8),
            ('员工发现摄像头偏移后及时向安保报告', 'HD-KS-SW-011', 11),
        ):
            item = self.matcher.match(event)['matches'][0]
            output = links.render_policy_citation(item)
            self.assertIn('<a href="/gradio_api/file=', output)
            self.assertIn(code, output)
            self.assertIn(f'#page={page}', output)
            self.assertIn('target="_blank"', output)
            self.assertIn('rel="noopener noreferrer"', output)

    def test_missing_wrong_version_and_unknown_document(self):
        item = self.matcher.match('供应商在NPI区域内无人陪同独自游走')['matches'][0]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(links, 'SOURCE_DIR', Path(directory)):
                self.assertEqual([], links.policy_source_paths())
                self.assertNotIn('<a ', links.render_policy_citation(item))
                self.assertIn('原文未配置', links.render_policy_citation(item))
        self.assertNotIn('<a ', links.render_policy_citation(dict(item, document_version='999')))
        self.assertNotIn('<a ', links.render_policy_citation(dict(item, document_code='../config/users')))

    def test_escaping_and_mounted_root(self):
        item = self.matcher.match('供应商在NPI区域内无人陪同独自游走')['matches'][0]
        request = SimpleNamespace(request=SimpleNamespace(scope={'root_path': '/workspace'}))
        output = links.render_policy_citation(dict(item, document_title='<script>alert(1)</script>'), request)
        self.assertNotIn('<script>', output)
        self.assertIn('&lt;script&gt;', output)
        self.assertIn('href="/workspace/gradio_api/file=', output)

    def test_http_pdf_requires_login_and_rejects_other_files(self):
        import gradio as gr
        from gradio.routes import App
        from fastapi.testclient import TestClient
        with gr.Blocks() as blocks:
            gr.HTML('文件访问验证')
        blocks.auth = lambda username, password: username == 'link-test' and password == 'local-test-only'
        blocks.auth_message = '登录后查阅'
        blocks.allowed_paths = links.policy_source_paths()
        with TestClient(App.create_app(blocks)) as client:
            paths = []
            for event in ('供应商在NPI区域内无人陪同独自游走', '员工发现摄像头偏移后及时向安保报告'):
                item = self.matcher.match(event)['matches'][0]
                output = links.render_policy_citation(item)
                url = html.unescape(re.search(r'href="([^"]+)"', output).group(1))
                paths.append(urlsplit(url).path)
            self.assertEqual(401, client.get(paths[0]).status_code)
            login = client.post('/login', data={'username': 'link-test', 'password': 'local-test-only'})
            self.assertEqual(200, login.status_code)
            for path in paths:
                response = client.get(path)
                self.assertEqual(200, response.status_code)
                self.assertEqual('application/pdf', response.headers['content-type'])
                self.assertIn('inline', response.headers['content-disposition'])
                self.assertTrue(response.content.startswith(b'%PDF-'))
                served_file = Path(unquote(path.split('/file=', 1)[1]))
                self.assertEqual(served_file.read_bytes(), response.content)
            # 工作台自身代码文件不在允许的两个 PDF 中。
            other_file = Path(__file__).resolve().parent.parent / 'app.py'
            response = client.get('/gradio_api/file=' + other_file.as_posix())
            self.assertEqual(403, response.status_code)

    def test_real_callback_contains_clickable_citation_and_original_text(self):
        import app
        from core.reward_penalty_store import RewardPenaltyStore
        with tempfile.TemporaryDirectory() as directory:
            try:
                store = RewardPenaltyStore(Path(directory) / 'records.db')
                with patch.object(app, 'reward_penalty_store', store):
                    output, table = app.run_reward_penalty_match('供应商在NPI区域内无人陪同独自游走')
                self.assertIn('<a href="/gradio_api/file=', output)
                self.assertIn('#page=8', output)
                self.assertIn('条款原文：', output)
                self.assertIn('6.5.1.2.4', output)
                self.assertIn('6.5.1.2.4', table)
                self.assertEqual(1, len(store.list_recent(username='system', all_users=True)))
            finally:
                gc.collect()


if __name__ == '__main__':
    unittest.main()
