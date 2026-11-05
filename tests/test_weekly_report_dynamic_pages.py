"""Dynamic page count and historical style reference regression tests."""
import os
import tempfile
import unittest
from pathlib import Path

# 导出会启动 PowerShell 调 PowerPoint 逐页渲染；单元测试不需要，装没装 Office 都不该等它。
os.environ.setdefault("LUX_SKIP_PPT_PREVIEW", "1")

from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from core.ppt_export import build_ppt, parse_report
from core.weekly_report_agent import (
    build_weekly_analysis_prompt,
    build_weekly_docx,
    extract_weekly_facts,
    make_offline_draft,
    normalize_weekly_analysis,
    validate_weekly_report,
)


class DynamicWeeklyReportTests(unittest.TestCase):
    def test_offline_draft_can_use_four_pages_when_material_is_limited(self):
        source = "本周完成门岗巡查并记录结果\n下周计划复核重点区域"
        draft = make_offline_draft(source, "2026/10/19—2026/10/25")
        check = validate_weekly_report(draft)
        self.assertTrue(check["valid"], check)
        self.assertEqual(4, check["page_count"])
        self.assertNotIn("厂区分布&人力分布", draft)
        self.assertIn("[type:key_work]", draft)
        self.assertIn("[type:next_plan]", draft)

    def test_parser_accepts_nine_pages_and_explicit_page_types(self):
        pages = [
            ("cover", "H区安保周报"),
            ("contents", "目录"),
            ("manpower", "厂区分布&人力分布"),
            ("operations", "专区运营看板"),
            ("anomalies", "异常汇总分析"),
            ("key_work", "本周重点工作"),
            ("photo_evidence", "现场执勤影像"),
            ("next_plan", "下周重点工作计划"),
            ("closing", "Thankyou"),
        ]
        report = "\n\n".join(
            f"## P{i} [type:{kind}] | {title}\n" + ("Thankyou" if kind == "closing" else f"本页素材 {i}")
            for i, (kind, title) in enumerate(pages, 1)
        )
        checked = validate_weekly_report(report)
        self.assertTrue(checked["valid"], checked)
        self.assertEqual(9, checked["page_count"])
        self.assertEqual("photo_evidence", parse_report(report)[6]["type"])

    def test_analysis_normalizer_accepts_flexible_page_plan(self):
        facts = extract_weekly_facts("本周违禁品查获27次\n上周违禁品查获21次\n本周完成现场巡查\n下周计划复核")
        payload = {
            "page_plan": [
                {"page": 1, "page_type": "cover", "title": "H区安保周报", "fact_ids": []},
                {"page": 2, "page_type": "operations", "title": "专区运营看板", "fact_ids": ["F001", "F002"]},
                {"page": 3, "page_type": "key_work", "title": "本周重点工作", "fact_ids": ["F003"]},
                {"page": 4, "page_type": "next_plan", "title": "下周计划", "fact_ids": ["F004"]},
                {"page": 5, "page_type": "closing", "title": "Thankyou", "fact_ids": []},
            ]
        }
        normalized, errors = normalize_weekly_analysis(payload, facts)
        self.assertEqual([], errors)
        self.assertEqual(5, len(normalized["page_plan"]))
        self.assertEqual("operations", normalized["page_plan"][1]["page_type"])

    def test_reference_ppt_rebuilds_dynamic_pages_without_old_data(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            bg = folder / "cover.png"
            Image.new("RGB", (1280, 720), (30, 60, 110)).save(bg)
            template = folder / "reference.pptx"
            prs = Presentation()
            prs.slide_width = Inches(13.333)
            prs.slide_height = Inches(7.5)
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            slide.shapes.add_picture(str(bg), 0, 0, width=prs.slide_width, height=prs.slide_height)
            old = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
            old.text_frame.text = "旧周报标题 2026/9/12"
            second = prs.slides.add_slide(prs.slide_layouts[6])
            stale = second.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
            stale.text_frame.text = "上期违禁品 27 次"
            prs.save(template)

            report = """## P1 [type:cover] | H区安保周报
统计周期：2026/10/19—2026/10/25

## P2 [type:operations] | 专区运营看板
本周违禁品查获 5 次
图表数据：
类别|本周|上周
违禁品|5|7
异常事件|2|4
Key message：本周提供了可比数据。

## P3 [type:key_work] | 本周重点工作
10/20日，完成现场巡查并记录问题

## P4 [type:next_plan] | 下周重点工作计划
下周计划复核现场问题

## P5 [type:closing] | Thankyou
Thankyou"""
            output = folder / "weekly.pptx"
            build_ppt(report, output, reference_template=template)
            result = Presentation(output)
            self.assertEqual(5, len(result.slides))
            self.assertTrue(any(shape.shape_type == 13 for shape in result.slides[0].shapes))
            all_text = "\n".join(
                shape.text for slide in result.slides for shape in slide.shapes if shape.has_text_frame
            )
            self.assertIn("H区安保周报", all_text)
            self.assertNotIn("旧周报标题", all_text)
            self.assertNotIn("上期违禁品 27 次", all_text)
            self.assertTrue(any(shape.has_chart for shape in result.slides[1].shapes))
            self.assertIn("04 / 05", all_text)

    def test_default_ppt_uses_builtin_historical_cover_asset(self):
        report = """## P1 [type:cover] | H区安保周报
统计周期：本周

## P2 [type:key_work] | 本周重点工作
完成巡查

## P3 [type:closing] | Thankyou
Thankyou"""
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "default.pptx"
            build_ppt(report, out)
            result = Presentation(out)
            self.assertEqual(3, len(result.slides))
            self.assertTrue(any(shape.shape_type == 13 for shape in result.slides[0].shapes))

    def test_analysis_prompt_allows_creative_page_count(self):
        facts = extract_weekly_facts("本周完成门岗巡查")
        prompt = build_weekly_analysis_prompt(facts)
        self.assertIn("按本期素材覆盖面", prompt)
        self.assertIn("photo_evidence", prompt)
        self.assertNotIn("必须且只能覆盖 page 1 到 8", prompt)

    def test_word_export_uses_dynamic_page_count(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as folder:
            report = """## P1 [type:cover] | H区安保周报
统计周期：本周

## P2 [type:key_work] | 本周重点工作
完成巡查

## P3 [type:next_plan] | 下周计划
复核隐患

## P4 [type:closing] | Thankyou
Thankyou"""
            out = build_weekly_docx(report, Path(folder) / "dynamic.docx")
            doc = Document(out)
            text = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("本周重点工作", text)
            self.assertIn("下周计划", text)
            self.assertIn("Thankyou", text)


if __name__ == "__main__":
    unittest.main()
