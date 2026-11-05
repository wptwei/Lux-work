import unittest
from unittest.mock import Mock
from core.library_chat import library_list_html
from core.library_retrieval import retrieve_files
from tests import test_library_retrieval as fixtures
from tests.test_pdf_library import OWNER

class LibraryWorkspaceTests(unittest.TestCase):
    setUp=fixtures.SemanticRetrievalTests.setUp
    def test_list_escape_and_metadata(self):
        rows=self.store.search('',OWNER)
        rows[0]['title']='<script>alert(1)</script>'
        output=library_list_html(rows)
        self.assertNotIn('<script>',output)
        self.assertIn('KB',output)
        self.assertIn('上传日期',output)
        self.assertIn('更多',output)
        self.assertIn(rows[0]['version_id'],output)
    def test_list_and_chat_share_model_selection(self):
        reply=retrieve_files('找文件 员工评分',self.store,OWNER,lambda *a:'{"intent":"retrieve","ids":[1]}',renderer=library_list_html)
        self.assertIn('library-file-row',reply)
        self.assertIn('AI 根据',reply)
        self.assertIn(self.record['version_id'],reply)
    def test_recycle_list_has_no_download(self):
        rows=self.store.search('',OWNER);rows[0]['deleted']=True
        output=library_list_html(rows)
        self.assertNotIn('href=',output)
        self.assertNotIn('ds-preview-btn',output)
    def test_no_match_has_no_fabricated_link(self):
        output=retrieve_files('找文件 太空飞船',self.store,OWNER,lambda *a:'{"intent":"retrieve","ids":[]}',renderer=library_list_html)
        self.assertIn('暂未找到',output)
        self.assertNotIn('library-files/',output)
