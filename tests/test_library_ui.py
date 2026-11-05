import unittest
from unittest.mock import patch
import app
from tests import test_library_retrieval as fixtures
from tests.test_pdf_library import OWNER
class LibraryUITests(unittest.TestCase):
    setUp=fixtures.SemanticRetrievalTests.setUp
    def test_initial_none_query_loads_real_rows(self):
        with patch.object(app,'pdf_library',self.store),patch.object(app,'pdf_user',return_value=OWNER):
            output,_,_=app.refresh_pdf_library(None)
        self.assertIn('library-file-row',output)
        self.assertIn(self.record['version_id'],output)
    def test_search_uses_shared_semantic_selector(self):
        with patch.object(app,'pdf_library',self.store),patch.object(app,'pdf_user',return_value=OWNER),patch.object(app,'_library_model',return_value='{"intent":"retrieve","ids":[1]}'):
            output,_,_=app.refresh_pdf_library('员工评分制度')
        self.assertIn('AI 根据',output)
        self.assertIn(self.record['version_id'],output)
    def test_upload_error_unlocks_button(self):
        with patch.object(app,'upload_pdfs',side_effect=OSError()):
            states=list(app.upload_pdfs_ui(['sample.pdf'],'通用',''))
        self.assertFalse(states[0][2]['interactive'])
        self.assertTrue(states[-1][2]['interactive'])
        self.assertIn('上传失败',states[-1][0])
    def test_empty_upload_does_not_start_work(self):
        with patch.object(app,'upload_pdfs') as upload:
            states=list(app.upload_pdfs_ui(None,'通用',''))
        upload.assert_not_called()
        self.assertIn('等待上传',states[-1][0])
