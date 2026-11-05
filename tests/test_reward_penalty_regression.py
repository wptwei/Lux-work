"""Run: python -m unittest tests.test_reward_penalty_regression -v"""
import gc
import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.reward_penalty_engine import RewardPenaltyMatcher
from core.incident_intake import IncidentIntake
from core.reward_penalty_store import RewardPenaltyStore

CASES = [
    ('供应商在NPI区域内无人陪同独自游走', '6.5.1.2.4'),
    ('供应商在NPI区域内睡觉', '6.5.1.2.9'),
    ('员工发现摄像头偏移后及时向安保报告', '6.5.6.1.5'),
    ('供应商尾随进入未授权的NPI区域', '6.5.1.3.4'),
    ('供应商尾随被警卫拦截', '6.5.1.2.6'),
    ('今天上午九点二十分，安保人员在H区巡查过程中发现一名供应商在NPI区域内无人陪同独自游走，随后带到门岗核实身份并登记处理，现场未造成损失。', '6.5.1.2.4'),
    ('今天上午九点二十分，巡查发现供应商在NPI区域内睡觉，随后安排人员到场核实并登记情况，现场处理完毕。', '6.5.1.2.9'),
    ('今天上午九点二十分，员工发现摄像头偏移后及时向安保报告，随后安排人员到场核实并登记情况，现场处理完毕。', '6.5.6.1.5'),
    ('今天上午九点二十分，供应商尾随进入未授权的NPI区域，随后安排人员到场核实并登记情况，现场处理完毕。', '6.5.1.3.4'),
    ('今天上午九点二十分，供应商尾随被警卫拦截，随后安排人员到场核实并登记情况，现场处理完毕。', '6.5.1.2.6'),
    ('供应商未登记准备入场', None),
    ('供应商报告无人车故障', None),
    ('今天没有异常', None),
    ('今天上午九点二十分，供应商报告无人车故障，随后安排人员到场核实并登记情况，现场处理完毕。', None),
    ('今天上午九点二十分，供应商未登记准备入场，随后安排人员到场核实并登记情况，现场处理完毕。', None),
    ('今天上午九点二十分，天气晴朗一切正常，值班人员完成交接工作，现场处理完毕。', None),
    ('你好', 'needs_information'),
]

class MatchRegressionTests(unittest.TestCase):
    def test_representative_inputs(self):
        matcher = RewardPenaltyMatcher()
        for event, expected in CASES:
            with self.subTest(event=event):
                result = matcher.match(event)
                if expected in (None, 'needs_information'):
                    self.assertEqual(expected or 'no_match', result['status'])
                    self.assertEqual([], result['matches'])
                else:
                    self.assertEqual('candidate', result['status'])
                    self.assertEqual(expected, result['matches'][0]['clause_no'])

    def test_callback_display_and_persistence(self):
        import app
        with tempfile.TemporaryDirectory() as directory:
            try:
                db = Path(directory) / 'incidents.db'
                store = RewardPenaltyStore(db)
                intake = IncidentIntake(db)
                with patch.object(app, 'reward_penalty_store', store),                      patch.object(app, 'incident_intake', intake):
                    for event, expected in (CASES[5], CASES[7], CASES[9], CASES[13]):
                        output, table = app.run_reward_penalty_match(event)
                        self.assertIn('INC-', output)
                        self.assertIn(expected or '未找到足够可靠', output)
                        matches = app.reward_penalty_matcher.match(event)['matches']
                        for item in matches:
                            self.assertIn('条款原文：', output)
                            self.assertIn(html.escape(item['original_text']), output)
                            self.assertIn(html.escape(item['document_title']), output)
                            self.assertIn(item['clause_no'], output)
                            self.assertIn(f"第{item['source_page']}页", output)
                            self.assertIn(f"版本{item['document_version']}", output)
                        _, _, mapping = app.guard.scan(event)
                        safe_event = app.guard.desensitize(event, mapping) if mapping else event
                        self.assertIn(html.escape(safe_event[:85]), table)
                    rows = store.list_recent(username='system', all_users=True)
                    self.assertEqual(4, len(rows))
                    self.assertEqual(3, sum(row['match_status'] == 'candidate' for row in rows))
                    self.assertEqual(1, sum(row['match_status'] == 'no_match' for row in rows))
            finally:
                # sqlite3 的连接上下文只提交/回滚；回收测试连接后清理 Windows 临时文件。
                gc.collect()

if __name__ == '__main__':
    unittest.main()
