from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.urgency_agent import UrgencyAgent
from core.policy_engine import IncidentFactExtractor, PolicyMatchEngine


class PolicyMatchEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture = {
            "document": {
                "code": "TEST-POLICY-001",
                "title": "测试制度（非生产条款）",
                "version": "test",
                "source_file": "tests/fixture.txt",
                "source_status": "ready",
            },
            "clauses": [
                {
                    "id": "test-supplier-entry",
                    "clause_no": "T-1",
                    "title": "测试供应商入场",
                    "text": "测试条款：供应商完成审批和登记后方可入场。",
                    "keywords": ["供应商", "入场", "审批", "登记"],
                    "semantic_terms": ["外协单位进场", "承包商进入"],
                    "required_facts": ["approved", "registered"],
                    "conditions": [
                        {"fact": "subject_types", "operator": "contains", "value": "supplier"},
                        {"fact": "activities", "operator": "contains", "value": "entry"},
                    ],
                    "questions": {
                        "approved": "该供应商是否已经完成审批？",
                        "registered": "该供应商是否已经完成登记？",
                    },
                    "actions": ["测试动作：核验审批和登记状态。"],
                }
            ],
        }
        (self.root / "test-policy.json").write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        self.engine = PolicyMatchEngine(self.root, low_confidence_threshold=0.35)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_only_explicit_facts(self):
        facts = IncidentFactExtractor().extract(
            "事件类别：门禁异常\n发生区域：H区\n现场状态：事发中\n"
            "发生时间：10:30\n事件描述：供应商准备入场，已审批但未登记"
        )
        self.assertIn("supplier", facts["subject_types"])
        self.assertIn("entry", facts["activities"])
        self.assertTrue(facts["facts"]["approved"])
        self.assertFalse(facts["facts"]["registered"])
        self.assertNotIn("escorted", facts["facts"])

    def test_keyword_semantic_retrieval_condition_and_citation(self):
        result = self.engine.match("供应商准备入场，已审批且已登记")
        self.assertEqual("matched", result["decision"])
        self.assertGreaterEqual(result["confidence"], 0.35)
        self.assertEqual("TEST-POLICY-001", result["citations"][0]["document_code"])
        self.assertEqual("T-1", result["citations"][0]["clause_no"])
        self.assertEqual(["测试动作：核验审批和登记状态。"], result["sourced_actions"])

    def test_missing_applicability_fact_triggers_targeted_questions(self):
        result = self.engine.match("外协单位准备进场")
        self.assertEqual("human_review", result["decision"])
        self.assertIn("approved", result["missing_facts"])
        self.assertIn("registered", result["missing_facts"])
        self.assertIn("该供应商是否已经完成审批？", result["follow_up_questions"])
        self.assertEqual([], result["citations"])

    def test_unrelated_event_does_not_invent_policy(self):
        result = self.engine.match("今天天气正常")
        self.assertEqual([], result["citations"])
        self.assertEqual([], result["sourced_actions"])
        self.assertTrue(result["requires_human_confirmation"])

    def test_empty_production_source_is_reported_without_fabricated_clauses(self):
        (self.root / "test-policy.json").unlink()
        empty = {
            "document": {
                "code": "HD-KS-SW-010",
                "title": "供应商安保管理办法",
                "source_status": "awaiting_source_text",
            },
            "clauses": [],
        }
        (self.root / "empty.json").write_text(json.dumps(empty, ensure_ascii=False), encoding="utf-8")
        result = PolicyMatchEngine(self.root).match("供应商未登记准备入场")
        self.assertEqual([], result["matches"])
        self.assertIn("目标制度原文尚未导入", result["manual_review_reasons"])


class UrgencyAgentTests(unittest.TestCase):
    def test_risk_and_urgency_are_independent_dimensions(self):
        agent = UrgencyAgent()
        self.assertEqual("routine", agent.score("现场状态：已控制，事后补录一起严重冲突") ["level"])
        self.assertEqual("urgent", agent.score("现场状态：事发中，普通门禁故障仍在持续") ["level"])


if __name__ == "__main__":
    unittest.main()
