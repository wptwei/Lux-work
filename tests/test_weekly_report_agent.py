"""周报及文档 Agent：事实分类、AI 语义分析、证据编译、质量闸门与真实文件导出。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# 导出会启动 PowerShell 调 PowerPoint 逐页渲染；单元测试不需要，装没装 Office 都不该等它。
os.environ.setdefault("LUX_SKIP_PPT_PREVIEW", "1")

import app as webapp
from core.deliverable_store import DeliverableStore
from core.ppt_export import build_ppt
from core.weekly_report_agent import (
    build_weekly_docx, make_offline_draft, ppt_content_issues, validate_weekly_report,
    extract_weekly_facts, make_offline_analysis, normalize_weekly_analysis,
    validate_report_grounding, compile_grounded_weekly_page, _chart_data_block_issues,
)


SOURCE = (
    "10/18日，安保团队完成重点区域安全巡检并记录隐患\n"
    "本周在岗人员 22 人\n供应商无人陪同进入保密区域，发现异常 2 次\n"
    "违禁品查获 3 次\n下周计划推进安保巡逻复盘"
)

CLEAN_REPORT = "\n\n".join([
    "## P1 [type:cover] | H区安保周报\n统计周期：2026/10/12—2026/10/18",
    "## P2 [type:contents] | 目录\n01 厂区分布与人力\n02 运营与异常\n03 本周重点工作\n04 下周计划",
    "## P3 [type:manpower] | 厂区分布与人力\n本周在岗人员 22 人。",
    "## P4 [type:operations] | 专区运营看板\n违禁品查获 3 次。",
    "## P5 [type:anomalies] | 异常汇总分析\n供应商无人陪同进入保密区域，发现异常 2 次。",
    "## P6 [type:key_work] | 本周重点工作\n10/18日，安保团队完成重点区域安全巡检并记录隐患。",
    "## P7 [type:next_plan] | 下周重点工作计划\n推进安保巡逻复盘。",
    "## P8 [type:closing] | Thankyou\n华东安保二部 · H区",
])


class WeeklyAgentTests(unittest.TestCase):
    def test_offline_draft_eight_pages_and_no_fabricated_comparison(self):
        report = make_offline_draft(SOURCE, "2026/10/12—2026/10/18")
        check = validate_weekly_report(report)
        self.assertTrue(check["valid"])
        self.assertEqual(8, len(check["pages"]))
        self.assertIn("22 人", report)
        self.assertIn("3 次", report)
        self.assertIn("【待补充：请提供本期/上期可比数据", report)
        self.assertNotIn("环比上升", report)

    def test_invalid_report_and_duplicates_blocked(self):
        report = make_offline_draft(SOURCE)
        self.assertFalse(validate_weekly_report(report.replace("## P8 [type:closing] | Thankyou", "## P9 [type:closing] | Thankyou"))["valid"])
        self.assertFalse(validate_weekly_report(report + "\n## P4 重复页\n重写")["valid"])

    def test_ppt_overflow_detected_instead_of_silently_dropping(self):
        report = make_offline_draft(SOURCE)
        report = report.replace("下周重点工作计划", "下周重点工作计划")
        report = report.replace("10/18日，安保团队完成重点区域安全巡检并记录隐患", "\n".join(f"10/{i}日，完成第{i}次检查" for i in range(1, 11)))
        self.assertTrue(any("P6" in issue for issue in ppt_content_issues(report)))

    def test_generated_docx_has_all_sections_and_is_editable(self):
        from docx import Document
        with tempfile.TemporaryDirectory() as folder:
            out = build_weekly_docx(make_offline_draft(SOURCE), Path(folder) / "weekly.docx")
            self.assertTrue(out.is_file())
            doc = Document(out)
            whole = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("本周在岗人员 22 人", whole)
            self.assertIn("供应商无人陪同进入保密区域", whole)
            self.assertIn("下周计划推进安保巡逻复盘", whole)

    def test_generated_ppt_all_six_business_text_pages(self):
        from pptx import Presentation
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "weekly.pptx"
            report = make_offline_draft(SOURCE)
            build_ppt(report, path)
            prs = Presentation(path)
            self.assertEqual(8, len(prs.slides))
            txt = "\n".join(shape.text for slide in prs.slides for shape in slide.shapes if shape.has_text_frame)
            self.assertIn("10/18日，安保团队完成重点区域安全巡检并记录隐患", txt)
            self.assertIn("Thankyou", txt)

    def test_model_exception_does_not_return_rule_draft_as_ai_report(self):
        with patch.object(webapp.guard, "call_llm", side_effect=RuntimeError("model not running")):
            report, plan, status, state = webapp.structure_ppt_source_ui(SOURCE, "2026/10/12—2026/10/18")
        self.assertEqual("", report)
        self.assertIn("AI 周报创作未完成", status)
        self.assertIn("本地规则分类", plan)
        self.assertEqual("failed", state["generation"]["status"])
        output, export_status = webapp.export_ppt_workspace(report, "标准版", None, state)
        self.assertFalse(output.get("visible", True))
        self.assertIn("不能作为正式周报导出", export_status)


    def test_fact_ledger_preserves_source_line_ids_categories_and_quantities(self):
        facts = extract_weekly_facts(SOURCE)
        self.assertEqual(["F001", "F002", "F003", "F004", "F005"], [f["id"] for f in facts])
        self.assertEqual("staff", facts[1]["category"])
        self.assertEqual("incident", facts[2]["category"])
        self.assertEqual((22.0, "count"), (facts[1]["quantities"][0]["value"], facts[1]["quantities"][0]["unit"]))
        self.assertIn("10/18日", facts[0]["dates"])
        self.assertEqual(3, facts[2]["source_line"])

    def test_analysis_rejects_unknown_fact_ids(self):
        facts = extract_weekly_facts(SOURCE)
        payload = {
            "page_plan": [
                {"page": n, "title": f"P{n}", "purpose": "test", "fact_ids": (["F999"] if n == 4 else []), "missing_fields": []}
                for n in range(1, 9)
            ]
        }
        analysis, errors = normalize_weekly_analysis(payload, facts)
        self.assertEqual({}, analysis)
        self.assertTrue(any("不存在的事实 ID" in err for err in errors))

    def test_grounding_rejects_unsupported_numbers(self):
        facts = extract_weekly_facts(SOURCE)
        analysis = {
            "page_plan": [
                {"page": n, "fact_ids": ({3: ["F002"], 4: ["F004"], 5: ["F003"], 6: ["F001"], 7: ["F005"]}.get(n, []))}
                for n in range(1, 9)
            ],
            "verified_calculations": [],
        }
        report = make_offline_draft(SOURCE).replace("本周在岗人员 22 人", "本周在岗人员 999 人")
        errors = validate_report_grounding(report, facts, analysis)
        self.assertTrue(any("999" in err for err in errors))

    def test_export_blocks_manual_unsupported_numbers_when_provenance_is_available(self):
        facts = extract_weekly_facts(SOURCE)
        analysis = {
            "page_plan": [
                {"page": n, "fact_ids": ({3: ["F002"], 4: ["F004"], 5: ["F003"], 6: ["F001"], 7: ["F005"]}.get(n, []))}
                for n in range(1, 9)
            ],
            "verified_calculations": [],
        }
        state = {"facts": facts, "analysis": analysis}
        report = make_offline_draft(SOURCE).replace("本周在岗人员 22 人", "本周在岗人员 999 人")
        output, message = webapp.export_word_workspace(report, state)
        self.assertFalse(output.get("visible", True))
        self.assertIn("来源核验未通过", message)
        self.assertIn("999", message)

    def test_verified_delta_requires_two_source_values_and_correct_calculation(self):
        facts = extract_weekly_facts("本周违禁品查获 27×\n上周违禁品查获 21×")
        page_plan = [
            {"page": n, "title": f"P{n}", "purpose": "test", "fact_ids": (["F001", "F002"] if n == 4 else []), "missing_fields": []}
            for n in range(1, 9)
        ]
        payload = {
            "page_plan": page_plan,
            "data_insights": [{
                "page": 4, "text": "本周较上周增加 6×", "fact_ids": ["F001", "F002"],
                "calculation": "27 - 21 = 6",
            }],
        }
        analysis, errors = normalize_weekly_analysis(payload, facts)
        self.assertEqual([], errors)
        self.assertEqual(1, len(analysis["data_insights"]))
        self.assertTrue(analysis["data_insights"][0]["calculation_verified"])
        payload["data_insights"][0]["calculation"] = "27 - 21 = 7"
        analysis, errors = normalize_weekly_analysis(payload, facts)
        self.assertEqual([], errors)
        self.assertEqual([], analysis["data_insights"])
        self.assertTrue(analysis["warnings"])

        mixed_metrics = extract_weekly_facts("本周在岗人员 22 人\n上周异常事件 3 次")
        from core.weekly_report_agent import _verify_delta_calculation
        by_id = {fact["id"]: fact for fact in mixed_metrics}
        self.assertIsNone(_verify_delta_calculation("22 - 3 = 19", ["F001", "F002"], by_id))

    def test_evidence_compiler_does_not_depend_on_freeform_page_generation(self):
        facts = extract_weekly_facts(SOURCE)
        category_id = {fact["category"]: fact["id"] for fact in facts}
        titles = ["H区安保周报", "目录", "厂区分布&人力分布", "专区运营看板", "异常汇总分析", "本周重点工作", "下周重点工作计划", "Thankyou"]
        assignment = {3: [category_id["staff"]], 4: [category_id["operations"]], 5: [category_id["incident"]], 6: [category_id["completed_work"]], 7: [category_id["next_plan"]]}
        analysis_payload = {
            "page_plan": [
                {"page": n, "page_type": ["cover", "contents", "manpower", "operations", "anomalies", "key_work", "next_plan", "closing"][n-1],
                 "title": titles[n-1], "purpose": "测试页面", "fact_ids": assignment.get(n, []), "missing_fields": []}
                for n in range(1, 9)
            ],
            "executive_summary": {"text": "本期覆盖人力、运营、异常和计划素材。", "fact_ids": [f["id"] for f in facts]},
        }
        results = [
            {"status": "ok", "output": __import__("json").dumps(analysis_payload, ensure_ascii=False)},
            RuntimeError("model unavailable on page 3"),
        ]
        with patch.object(webapp.guard, "call_llm", side_effect=results):
            report, plan, status, state = webapp.structure_ppt_source_ui(SOURCE, "2026/10/12—2026/10/18")
        self.assertTrue(validate_weekly_report(report)["valid"])
        self.assertIn("证据编译", status)
        self.assertEqual("completed", state["generation"]["status"])
        self.assertEqual(5, state["generation"]["evidence_compiled_pages"])

    def test_two_stage_content_intelligence_pipeline(self):
        facts = extract_weekly_facts(SOURCE)
        categories = {fact["category"]: fact["id"] for fact in facts}
        titles = ["封面", "目录", "厂区分布&人力分布", "安保服务项目| 专区运营看板", "安保服务项目| 异常汇总分析", "本周重点工作", "下周重点工作计划", "结束页"]
        assignment = {3: [categories["staff"]], 4: [categories["operations"]], 5: [categories["incident"]], 6: [categories["completed_work"]], 7: [categories["next_plan"]]}
        analysis_payload = {
            "executive_summary": {"text": "本期素材覆盖巡检、人力、运营、异常和下周计划。", "fact_ids": [f["id"] for f in facts]},
            "highlights": [{"text": "安保团队完成重点区域安全巡检并记录隐患", "fact_ids": [categories["completed_work"]]}],
            "risks": [{"text": "供应商无人陪同进入保密区域，发现异常 2 次", "fact_ids": [categories["incident"]]}],
            "next_week": [{"text": "下周计划推进安保巡逻复盘", "fact_ids": [categories["next_plan"]]}],
            "data_insights": [],
            "page_plan": [
                {"page": n, "title": titles[n-1], "purpose": "按八页模板组织内容", "fact_ids": assignment.get(n, []), "missing_fields": []}
                for n in range(1, 9)
            ],
            "missing_info": [],
        }
        page_bodies = [
            "本周在岗人员 22 人。",
            "违禁品查获 3 次。本期素材未提供上周可比基数，暂不生成趋势结论。",
            "供应商无人陪同进入保密区域，发现异常 2 次。",
            "10/18日，安保团队完成重点区域安全巡检并记录隐患。",
            "推进安保巡逻复盘。",
        ]
        llm_results = [
            {"status": "ok", "output": __import__("json").dumps(analysis_payload, ensure_ascii=False)},
            *[{"status": "ok", "output": body} for body in page_bodies],
        ]
        with patch.object(webapp.guard, "call_llm", side_effect=llm_results) as mock_llm:
            report, plan, status, state = webapp.structure_ppt_source_ui(SOURCE, "2026/10/12—2026/10/18")
        self.assertEqual(1, mock_llm.call_count)  # 仅一次 AI 语义分析；业务事实由证据编译器生成
        self.assertTrue(validate_weekly_report(report)["valid"])
        self.assertIn("AI 语义分析与证据编译", status)
        self.assertIn("AI 结构化分析", plan)
        self.assertIn("来源：F002", plan)
        self.assertEqual(5, len(state["facts"]))
        self.assertEqual("ai_structured", state["analysis"]["mode"])
        self.assertEqual("completed", state["generation"]["status"])
        self.assertEqual(3, state["generation"]["ai_summary_pages"])
        self.assertEqual(5, state["generation"]["evidence_compiled_pages"])

    def test_fact_extraction_splits_mixed_metrics_and_date_event(self):
        source = (
            "①岗位/人力数据 锦溪 | H区:楼栋3x，岗位3x，人力5x，日均通行数量166x "
            "9/15日，BU21经理至H1-3F车间开展视察工作，执勤保安全程在岗接待"
        )
        facts = extract_weekly_facts(source)
        self.assertEqual(["staff", "staff", "staff", "operations", "completed_work"], [f["category"] for f in facts])
        self.assertIn("9/15日", facts[-1]["text"])
        self.assertIn("H1-3F", facts[-1]["text"])
        self.assertNotIn("H1-3F", " ".join(f["text"] for f in facts[:-1]))

    def test_historical_week_table_values_can_drive_chart_but_fabricated_value_is_blocked(self):
        from core.weekly_report_agent import weekly_page_generation_issues, _has_comparable_period_values
        facts = extract_weekly_facts("违禁品携带\t9wk1\t21\t9wk2\t27")
        self.assertTrue(_has_comparable_period_values(facts))
        page = {"page": 4, "page_type": "operations", "fact_ids": [facts[0]["id"]]}
        analysis = {"verified_calculations": []}
        supported = "违禁品携带数据对比。\n图表数据：\n指标 | 上周 | 本周\n违禁品携带 | 21 | 27"
        self.assertEqual([], weekly_page_generation_issues(supported, page, facts, analysis))
        fabricated = supported.replace("| 27", "| 99")
        self.assertTrue(any("99 未出现在" in issue for issue in weekly_page_generation_issues(fabricated, page, facts, analysis)))

    def test_user_writing_instruction_is_removed_but_plan_fact_is_kept(self):
        facts = extract_weekly_facts("⑤下周计划：01、NPI架设缺失项跟进；下周计划的具体详细内容你帮我写")
        self.assertEqual(1, len(facts))
        self.assertEqual("next_plan", facts[0]["category"])
        self.assertIn("NPI架设缺失项跟进", facts[0]["text"])
        self.assertNotIn("帮我写", facts[0]["text"])

    def test_evidence_compiler_emits_verified_chart_blocks(self):
        """证据编译器必须自己出图：有可比周期值做对比图，有并列数量做分类图。"""
        facts = extract_weekly_facts(
            "①岗位/人力数据 锦溪 | H区:楼栋3x，岗位3x，人力5x\n"
            "②专区运营 违禁品携带\t9wk1\t21\t9wk2\t27\n"
            "9wk3\t25"
        )
        analysis = make_offline_analysis(facts, "2025年第34周")
        compiled = {}
        for page in analysis["page_plan"]:
            body, _ = compile_grounded_weekly_page(facts, analysis, page)
            page_facts = [f for f in facts if f["id"] in page.get("fact_ids", [])]
            compiled[page.get("page_type")] = body
            self.assertEqual(
                [], _chart_data_block_issues(body, page_facts, page["page"]), page.get("title"),
            )
        self.assertIn("图表数据：", compiled["operations"])
        self.assertIn("违禁品携带", compiled["operations"])
        self.assertIn("9wk2|27", compiled["operations"].replace(" ", ""))
        # 后续周期单列一行时也要并入同一张图，不能少画一期。
        self.assertIn("9wk3|25", compiled["operations"].replace(" ", ""))
        self.assertIn("楼栋|3", compiled["manpower"].replace(" ", ""))
        self.assertNotIn("图标", compiled["manpower"])

    def test_exported_ppt_contains_editable_chart(self):
        from pptx import Presentation
        facts = extract_weekly_facts("②专区运营 违禁品携带\t9wk1\t21\t9wk2\t27")
        analysis = make_offline_analysis(facts, "2025年第34周")
        pages = []
        for page in analysis["page_plan"]:
            body, _ = compile_grounded_weekly_page(facts, analysis, page)
            pages.append(f"## P{page['page']} [type:{page['page_type']}] | {page['title']}\n{body}")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "weekly.pptx"
            build_ppt("\n\n".join(pages), out)
            prs = Presentation(str(out))
            charts = [sh for slide in prs.slides for sh in slide.shapes if getattr(sh, "has_chart", False)]
            self.assertTrue(charts, "含图表数据块的周报应导出为可编辑图表")
            self.assertEqual("LUX_DYNAMIC_CHART", charts[0].name)

    def test_preview_shows_chart_before_export(self):
        """界面预览必须自己画出图表，否则用户会以为“生成成功但没有图表”。"""
        report = (
            "## P1 [type:cover] | 封面\n统计周期：2026/10/19—2026/10/25\n\n"
            "## P2 [type:operations] | 专区运营看板\n违禁品携带 9wk1 21 9wk2 27\n\n"
            "图表数据：\n类别|违禁品携带\n9wk1|21\n9wk2|27"
        )
        rendered = webapp._render_ppt_plan(report, None, None)
        self.assertEqual(1, rendered.count("weekly-chart-svg"))
        self.assertIn("P2 · 专区运营看板", rendered)
        self.assertIn("9wk1", rendered)

    def test_standalone_section_heading_carries_to_following_lines(self):
        # 标题单独成行时，其后的条目必须沿用该分区，否则下周计划会被写进本周重点工作。
        source = (
            "本周工作：\n"
            "8/18 完成H区东门门禁系统巡检，发现读卡器故障2处，已报修并更换完毕。\n"
            "下周计划：\n"
            "8/25 开展全员反恐防暴演练。\n"
            "8/26 完成H区监控盲区排查并提交改造建议。"
        )
        facts = extract_weekly_facts(source)
        self.assertEqual(
            ["completed_work", "next_plan", "next_plan"],
            [f["category"] for f in facts],
        )
        self.assertNotIn("本周工作", [f["text"] for f in facts])

    def test_quality_gate_blocks_prompt_leak_and_placeholders(self):
        from core.weekly_report_agent import weekly_content_quality_issues
        self.assertEqual([], weekly_content_quality_issues(CLEAN_REPORT))
        leaked = CLEAN_REPORT.replace("推进安保巡逻复盘。", "下周计划的具体详细内容你帮我写")
        self.assertTrue(any("用户指令" in issue for issue in weekly_content_quality_issues(leaked)))
        placeholder = CLEAN_REPORT.replace("推进安保巡逻复盘。", "【待补充：下周计划】")
        self.assertTrue(any("待补充占位内容" in issue for issue in weekly_content_quality_issues(placeholder)))

    def test_export_blocks_unreviewed_placeholder_content(self):
        output, message = webapp.export_word_workspace(CLEAN_REPORT.replace("推进安保巡逻复盘。", "【待补充：下周计划】"))
        self.assertFalse(output.get("visible", True))
        self.assertIn("质量检查未通过", message)

    def test_two_exports_unique_and_auditable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original_cwd = os.getcwd()
            orig_store = webapp.deliverable_store
            try:
                os.chdir(root)
                webapp.deliverable_store = DeliverableStore(root / "deliverables.db")
                report = CLEAN_REPORT
                first, tip = webapp.export_word_workspace(report)
                second, tip2 = webapp.export_word_workspace(report)
                self.assertNotEqual(first.get("value"), second.get("value"))
                self.assertTrue(Path(first["value"]).exists())
                self.assertIn("V2", tip2)
                ppt, ppt_tip = webapp.export_ppt_workspace(report, "标准版", None)
                self.assertTrue(Path(ppt["value"]).exists())
                self.assertIn("已生成", ppt_tip)
                rows = webapp.deliverable_store.list_for_user("system")
                self.assertEqual(3, len(rows))
                self.assertEqual({"Word", "PPT"}, {r["kind"] for r in rows})
            finally:
                webapp.deliverable_store = orig_store
                os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
