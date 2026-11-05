"""Review probes: isolated temporary databases; no live service or production data."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
scratch = tempfile.TemporaryDirectory(prefix="lux-review-probes-")
root = Path(scratch.name)
os.environ.update(
    LUX_DATA_DIR=str(root / "data"), LUX_LOG_DIR=str(root / "logs"),
    LUX_ENV_FILE=str(root / "absent.env"),
)
import app as webapp
from core.incident_intake import IncidentIntake, Submission
from core.reward_penalty_store import RewardPenaltyStore
from tests.test_auto_incident_record import AllowGuard

webapp.guard = AllowGuard()

class Matcher:
    ruleset_version = "rules-old"
    matcher_version = "review-matcher"

    def match(self, text):
        return {"status": "candidate", "matches": [{
            "document_code": "TEST", "clause_no": "1", "score": 0.8,
            "original_text": "review fixture", "penalty": "review fixture",
            "document_title": "review fixture", "document_version": "1",
            "source_page": 1, "kind": "test", "event": "review fixture",
        }], "marker": self.ruleset_version}

class Broken(Matcher):
    def match(self, text):
        raise RuntimeError("synthetic_failure")

webapp.reward_penalty_matcher = Matcher()

def fresh(name):
    db = root / (name + ".db")
    intake = IncidentIntake(db)
    store = RewardPenaltyStore(db)
    webapp.incident_intake = intake
    webapp.reward_penalty_store = store
    intake.match_sink = webapp._sync_reward_log
    return intake, store

def emit(case, **values):
    print(json.dumps({"case": case, **values}, ensure_ascii=True))

def send(text, sid):
    hint = json.dumps({"channel": "manual", "submission_id": sid})
    return list(webapp.respond(text, [], "chat", None, None, "local", hint))

def put(intake, sid, text="北门消防通道堆放货物，已通知清理。", **kwargs):
    return intake.submit(Submission(submission_id=sid, text=text, **kwargs),
        actor="system", region="test", guard=AllowGuard(),
        ruleset_version="rules-old", matcher_version="review-matcher")

intake, store = fresh("empty-command")
send("请帮我记录：", "empty-1")
rows = intake.list_recent(username="system")
emit("empty_command", records=len(rows),
     saved_text=intake.get(rows[0]["id"])["submission"]["text_snapshot"] if rows else None)

intake, store = fresh("form-dedupe")
for _ in range(2):
    webapp.run_reward_penalty_match("北门消防通道堆放货物，已通知清理。")
emit("two_independent_form_reports", records=len(intake.list_recent(username="system")))

intake, store = fresh("correct-aba")
event = put(intake, "initial-1")["incident_id"]
for text in ("北门现场存在堆放物品。", "南门现场存在堆放物品。", "北门现场存在堆放物品。"):
    webapp.amend_incident(event, "更正", text)
row = intake.get(event)
emit("correction_A_B_A", version=row["current_version"], current_text=row["submission"]["text_snapshot"])

intake, store = fresh("stale-chat")
event = put(intake, "initial-2")["incident_id"]
intake.process_incident(event, Matcher())
with patch.object(webapp, "reward_penalty_matcher", Broken()):
    output = send("更正记录 " + event + "：南门现场情况需要重新核查。", "correction-2")
reply = output[-1][0][-1]["content"]
emit("failed_new_version_shows_old_result", current_state=intake.get(event)["match_status"],
     batch_is_stale=intake.latest_match(event)["stale"],
     reply_has_candidate="系统匹配建议" in reply, reply_has_failure="匹配失败" in reply)

intake, store = fresh("late-failure")
event = put(intake, "initial-3")["incident_id"]
old_task = intake.claim_task()
class LateFailure(Matcher):
    def match(self, text):
        put(intake, "correction-3", text="南门现场情况需要重新核查。", operation="correct", incident_id=event)
        intake.process_incident(event, Matcher())
        raise RuntimeError("old_request_finished_late")
intake.process_task(old_task, LateFailure())
emit("old_failure_overwrites_new_state", current_version=intake.get(event)["current_version"],
     current_state=intake.get(event)["match_status"], latest_batch_status=intake.latest_match(event)["status"])

intake, store = fresh("rules-history")
event = put(intake, "initial-4")["incident_id"]
intake.process_incident(event, Matcher())
new_matcher = Matcher()
new_matcher.ruleset_version = "rules-new"
intake.retry_match(event, new_matcher)
batch = intake.latest_match(event)
emit("new_rules_overwrite_old_batch", batch_count=len(intake.match_history(event)),
     stored_ruleset=batch["ruleset_version"], actual_result_ruleset=batch["result"]["marker"])

intake, store = fresh("late-reward-copy")
event = put(intake, "initial-5")["incident_id"]
delayed = []
intake.match_sink = delayed.append
intake.process_incident(event, Matcher())
put(intake, "correction-5", text="南门现场情况需要重新核查。", operation="correct", incident_id=event)
intake.match_sink = webapp._sync_reward_log
intake.process_incident(event, Matcher())
webapp._sync_reward_log(delayed[0])
emit("late_reward_copy_overwrites_new_version",
     event_text=intake.get(event)["submission"]["text_snapshot"],
     reward_text=store.find_by_incident(event)["event_description"])
