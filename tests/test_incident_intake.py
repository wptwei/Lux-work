"""统一事件受理（第一批）：来源一致、先保存后匹配、幂等、失败不丢记录。"""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
import unittest
from datetime import datetime
from pathlib import Path

from core.incident_intake import (
    IncidentIntake, IncidentWorker, Submission, SubmissionConflict, SubmissionError,
    extract_amend_request, extract_record_request, parse_hint,
)
from core.incident_store import IncidentStore
from core.reward_penalty_engine import RewardPenaltyMatcher
from core.reward_penalty_store import RewardPenaltyStore


@contextmanager
def sql(path):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SAMPLE = "一名供应商未由ICT人员陪同，在NPI区域内独自行走。"


class AllowGuard:
    def scan(self, text, **kwargs):  # noqa: ARG002
        return "allow", [], {}

    def desensitize(self, text, mapping):  # noqa: ARG002
        return text


class BlockGuard:
    def scan(self, text, **kwargs):  # noqa: ARG002
        return "block", [{"category": "高敏内容", "term": "x", "action": "block"}], {}

    def desensitize(self, text, mapping):  # noqa: ARG002
        return text


class FailingMatcher:
    ruleset_version = "rules-1"
    matcher_version = "matcher-test"

    def match(self, text):  # noqa: ARG002
        raise RuntimeError("model unavailable")


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "incidents.db"
        self.store = IncidentStore(self.db)      # 现有 baseline 先创建
        self.intake = IncidentIntake(self.db)
        self.matcher = RewardPenaltyMatcher()
        # 失败桩使用当前规则版本，模拟“规则未变、执行异常”，而不是规则变更重排。
        FailingMatcher.ruleset_version = self.matcher.ruleset_version
        FailingMatcher.matcher_version = self.matcher.matcher_version
        self.guard = AllowGuard()

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, *, text=SAMPLE, submission_id="sub-1", channel="manual", actor="alice"):
        return self.intake.submit(
            Submission(submission_id=submission_id, text=text, source_channel=channel),
            actor=actor, region="H区", guard=self.guard,
            ruleset_version=self.matcher.ruleset_version,
            matcher_version=self.matcher.matcher_version,
        )

    def counts(self):
        with sql(self.db) as conn:
            def n(table):
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            return {
                "incidents": n("incidents"),
                "submissions": n("incident_submissions"),
                "actions": n("incident_actions"),
                "tasks": n("incident_tasks"),
                "batches": n("incident_match_batches"),
            }

    # ---- 来源一致 ----
    def test_three_sources_share_one_structure(self):
        rows = []
        for index, channel in enumerate(("manual", "dictation", "voice_assistant")):
            receipt = self.submit(submission_id=f"sub-{channel}", channel=channel)
            incident = self.intake.get(receipt["incident_id"])
            sub = incident["submission"]
            rows.append({
                "receipt_keys": tuple(sorted(receipt)),
                "incident_keys": tuple(sorted(k for k in incident if k != "submission")),
                "submission_keys": tuple(sorted(sub)),
                "channel": sub["source_channel"],
                "origin": sub["source_origin"],
                "operation": sub["operation"],
                "version": receipt["version"],
                "status": receipt["status"],
                "workflow_status": receipt["workflow_status"],
                "match_status": receipt["match_status"],
            })
        for row in rows[1:]:
            self.assertEqual(row["receipt_keys"], rows[0]["receipt_keys"])
            self.assertEqual(row["incident_keys"], rows[0]["incident_keys"])
            self.assertEqual(row["submission_keys"], rows[0]["submission_keys"])
            self.assertEqual(row["operation"], "report")
            self.assertEqual(row["version"], 1)
            self.assertEqual(row["status"], "registered")
            self.assertEqual(row["workflow_status"], "RECORDED")
            self.assertEqual(row["match_status"], "pending")
        self.assertEqual([r["channel"] for r in rows], ["manual", "dictation", "voice_assistant"])
        self.assertEqual([r["origin"] for r in rows], ["typed", "asr", "asr"])

    # ---- 台账检索与分页 ----
    def test_search_filters_and_pages(self):
        for index in range(5):
            self.submit(
                text=f"供应商未陪同进入区域第{index}号，保安已到场核实。",
                submission_id=f"sub-{index}",
                channel="manual" if index < 3 else "dictation",
            )
        first = self.intake.search_recent(username="alice", page=1, page_size=2)
        self.assertEqual(5, first["total"])
        self.assertEqual(3, first["pages"])
        self.assertEqual(2, len(first["rows"]))
        last = self.intake.search_recent(username="alice", page=3, page_size=2)
        self.assertEqual(1, len(last["rows"]))
        clamped = self.intake.search_recent(username="alice", page=99, page_size=2)
        self.assertEqual(3, clamped["page"])
        keyword = self.intake.search_recent(username="alice", query="第3号")
        self.assertEqual(1, keyword["total"])
        self.assertIn("第3号", keyword["rows"][0]["text_preview"])
        manual = self.intake.search_recent(username="alice", source_channel="manual")
        self.assertEqual(3, manual["total"])
        dictated = self.intake.search_recent(username="alice", source_channel="dictation")
        self.assertEqual(2, dictated["total"])
        self.assertEqual(5, self.intake.search_recent(
            username="alice", match_status="pending")["total"])
        self.assertEqual(0, self.intake.search_recent(
            username="alice", match_status="candidate")["total"])
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(5, self.intake.search_recent(
            username="alice", date_from=today, date_to=today)["total"])
        self.assertEqual(0, self.intake.search_recent(username="bob")["total"])

    # ---- 幂等 ----
    def test_retry_returns_same_incident_without_duplicates(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(self.counts(), {
            "incidents": 1, "submissions": 1, "actions": 1, "tasks": 1, "batches": 0,
        })

    def test_same_submission_id_with_different_text_is_a_conflict(self):
        self.submit()
        with self.assertRaises(SubmissionConflict):
            self.submit(text="另一件完全不同的现场事件，内容已经改变。")
        self.assertEqual(self.counts()["incidents"], 1)
        self.assertEqual(self.counts()["submissions"], 1)

    # ---- 先保存后匹配 ----
    def test_matcher_failure_keeps_event_and_retries(self):
        receipt = self.submit()
        worker = IncidentWorker(self.intake, FailingMatcher())
        task = self.intake.claim_task()
        status = self.intake.process_task(task, FailingMatcher())
        self.assertEqual(status, "retry")
        incident = self.intake.get(receipt["incident_id"])
        self.assertIsNotNone(incident)
        self.assertEqual(incident["match_status"], "failed")
        with sql(self.db) as conn:
            row = conn.execute(
                "SELECT status, attempts, last_error_category FROM incident_tasks"
            ).fetchone()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], "RuntimeError")
        # 事件与来源仍在，回执可查。
        self.assertIsNotNone(self.intake.receipt(receipt["incident_id"], actor="alice"))
        self.assertIsNotNone(worker)

    def test_matcher_success_persists_batch_and_is_idempotent(self):
        receipt = self.submit()
        self.intake.process_incident(receipt["incident_id"], self.matcher)
        incident = self.intake.get(receipt["incident_id"])
        self.assertEqual(incident["match_status"], "candidate")
        batch = self.intake.latest_match(receipt["incident_id"])
        self.assertEqual(batch["status"], "candidate")
        self.assertGreater(batch["candidates_count"], 0)
        self.assertTrue(batch["result"]["matches"])
        # 再次领取/执行同一版本：批次不重复，时间线不重复。
        with sql(self.db) as conn:
            conn.execute(
                "UPDATE incident_tasks SET status='pending', next_attempt_at=? WHERE incident_id=?",
                (datetime.now().isoformat(timespec="seconds"), receipt["incident_id"]),
            )
        self.intake.process_incident(receipt["incident_id"], self.matcher)
        with sql(self.db) as conn:
            batches = conn.execute(
                "SELECT COUNT(*) FROM incident_match_batches WHERE incident_id=?",
                (receipt["incident_id"],),
            ).fetchone()[0]
            actions = conn.execute(
                "SELECT COUNT(*) FROM incident_actions WHERE incident_id=? AND action='奖惩匹配完成'",
                (receipt["incident_id"],),
            ).fetchone()[0]
        self.assertEqual(batches, 1)
        self.assertEqual(actions, 1)

    # ---- 事务原子性 ----
    def test_mid_transaction_failure_leaves_no_half_records(self):
        with sql(self.db) as conn:
            conn.execute(
                "CREATE TRIGGER fail_task BEFORE INSERT ON incident_tasks "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END;"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit()
        self.assertEqual(self.counts()["incidents"], 0)
        self.assertEqual(self.counts()["submissions"], 0)
        self.assertEqual(self.counts()["actions"], 0)
        # 触发器移除后可以正常登记，说明没有残留写锁或半套记录。
        with sql(self.db) as conn:
            conn.execute("DROP TRIGGER fail_task")
        receipt = self.submit()
        self.assertTrue(receipt["incident_id"].startswith("INC-"))

    # ---- 安全与权限 ----
    def test_redline_block_persists_nothing(self):
        with self.assertRaises(SubmissionError) as ctx:
            self.intake.submit(
                Submission(submission_id="blocked-1", text=SAMPLE, source_channel="dictation"),
                actor="alice", region="H区", guard=BlockGuard(),
            )
        self.assertEqual(ctx.exception.code, "redline_blocked")
        self.assertEqual(self.counts()["incidents"], 0)
        self.assertEqual(self.counts()["submissions"], 0)

    def test_other_user_cannot_read_receipt(self):
        receipt = self.submit(actor="alice")
        self.assertIsNotNone(self.intake.receipt(receipt["incident_id"], actor="alice"))
        self.assertIsNone(self.intake.receipt(receipt["incident_id"], actor="bob"))
        self.assertIsNotNone(self.intake.receipt(receipt["incident_id"], actor="bob", can_view_all=True))

    def test_short_or_empty_text_is_not_registered(self):
        for text in ("", "有人", "帮我记录"):
            with self.assertRaises(SubmissionError):
                self.submit(text=text, submission_id=f"s-{len(text)}")
        self.assertEqual(self.counts()["incidents"], 0)

    # ---- 口令只识别明确登记意图 ----
    def test_record_command_detection_is_conservative(self):
        self.assertEqual(extract_record_request("今天天气怎么样")[0], False)
        self.assertEqual(extract_record_request("记录一下这份会议纪要的要点")[0], False)
        self.assertEqual(extract_record_request("供应商违规怎么处罚？")[0], False)
        ok, body = extract_record_request("帮我记录：14:20 H1 入口有一名供应商未登记进入管制区")
        self.assertTrue(ok)
        self.assertTrue(body.startswith("14:20"))
        self.assertFalse(parse_hint('{"channel": "admin"}'))
        self.assertEqual(parse_hint('{"channel": "dictation", "edited": true}')["channel"], "dictation")

    # ---- 租约恢复 ----
    def test_expired_lease_is_reclaimed(self):
        self.submit()
        first = self.intake.claim_task(worker_id="w1", lease_seconds=1)
        self.assertIsNotNone(first)
        self.assertIsNone(self.intake.claim_task(worker_id="w2"))
        with sql(self.db) as conn:
            conn.execute(
                "UPDATE incident_tasks SET lease_expires=? WHERE id=?",
                ("2000-01-01T00:00:00", first["id"]),
            )
        again = self.intake.claim_task(worker_id="w2")
        self.assertIsNotNone(again)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["attempts"], 2)

    # ---- 版本保护与关联 ----
    def test_attach_analysis_only_updates_matching_version(self):
        receipt = self.submit()
        ok = self.intake.attach_analysis(
            receipt["incident_id"], expected_version=1, event_type="门禁异常",
            region="H区", reported_status="已控制", risk_level="high", risk_score=80,
            policy_matches=[], source_references=[], missing_info=[], policy_confidence=0.6,
            actor="supervisor",
        )
        self.assertTrue(ok)
        self.assertEqual(self.intake.get(receipt["incident_id"])["event_type"], "门禁异常")
        stale = self.intake.attach_analysis(
            receipt["incident_id"], expected_version=99, event_type="其他",
            region="H区", reported_status="已控制", risk_level="low", risk_score=1,
            actor="supervisor",
        )
        self.assertFalse(stale)
        self.assertEqual(self.intake.get(receipt["incident_id"])["event_type"], "门禁异常")

    def test_reward_store_links_to_incident(self):
        receipt = self.submit()
        self.intake.process_incident(receipt["incident_id"], self.matcher)
        batch = self.intake.latest_match(receipt["incident_id"])
        store = RewardPenaltyStore(self.db)
        saved = store.create(
            created_by="alice", event_description=SAMPLE,
            result=batch["result"], incident_id=receipt["incident_id"],
            match_batch_id=batch["id"],
        )
        self.assertEqual(saved["incident_id"], receipt["incident_id"])
        found = store.find_by_incident(receipt["incident_id"])
        self.assertEqual(found["id"], saved["id"])


    # ---- 自动匹配结果字段 ----
    def test_match_result_includes_basis_page_and_missing_conditions(self):
        result = self.matcher.match(SAMPLE)
        self.assertEqual(result["status"], "candidate")
        item = result["matches"][0]
        self.assertIn("本地规则", item["basis"])
        self.assertTrue(item["source_page_label"].startswith("第"))
        self.assertIsInstance(item["missing_conditions"], list)
        self.assertIn("发生时间未提供", item["missing_conditions"])
        health = self.matcher.health()
        self.assertGreater(health["rules_loaded"], 0)
        self.assertEqual(0, health["rules_rejected"])

    def test_rules_missing_is_not_reported_as_no_match(self):
        with tempfile.TemporaryDirectory() as directory:
            rules = Path(directory) / "rules.json"
            rules.write_text('{"rules": []}', encoding="utf-8")
            matcher = RewardPenaltyMatcher(rules)
            self.assertEqual(matcher.match(SAMPLE)["status"], "rules_missing")
            receipt = self.submit(submission_id="rules-missing-1")
            self.intake.process_incident(receipt["incident_id"], matcher)
            incident = self.intake.get(receipt["incident_id"])
            self.assertEqual(incident["match_status"], "rules_missing")

    # ---- 补充 / 更正版本 ----
    def supplement(self, receipt, *, text, submission_id="sup-1", actor="alice", operation="supplement"):
        return self.intake.submit(
            Submission(
                submission_id=submission_id, text=text, operation=operation,
                incident_id=receipt["incident_id"], source_channel="manual",
            ),
            actor=actor, region="H区", guard=self.guard,
            ruleset_version=self.matcher.ruleset_version,
            matcher_version=self.matcher.matcher_version,
        )

    def test_supplement_keeps_old_result_and_rematches_new_version(self):
        receipt = self.submit()
        self.intake.process_incident(receipt["incident_id"], self.matcher)
        old_batch = self.intake.latest_match(receipt["incident_id"])
        updated = self.supplement(
            receipt, text="保安已到场劝阻，人员未进入管制区。", submission_id="sup-1",
        )
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["operation"], "supplement")
        incident = self.intake.get(receipt["incident_id"])
        self.assertEqual(incident["current_version"], 2)
        self.assertEqual(incident["match_status"], "pending")
        self.assertEqual(incident["submission"]["incident_version"], 2)
        self.assertIn("【补充】", incident["submission"]["text_snapshot"])
        self.assertIn(SAMPLE[:8], incident["submission"]["text_snapshot"])
        self.intake.process_incident(receipt["incident_id"], self.matcher)
        history = self.intake.match_history(receipt["incident_id"])
        self.assertEqual(2, len(history))
        self.assertEqual(old_batch["id"], history[0]["id"])
        self.assertTrue(history[0]["stale"])
        self.assertFalse(history[1]["stale"])
        with sql(self.db) as conn:
            superseded = conn.execute(
                "SELECT version, superseded FROM incident_versions WHERE incident_id=? ORDER BY version",
                (receipt["incident_id"],),
            ).fetchall()
            actions = conn.execute(
                "SELECT action FROM incident_actions WHERE incident_id=? ORDER BY id",
                (receipt["incident_id"],),
            ).fetchall()
        self.assertEqual([(1, 1), (2, 0)], [tuple(row) for row in superseded])
        self.assertIn("补充登记", [row[0] for row in actions])
        self.assertEqual(2, sum(1 for row in actions if row[0] == "奖惩匹配完成"))

    def test_correct_replaces_current_text_and_old_batch_expires(self):
        receipt = self.submit()
        self.intake.process_incident(receipt["incident_id"], self.matcher)
        corrected_text = "更正：供应商在NPI区域内无人陪同独自游走。"
        updated = self.supplement(
            receipt, text=corrected_text, submission_id="cor-1", operation="correct",
        )
        self.assertEqual(updated["version"], 2)
        incident = self.intake.get(receipt["incident_id"])
        self.assertEqual(corrected_text, incident["submission"]["text_snapshot"])
        self.assertNotIn("【补充】", incident["submission"]["text_snapshot"])
        self.intake.process_incident(receipt["incident_id"], self.matcher)
        history = self.intake.match_history(receipt["incident_id"])
        self.assertTrue(history[0]["stale"])
        self.assertFalse(history[1]["stale"])
        with sql(self.db) as conn:
            action = conn.execute(
                "SELECT note FROM incident_actions WHERE incident_id=? AND action='更正登记'",
                (receipt["incident_id"],),
            ).fetchone()
        self.assertIn("v2", action[0])

    def test_supplement_retry_is_idempotent_and_other_user_is_forbidden(self):
        receipt = self.submit()
        first = self.supplement(receipt, text="补充：保安已到场核实并登记。", submission_id="sup-x")
        second = self.supplement(receipt, text="补充：保安已到场核实并登记。", submission_id="sup-x")
        self.assertEqual(first["version"], second["version"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(2, len(self.intake.submissions(receipt["incident_id"])))
        with self.assertRaises(SubmissionError) as ctx:
            self.supplement(receipt, text="补充：试图修改他人的记录。", submission_id="sup-y", actor="bob")
        self.assertEqual(ctx.exception.code, "forbidden")
        self.assertEqual(2, len(self.intake.submissions(receipt["incident_id"])))

    def test_amendment_command_requires_real_incident_id(self):
        parsed = extract_amend_request("更正记录 INC-20261104-ABC123：供应商已由ICT人员陪同离开。")
        self.assertEqual(parsed["operation"], "correct")
        self.assertEqual(parsed["incident_id"], "INC-20261104-ABC123")
        self.assertIn("陪同", parsed["text"])
        self.assertIsNone(extract_amend_request("补充记录：今天还有一条情况。"))
        self.assertIsNone(extract_amend_request("供应商违规怎么处罚？"))

    def test_retry_match_requeues_dead_task(self):
        receipt = self.submit()
        for _ in range(5):
            task = self.intake.claim_task()
            if task is None:
                break
            self.intake.process_task(task, FailingMatcher())
        incident = self.intake.get(receipt["incident_id"])
        self.assertEqual(incident["match_status"], "failed")
        self.assertEqual(1, self.intake.retry_match(receipt["incident_id"], self.matcher))
        incident = self.intake.get(receipt["incident_id"])
        self.assertEqual(incident["match_status"], "candidate")

    def test_detail_is_permission_scoped(self):
        receipt = self.submit(actor="alice")
        self.assertIsNotNone(self.intake.detail(receipt["incident_id"], actor="alice"))
        self.assertIsNone(self.intake.detail(receipt["incident_id"], actor="bob"))
        self.assertIsNotNone(
            self.intake.detail(receipt["incident_id"], actor="bob", can_view_all=True)
        )


if __name__ == "__main__":
    unittest.main()
