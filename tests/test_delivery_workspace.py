import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app
from core.deliverable_store import DeliverableStore
class DeliveryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=DeliverableStore(self.root/'ledger.db')
        self.file=self.root/'report.docx';self.file.write_bytes(b'test')
        self.record=self.store.register(self.file,kind='Word',created_by='alice',logical_name='<测试>')
        self.store.register(self.file,kind='PPT',created_by='bob',logical_name='他人的交付')
    def render(self,kind='全部'):
        with patch.object(app,'deliverable_store',self.store),patch.object(app,'_current_user',return_value={'username':'alice'}):
            return app.filter_deliverables(kind)
    def test_owner_isolation_and_safe_rendering(self):
        output=self.render();self.assertIn('&lt;测试&gt;',output);self.assertNotIn('他人的交付',output)
        self.assertIn('report.docx',output);self.assertNotIn(str(self.root),output)
    def test_filter_and_missing_file_state(self):
        self.assertIn('暂无',self.render('PDF'))
        self.file.unlink();self.assertIn('文件需核查',self.render())
    def test_real_progress_only(self):
        self.assertNotIn('<progress',app.render_delivery_progress({'generation':{'status':'in_progress'}}))
        self.assertIn("value='2' max='5'",app.render_delivery_progress({'generation':{'status':'in_progress','completed_pages':2,'total_pages':5}}))
        self.assertEqual('',app.render_delivery_progress({'generation':{'status':'failed'}}))
