import unittest
from unittest.mock import patch
import app

class AssistantLifecycleTests(unittest.TestCase):
    def test_stream_lock_and_release(self):
        turn=([{'role':'assistant','content':'正文'}],{},'',{},'t')
        with patch.object(app,'respond',return_value=iter([turn])), patch.object(app.task_queue,'get',return_value={'status':'COMPLETED'}):
            updates=list(app.respond_ui('你好',[],'chat'))
        self.assertFalse(updates[0][5]['interactive'])
        self.assertFalse(updates[1][5]['interactive'])
        self.assertTrue(updates[-1][5]['interactive'])
        self.assertFalse(updates[-1][7]['visible'])
    def test_unexpected_error_unlocks_and_offers_retry(self):
        with patch.object(app,'respond',side_effect=RuntimeError('offline')):
            updates=list(app.respond_ui('你好',[],'chat'))
        self.assertTrue(updates[-1][5]['interactive'])
        self.assertTrue(updates[-1][7]['visible'])
    def test_retry_never_overwrites_draft(self):
        value,status=app.restore_chat_draft([{'role':'user','content':'原请求'}],'正在编辑')
        self.assertNotIn('value',value)
        self.assertIn('保留',status)
    def test_retry_restores_latest_user_message(self):
        value,status=app.restore_chat_draft([{'role':'user','content':'上一条'},{'role':'assistant','content':'出错'}],'')
        self.assertEqual(value,'上一条')
    def test_retry_supports_gradio_text_blocks(self):
        value,_=app.restore_chat_draft([{'role':'user','content':[{'type':'text','text':'原始请求'}]}],'')
        self.assertEqual(value,'原始请求')
    def test_interrupted_stream_marks_task_failed_and_replaces_pending_message(self):
        def stream(*args):
            yield ([{'role':'user','content':'提问'},{'role':'assistant','content':'正在生成…'}],{},'',{},'t')
            raise ConnectionError()
        with patch.object(app,'respond',side_effect=stream), patch.object(app.task_queue,'update') as update:
            updates=list(app.respond_ui('提问',[],'chat'))
        update.assert_called_once_with('t','FAILED',{'error_type':'stream_interrupted'})
        self.assertIn('请求中断',updates[-1][0][-1]['content'])
        self.assertTrue(updates[-1][5]['interactive'])
