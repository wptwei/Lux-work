"""奖惩匹配精简流程，均使用虚构现场描述验证，不进行实际奖惩决定。"""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.reward_penalty_engine import RewardPenaltyMatcher
from core.incident_intake import IncidentIntake
from core.reward_penalty_store import RewardPenaltyStore


class RewardPenaltyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher=RewardPenaltyMatcher()

    def test_supplier_wandering_uses_supplier_penalty_clause(self):
        response=self.matcher.match('供应商在NPI区域内无人陪同独自游走')
        self.assertEqual('candidate',response['status'])
        self.assertEqual('6.5.1.2.4',response['matches'][0]['clause_no'])
        self.assertEqual('HD-KS-SW-010',response['matches'][0]['document_code'])

    def test_supplier_tailgating_distinguishes_blocked_from_entered(self):
        entered=self.matcher.match('供应商尾随进入未授权的NPI区域')
        stopped=self.matcher.match('供应商尾随被警卫拦截')
        self.assertEqual('6.5.1.3.4',entered['matches'][0]['clause_no'])
        self.assertEqual('6.5.1.2.6',stopped['matches'][0]['clause_no'])

    def test_reported_camera_fault_may_match_reward(self):
        found=self.matcher.match('员工发现摄像头偏移后及时向安保报告')
        self.assertEqual('奖励',found['matches'][0]['kind'])
        self.assertEqual('6.5.6.1.5',found['matches'][0]['clause_no'])

    def test_no_fabricated_penalty_for_registration_only(self):
        for event in ('供应商未登记准备入场','供应商报告无人车故障','今天没有异常'):
            with self.subTest(event=event):
                response=self.matcher.match(event)
                self.assertEqual('no_match',response['status'])
                self.assertEqual([],response['matches'])

    def test_store_creates_record_and_isolates_users(self):
        with tempfile.TemporaryDirectory() as directory:
            store=RewardPenaltyStore(Path(directory)/'incidents.db')
            a=self.matcher.match('供应商在NPI区域内睡觉')
            saved=store.create(created_by='alice',event_description='供应商在NPI区域内睡觉',result=a)
            store.create(created_by='bob',event_description='员工发现摄像头偏移后及时上报',result=a)
            self.assertTrue(saved['id'].startswith('RP-'))
            self.assertEqual(1,len(store.list_recent(username='alice')))
            self.assertEqual(2,len(store.list_recent(username='alice',all_users=True)))

    def test_app_callback_persists_one_match_and_updates_table(self):
        import app
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory)/'incidents.db'
            test_store=RewardPenaltyStore(db)
            test_intake=IncidentIntake(db)
            with patch.object(app,'reward_penalty_store',test_store),                  patch.object(app,'incident_intake',test_intake):
                html,table=app.run_reward_penalty_match('员工发现摄像头偏移后及时向安保报告')
                self.assertIn('6.5.6.1.5',html)
                rows=test_store.list_recent(username='system',all_users=True)
                self.assertEqual(1,len(rows))
                incident_id=rows[0]['incident_id']
                self.assertTrue(incident_id.startswith('INC-'))
                self.assertIn(incident_id,html)
                self.assertIn(incident_id,table)
                self.assertIn('员工发现摄像头',table)
                self.assertIsNotNone(test_intake.get(incident_id))


if __name__=='__main__':unittest.main()
