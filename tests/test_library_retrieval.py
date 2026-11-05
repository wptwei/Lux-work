import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tests.test_pdf_library import VIEWER, OWNER, OTHER, pdf_bytes
from core.pdf_library import PdfLibrary
from core.library_retrieval import retrieve_files, should_search_library, candidates_for


class SemanticRetrievalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = PdfLibrary(self.root / 'library')
        self.pdf = self.root / 'HD-KS-SW-008 安保人员管理考核办法.pdf'
        self.pdf.write_bytes(pdf_bytes())
        self.record = self.store.upload(self.pdf, OWNER)

    def test_semantic_selection_without_shared_filename_phrase(self):
        def select(prompt, system):
            data = json.loads(prompt)
            self.assertEqual(data['request'], '帮我找一下员工评分的规定')
            self.assertIn('考核办法', data['candidates'][0]['title'])
            self.assertNotIn('Inspection rules', prompt)
            return '{"intent":"retrieve","ids":[1]}'
        model = Mock(side_effect=select)
        reply = retrieve_files('帮我找一下员工评分的规定', self.store, VIEWER, model)
        self.assertIn(self.record['version_id'], reply)
        self.assertIn('可能相关', reply)
        model.assert_called_once()

    def test_greeting_and_partial_name_skip_model(self):
        model = Mock(side_effect=AssertionError('exact lookup needs no model'))
        reply = retrieve_files('你好 帮我找一下安保人员管理考核办法', self.store, VIEWER, model)
        self.assertIn(self.record['version_id'], reply)
        model.assert_not_called()

    def test_unknown_or_malformed_ids_never_become_links(self):
        for raw in ('{}', 'not json', '[]', '{"intent":"retrieve","ids":[99]}',
                    '{"intent":"retrieve","ids":[true]}',
                    '{"intent":"retrieve","ids":["https://fake.example"]}'):
            with self.subTest(raw=raw):
                reply = retrieve_files('找文件 员工评分的规定', self.store, VIEWER, lambda *a: raw)
                self.assertIn('暂时不可用', reply)
                self.assertNotIn('library-files/', reply)

    def test_private_and_deleted_metadata_not_sent_to_model(self):
        private = self.root / '绝密隐藏文件.pdf'; private.write_bytes(pdf_bytes())
        self.store.upload(private, OTHER, visibility='private')
        removed = self.root / '已删除文件.pdf'; removed.write_bytes(pdf_bytes())
        rec = self.store.upload(removed, OWNER)
        self.store.set_deleted(rec['document_id'], OWNER, True)
        def select(prompt, system):
            self.assertNotIn('绝密隐藏', prompt)
            self.assertNotIn('已删除文件', prompt)
            return '{"intent":"retrieve","ids":[1]}'
        self.assertIn(self.record['version_id'], retrieve_files('找文件 员工评分', self.store, VIEWER, select))

    def test_revalidates_deleted_results_after_model_returns(self):
        def select(*args):
            self.store.set_deleted(self.record['document_id'], OWNER, True)
            return '{"intent":"retrieve","ids":[1]}'
        reply = retrieve_files('找文件 员工评分', self.store, VIEWER, select)
        self.assertNotIn('library-files/', reply)
        self.assertIn('已变更', reply)

    def test_writing_intent_returns_to_normal_chat(self):
        reply = retrieve_files('帮我写一份员工评分制度', self.store, VIEWER,
                               lambda *a: '{"intent":"other","ids":[]}')
        self.assertIsNone(reply)

    def test_model_unavailable_does_not_claim_semantic_success(self):
        model = Mock(side_effect=TimeoutError())
        reply = retrieve_files('找文件 员工评分', self.store, VIEWER, model)
        self.assertIn('智能匹配暂时不可用', reply)
        self.assertNotIn('已尝试智能匹配', reply)

    def test_no_semantic_match_does_not_invent_file(self):
        reply = retrieve_files('找文件 太空旅行', self.store, VIEWER,
                               lambda *a: '{"intent":"retrieve","ids":[]}')
        self.assertIn('暂未找到', reply)
        self.assertNotIn('library-files/', reply)

    def test_catalog_ranking_checks_older_items_and_bounds_prompt(self):
        rows = [{'title': '无关设备手册' + str(i), 'category': '通用', 'tags': ''} for i in range(120)]
        rows.append({'title': '人员考核办法', 'category': '人员', 'tags': '员工评分'})
        selected, entries = candidates_for('员工评分', rows)
        self.assertEqual(selected[0]['title'], '人员考核办法')
        self.assertLessEqual(len(entries), 20)
        self.assertLessEqual(sum(len(json.dumps(e, ensure_ascii=False)) for e in entries), 2600)

    def test_intent_gate_includes_greeting_and_description(self):
        for text in ('你好 帮我找一下安保体系文件管理规范', '有没有管员工评分的规定', '文件怎么归档的制度'):
            self.assertTrue(should_search_library(text))
        self.assertFalse(should_search_library('你好'))

    def test_my_uploads_exclude_other_owners_without_model_guessing(self):
        other = self.root / '另一个用户的项目报告.pdf'
        other.write_bytes(pdf_bytes())
        record = self.store.upload(other, OTHER)
        model = Mock(side_effect=AssertionError('ownership is not a model decision'))
        reply = retrieve_files('找出我之前上传的资料', self.store, OWNER, model)
        self.assertIn(self.record['version_id'], reply)
        self.assertNotIn(record['version_id'], reply)
        self.assertIn('当前账号', reply)
        model.assert_not_called()

    def test_specific_own_upload_request_keeps_topic_and_ownership(self):
        other=self.root/'其他用户项目报告.pdf';other.write_bytes(pdf_bytes())
        other_record=self.store.upload(other,OTHER)
        def select(prompt, system):
            self.assertNotIn('其他用户项目报告',prompt)
            self.assertIn('考核',prompt)
            return '{"intent":"retrieve","ids":[1]}'
        reply=retrieve_files('找出我之前上传的考核资料',self.store,OWNER,select)
        self.assertIn(self.record['version_id'],reply)
        self.assertNotIn(other_record['version_id'],reply)
