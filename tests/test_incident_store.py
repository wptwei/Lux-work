from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from core.incident_store import IncidentStore


class IncidentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = IncidentStore(Path(self.tmp.name) / "incidents.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self):
        return self.store.create(
            task_id="task-1",
            event_type="门禁异常",
            region="H区",
            reported_status="事发中",
            risk_level="high",
            risk_score=7,
            source_text="这段事件原文只用于生成摘要",
            created_by="zhangsan",
        )

    def test_case_lifecycle_requires_valid_transitions(self):
        item = self._create()
        self.assertEqual("PENDING_CONFIRMATION", item["workflow_status"])
        item = self.store.transition(item["id"], "确认处置", actor="boss", owner="当班班长")
        self.assertEqual("IN_PROGRESS", item["workflow_status"])
        item = self.store.transition(item["id"], "确认结案", actor="boss", note="现场已恢复并完成核查")
        self.assertEqual("CLOSED", item["workflow_status"])
        self.assertEqual(2, len(self.store.actions(item["id"])))

    def test_return_and_resubmit_flow(self):
        item = self._create()
        item = self.store.transition(item["id"], "退回补充", actor="boss", note="缺少发生时间")
        self.assertEqual("NEEDS_INFO", item["workflow_status"])
        item = self.store.transition(item["id"], "重新提交", actor="zhangsan")
        self.assertEqual("PENDING_CONFIRMATION", item["workflow_status"])

    def test_close_requires_note_and_does_not_store_source_plaintext(self):
        item = self._create()
        stored = self.store.get(item["id"])
        self.assertNotIn("这段事件原文", str(stored))
        self.store.transition(item["id"], "确认处置", actor="boss", owner="当班班长")
        with self.assertRaises(ValueError):
            self.store.transition(item["id"], "确认结案", actor="boss", note="")

    def test_user_listing_is_isolated(self):
        self._create()
        self.assertEqual(1, len(self.store.list_recent(username="zhangsan")))
        self.assertEqual(0, len(self.store.list_recent(username="lisi")))
        self.assertEqual(1, len(self.store.list_recent(all_users=True)))

    def test_policy_match_and_urgency_are_written_to_incident_ledger(self):
        item = self.store.create(
            task_id="task-policy",
            event_type="供应商入场异常",
            region="H区",
            reported_status="事发中",
            risk_level="medium",
            risk_score=4,
            source_text="事件原文不应落库",
            created_by="zhangsan",
            urgency_level="urgent",
            urgency_score=4,
            extracted_facts={"subject_types": ["supplier"], "description": "事件原文不应落库"},
            policy_matches=[{"document_code": "TEST", "clause_no": "T-1"}],
            source_references=[{"document_code": "TEST", "clause_no": "T-1"}],
            missing_info=["审批状态"],
            policy_confidence=0.72,
            manual_review=True,
        )
        self.assertEqual("urgent", item["urgency_level"])
        self.assertEqual(4, item["urgency_score"])
        self.assertAlmostEqual(0.72, item["policy_confidence"])
        self.assertIn("TEST", item["source_references_json"])
        self.assertNotIn("事件原文不应落库", str(item))
        self.assertNotIn("description", item["extracted_facts_json"])

    def test_legacy_incident_database_is_migrated_in_place(self):
        legacy = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy)
        try:
            conn.execute(
                """
                CREATE TABLE incidents(
                    id TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL, event_type TEXT NOT NULL,
                    region TEXT NOT NULL, reported_status TEXT NOT NULL, risk_level TEXT NOT NULL,
                    risk_score INTEGER NOT NULL, workflow_status TEXT NOT NULL, owner TEXT DEFAULT '',
                    source_sha256_12 TEXT NOT NULL, source_chars INTEGER NOT NULL, created_by TEXT NOT NULL,
                    confirmed_by TEXT DEFAULT '', decision_note TEXT DEFAULT '', due_at TEXT DEFAULT '',
                    created TEXT NOT NULL, updated TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        migrated = IncidentStore(legacy)
        item = migrated.create(
            task_id="legacy-task", event_type="其他", region="H区", reported_status="已控制",
            risk_level="low", risk_score=0, source_text="legacy", created_by="zhangsan",
            urgency_level="routine",
        )
        self.assertEqual("routine", item["urgency_level"])
        self.assertIn("source_references_json", item)


if __name__ == "__main__":
    unittest.main()
