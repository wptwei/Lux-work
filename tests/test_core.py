from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

# 导出会启动 PowerShell 调 PowerPoint 逐页渲染；单元测试不需要，装没装 Office 都不该等它。
os.environ.setdefault("LUX_SKIP_PPT_PREVIEW", "1")

from core.db import PromptDB
from core.file_reader import read_file
from core.ingest import ingest_document
from core.image_classifier import classify_image
from core.review_queue import ReviewQueue
from core.ppt_export import parse_report
from core.redline_guard import RedlineGuard
from core.task_queue import TaskQueue


ROOT = Path(__file__).resolve().parents[1]


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.guard = RedlineGuard(log_dir=Path(self.tmp.name) / "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_block_identity_number_without_calling_model(self):
        called = False

        def fake(prompt, system=""):
            nonlocal called
            called = True
            return "不应调用"

        result = self.guard.call_llm("身份证号 340200199001011234", llm_fn=fake)
        self.assertEqual("rejected", result["status"])
        self.assertFalse(called)

    def test_block_identity_with_separators(self):
        result = self.guard.call_llm(
            "身份证 340200 19900101 1234 请处理",
            llm_fn=lambda prompt, system="": "不应调用",
        )
        self.assertEqual("rejected", result["status"])

    def test_external_masks_and_restores(self):
        seen = {}

        def fake(prompt, system=""):
            seen["prompt"] = prompt
            return prompt

        result = self.guard.call_llm(
            "工号12345678在H区巡检", llm_fn=fake, external=True
        )
        self.assertNotIn("12345678", seen["prompt"])
        self.assertNotIn("H区", seen["prompt"])
        self.assertIn("12345678", result["output"])
        self.assertTrue(result["masked"])

    def test_local_keeps_desensitize_level_data_local(self):
        seen = {}

        def fake(prompt, system=""):
            seen["prompt"] = prompt
            return "完成"

        result = self.guard.call_llm(
            "工号12345678在H区巡检", llm_fn=fake, external=False
        )
        self.assertIn("12345678", seen["prompt"])
        self.assertFalse(result["masked"])

    def test_audit_log_has_no_plaintext(self):
        self.guard.call_llm("普通测试文本", llm_fn=lambda prompt, system="": "处理完成")
        content = next((Path(self.tmp.name) / "logs").glob("*.jsonl")).read_text("utf-8")
        self.assertNotIn("普通测试文本", content)
        self.assertNotIn("处理完成", content)
        entry = json.loads(content)
        self.assertIn("prompt_sha256_12", entry)

    def test_stream_blocks_before_calling_model(self):
        def forbidden(prompt, system=""):
            raise AssertionError("红线命中时不应调用模型")
            yield  # pragma: no cover

        events = list(
            self.guard.call_llm_stream("身份证号 340200199001011234", llm_stream=forbidden)
        )
        self.assertEqual("final", events[-1]["type"])
        self.assertEqual("rejected", events[-1]["result"]["status"])

    def test_stream_yields_chunks_then_final(self):
        def fake_stream(prompt, system=""):
            yield "处理"
            yield "完成"

        events = list(self.guard.call_llm_stream("普通测试文本", llm_stream=fake_stream))
        chunks = [e["text"] for e in events if e["type"] == "chunk"]
        self.assertEqual(["处理", "完成"], chunks)
        self.assertEqual("ok", events[-1]["result"]["status"])
        self.assertEqual("处理完成", events[-1]["result"]["output"])

    def test_stream_allows_discussion_of_asset_terms(self):
        def fake_stream(prompt, system=""):
            yield "正常内容"
            yield "监控画面"

        events = list(self.guard.call_llm_stream("普通测试", llm_stream=fake_stream))
        self.assertEqual("ok", events[-1]["result"]["status"])
        self.assertEqual(["正常内容", "监控画面"], [e["text"] for e in events if e["type"] == "chunk"])

    def test_plain_text_asset_discussion_is_not_blocked(self):
        level, hits, _ = self.guard.scan("请制定监控画面和厂区布局资料的管理制度")
        self.assertEqual("allow", level)
        self.assertEqual([], hits)

    def test_stream_still_blocks_personal_data_in_output(self):
        def fake_stream(prompt, system=""):
            yield "联系电话："
            yield "13812345678"

        events = list(self.guard.call_llm_stream("普通测试", llm_stream=fake_stream))
        self.assertEqual("rejected", events[-1]["result"]["status"])


class IngestTests(unittest.TestCase):
    def test_risky_filename_is_sent_to_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "门禁记录.txt"
            path.write_text("普通内容", encoding="utf-8")
            guard = RedlineGuard(log_dir=Path(tmp) / "logs")
            result = ingest_document(path, guard)
            self.assertFalse(result.ok)
            self.assertEqual("", result.text)
            self.assertEqual("review", result.level)
            self.assertIn("人工复核", result.message)

    def test_structured_roster_is_sent_to_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "人员信息.txt"
            path.write_text(
                "姓名,工号,部门,岗位\n张三,10000001,生产部,保安\n李四,10000002,行政部,主管",
                encoding="utf-8",
            )
            guard = RedlineGuard(log_dir=Path(tmp) / "logs")
            result = ingest_document(path, guard)
            self.assertFalse(result.ok)
            self.assertEqual("review", result.level)

    def test_full_file_is_scanned_before_input_is_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "长文档.txt"
            path.write_text("普通内容" * 6000 + " 13812345678", encoding="utf-8")
            guard = RedlineGuard(log_dir=Path(tmp) / "logs")
            result = ingest_document(path, guard)
            self.assertFalse(result.ok)
            self.assertEqual("block", result.level)

    def test_region_policy_can_be_stricter_for_npi(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "样机资料.txt"
            path.write_text("普通说明", encoding="utf-8")
            guard = RedlineGuard(log_dir=Path(tmp) / "logs")
            npi = ingest_document(path, guard, region="NPI区")
            office = ingest_document(path, guard, region="办公区")
            self.assertEqual("review", npi.level)
            self.assertTrue(office.ok)


class ImageClassifierTests(unittest.TestCase):
    def test_local_classifier_marks_monitoring_like_image(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "screen.png"
            Image.new("RGB", (640, 360), (10, 10, 10)).save(path)
            risk = classify_image(path, "CCTV CAM01 通道 实时预览", threshold=0.58)
            self.assertEqual("疑似监控画面", risk.label)
            self.assertGreaterEqual(risk.score, 0.58)


class ReviewQueueTests(unittest.TestCase):
    def test_submit_deduplicates_pending_and_records_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "监控截图.png"
            source.write_bytes(b"fake-image")
            queue = ReviewQueue(root / "reviews.db", root / "files")
            first = queue.submit(source, region="H区", asset_type="图片", risk_level="疑似监控画面")
            second = queue.submit(source, region="H区", asset_type="图片", risk_level="疑似监控画面")
            self.assertEqual(first, second)
            self.assertTrue(queue.decide(first, "已拒绝", "测试审核员", "属于受控材料"))
            self.assertEqual("已拒绝", queue.get(first)["status"])
            self.assertFalse(queue.decide(first, "已批准", "测试审核员"))


class DatabaseTests(unittest.TestCase):
    def test_prompt_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "prompts.db"
            db = PromptDB(db_path)
            prompt_id = db.add("测试", "整理以下内容", "通用")
            self.assertEqual("测试", db.get(prompt_id)["title"])
            conn = sqlite3.connect(db_path)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual("wal", mode.lower())


class TaskQueueTests(unittest.TestCase):
    def test_legacy_database_migrates_and_tracks_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tasks.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """CREATE TABLE tasks(
                    id TEXT PRIMARY KEY, task_type TEXT, payload TEXT, status TEXT,
                    result TEXT DEFAULT '', retry INTEGER DEFAULT 0, created TEXT)"""
                )
                conn.commit()
            finally:
                conn.close()

            queue = TaskQueue(db_path)
            task_id = queue.create(
                "chat",
                {"prompt_sha256_12": "abc123"},
                title="普通对话",
                category="通用",
                input_chars=18,
            )
            queue.update(task_id, "RUNNING")
            queue.update(task_id, "COMPLETED", {"output_chars": 60}, duration_ms=1250)
            self.assertTrue(queue.add_feedback(task_id, "需修改", 8, "格式需调整"))

            task = queue.get(task_id)
            self.assertEqual("COMPLETED", task["status"])
            self.assertEqual(1250, task["duration_ms"])
            self.assertEqual("需修改", task["feedback"])
            self.assertEqual(8, task["actual_minutes"])
            self.assertNotIn("业务原文", task["payload"])
            self.assertEqual(1, queue.summary()["feedback_count"])

    def test_rejects_invalid_status_and_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = TaskQueue(Path(tmp) / "tasks.db")
            task_id = queue.create("chat", {})
            with self.assertRaises(ValueError):
                queue.update(task_id, "UNKNOWN")
            with self.assertRaises(ValueError):
                queue.add_feedback(task_id, "一般")

    def test_awaiting_confirmation_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = TaskQueue(Path(tmp) / "tasks.db")
            task_id = queue.create("incident", {}, created_by="tester")
            queue.update(task_id, "AWAITING_CONFIRMATION")
            self.assertEqual("AWAITING_CONFIRMATION", queue.get(task_id)["status"])
            self.assertEqual(1, queue.summary("tester")["awaiting_confirmation"])
            self.assertEqual(task_id, queue.pending()[0][0])


class PptxImportTests(unittest.TestCase):
    @staticmethod
    def _make_pptx(path: Path, text: str) -> None:
        from pptx import Presentation

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(0, 0, 4000000, 1000000)
        box.text_frame.text = text
        prs.save(path)

    def test_read_pptx_extracts_slide_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "样例.pptx"
            self._make_pptx(path, "东门未锁，已上报")

            text = read_file(str(path))
            self.assertIn("第 1 页", text)
            self.assertIn("东门未锁，已上报", text)

    def test_ingest_accepts_pptx(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "周报样例.pptx"
            self._make_pptx(path, "本周巡检正常")

            guard = RedlineGuard(log_dir=Path(tmp) / "logs")
            result = ingest_document(path, guard)
            self.assertTrue(result.ok)
            self.assertIn("本周巡检正常", result.text)


class PptTests(unittest.TestCase):
    def test_parse_report_uses_declared_pages_without_filling_gaps(self):
        pages = parse_report("## P1 封面\n周报\n## P4 看板\nKey message：稳定")
        self.assertEqual(2, len(pages))
        self.assertEqual("周报", pages[0]["title"])
        self.assertEqual(4, pages[1]["no"])
        self.assertIn("Key message", pages[1]["body"])


class PptTemplateTests(unittest.TestCase):
    def test_build_from_template_uses_dynamic_page_count(self):
        from pptx import Presentation

        from core.ppt_template import build_ppt_from_template, inspect_template

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "模板.pptx"
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[0])
            slide.shapes.title.text = "示例封面"
            prs.save(template)

            report = (
                "## P1 封面\nH区第3周周报\n"
                "## P2 重点工作\n本周完成东门加固\n"
                "## P3 数据\nKey message：违禁品查获 5×"
            )
            out = Path(tmp) / "out.pptx"
            build_ppt_from_template(template, report, out)

            result = Presentation(str(out))
            self.assertEqual(3, len(result.slides))

            texts = []
            for slide in result.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        texts.append(shape.text_frame.text)
            joined = "\n".join(texts)
            self.assertIn("H区第3周周报", joined)
            self.assertIn("本周完成东门加固", joined)
            self.assertNotIn("Thankyou", joined)

            info = inspect_template(template)
            self.assertGreaterEqual(len(info["layouts"]), 1)

    def test_marker_template_replaces_in_place(self):
        from pptx import Presentation

        from core.ppt_template import build_ppt_from_template

        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "标记模板.pptx"
            prs = Presentation()
            cover = prs.slides.add_slide(prs.slide_layouts[6])
            cover.shapes.add_textbox(0, 0, 4000000, 1000000).text_frame.text = "{{标题}}"
            body = prs.slides.add_slide(prs.slide_layouts[6])
            body.shapes.add_textbox(0, 0, 4000000, 1000000).text_frame.text = "{{P1}}"
            prs.save(template)

            out = Path(tmp) / "out.pptx"
            build_ppt_from_template(template, "## P1 封面\nH区周报摘要", out)

            result = Presentation(str(out))
            self.assertEqual(2, len(result.slides))
            texts = [
                shape.text_frame.text
                for slide in result.slides
                for shape in slide.shapes
                if shape.has_text_frame
            ]
            joined = "\n".join(texts)
            self.assertNotIn("{{", joined)
            self.assertIn("H区", joined)


if __name__ == "__main__":
    unittest.main()
