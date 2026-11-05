"""历史周报模板映射、结构化图表、文本适配与交付诊断测试。"""
import os
import tempfile
import unittest
from pathlib import Path

# 导出会启动 PowerShell 调 PowerPoint 逐页渲染；单元测试不需要，装没装 Office 都不该等它。
os.environ.setdefault("LUX_SKIP_PPT_PREVIEW", "1")

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Inches

from core.ppt_template import (
    build_ppt_from_template,
    fill_real_template,
    fill_real_template_with_diagnostics,
    inspect_template,
)
from core.ppt_export import build_ppt
from core.weekly_report_agent import ppt_content_issues


def make_report() -> str:
    return """## P1 封面
H区安保第42周周报
## P2 目录
Contents
## P3 厂区分布&人力分布
厂区分布&人力分布
安保岗：22人
巡逻岗：8人
Key Message：本周岗位数据已汇总。
## P4 安保服务项目| 专区运营看板
安保服务项目| 专区运营看板
违禁品查获：27×
Key message：本周违禁品查获 27×，上周 21×。
图表数据：
类别|本周|上周
违禁品|27|21
异常事件|5|8
## P5 安保服务项目| 异常汇总分析
安保服务项目| 异常汇总分析
异常事件：5次
Key Message：本周异常事件 5 次，上周 8 次。
图表数据：
类别|本周|上周
门禁异常|2|3
未授权进入|3|5
## P6 本周重点工作
本周重点工作
10/20日，完成现场巡查
10/21日，完成隐患复核
10/22日，完成岗点培训
10/23日，完成台账更新
## P7 下周重点工作计划
下周重点工作计划
01、现场跟进
跟进现场架设
02、物理防护
加强巡逻点检
03、AI解答
收集同事需求
## P8 结束页
Thankyou
"""


class PptTemplateEngineTests(unittest.TestCase):
    def _make_history_template(self, path: Path, with_native_chart: bool = False):
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        blank = prs.slide_layouts[6]
        for i in range(8):
            slide = prs.slides.add_slide(blank)
            if i == 0:
                title = slide.shapes.add_textbox(Inches(1), Inches(2.5), Inches(10), Inches(1))
                title.name = "CoverTitle"
                title.text_frame.text = "H区第41周周报"
                date = slide.shapes.add_textbox(Inches(8), Inches(6), Inches(4), Inches(.4))
                date.name = "CoverDate"
                date.text_frame.text = "By : Security team Date： 2026/10/1 Rev. 01"
            elif i == 1:
                title = slide.shapes.add_textbox(Inches(.5), Inches(.3), Inches(4), Inches(.5))
                title.text_frame.text = "Contents"
            elif i == 2:
                title = slide.shapes.add_textbox(Inches(.5), Inches(.3), Inches(8), Inches(.5))
                title.name = "P3Title"
                title.text_frame.text = "厂区分布&人力分布（上期）"
                table_shape = slide.shapes.add_table(4, 2, Inches(.5), Inches(1.5), Inches(5), Inches(2))
                table_shape.name = "ManpowerTable"
                table = table_shape.table
                table.cell(0, 0).text = "岗位"
                table.cell(0, 1).text = "人数"
                for r in range(1, 4):
                    table.cell(r, 0).text = f"旧岗位{r}"
                    table.cell(r, 1).text = str(r)
                key = slide.shapes.add_textbox(Inches(6), Inches(1.5), Inches(5), Inches(1))
                key.text_frame.text = "Key Message：旧结论"
            elif i == 3:
                title = slide.shapes.add_textbox(Inches(.5), Inches(.3), Inches(8), Inches(.5))
                title.name = "P4Title"
                title.text_frame.text = "上期运营看板"
                placeholder = slide.shapes.add_textbox(Inches(1), Inches(1.5), Inches(5), Inches(3))
                placeholder.name = "P4ChartPlaceholder"
                placeholder.text_frame.text = "数据图表（每周更新）"
                key = slide.shapes.add_textbox(Inches(1), Inches(5), Inches(10), Inches(.8))
                key.text_frame.text = "Key message：旧数据"
            elif i == 4:
                title = slide.shapes.add_textbox(Inches(.5), Inches(.3), Inches(8), Inches(.5))
                title.text_frame.text = "上期异常分析"
                chart_data = CategoryChartData()
                chart_data.categories = ["旧A", "旧B"]
                chart_data.add_series("旧系列", [1, 2])
                slide.shapes.add_chart(51, Inches(1), Inches(1.5), Inches(5), Inches(3), chart_data)
                key = slide.shapes.add_textbox(Inches(1), Inches(5), Inches(10), Inches(.8))
                key.text_frame.text = "Key Message：旧结论"
            elif i == 5:
                title = slide.shapes.add_textbox(Inches(.5), Inches(.3), Inches(8), Inches(.5))
                title.text_frame.text = "本周重点工作"
                for n in range(3):
                    item = slide.shapes.add_textbox(Inches(1), Inches(1.5 + n * .7), Inches(10), Inches(.5))
                    item.name = f"WorkItem{n+1}"
                    item.text_frame.text = f"10/{n+1}日，旧工作事项"
            elif i == 6:
                title = slide.shapes.add_textbox(Inches(.5), Inches(.3), Inches(8), Inches(.5))
                title.text_frame.text = "下周重点工作计划"
                body = slide.shapes.add_textbox(Inches(1), Inches(1.5), Inches(10), Inches(4))
                body.name = "NextWeekBody"
                body.text_frame.text = "具体事项1\n具体事项2"
            else:
                end = slide.shapes.add_textbox(Inches(4), Inches(3), Inches(5), Inches(1))
                end.text_frame.text = "Thankyou"
        prs.save(path)

    def test_inspect_template_returns_shape_map_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "history.pptx"
            self._make_history_template(template, with_native_chart=True)
            meta = inspect_template(template)
            self.assertEqual(8, meta["slide_count"])
            self.assertGreaterEqual(meta["table_count"], 1)
            self.assertGreaterEqual(meta["native_chart_count"], 1)
            self.assertEqual(8, len(meta["slides"]))
            self.assertIn("suggested_title_shape", meta["slides"][0])

    def test_historical_template_updates_titles_tables_and_dynamic_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "history.pptx"
            out = Path(tmp) / "filled.pptx"
            self._make_history_template(template)
            diagnostics = fill_real_template_with_diagnostics(template, make_report(), out)
            self.assertTrue(out.exists())
            self.assertGreaterEqual(diagnostics["updated_text_frames"], 4)
            self.assertGreaterEqual(diagnostics["updated_tables"], 1)
            self.assertGreaterEqual(diagnostics["created_charts"], 1)
            result = Presentation(out)
            self.assertEqual(8, len(result.slides))
            slide3 = result.slides[2]
            table_text = " ".join(cell.text for row in next(s.table for s in slide3.shapes if s.has_table).rows for cell in row.cells)
            self.assertIn("安保岗", table_text)
            self.assertIn("22人", table_text)
            slide4_text = "\n".join(s.text for s in result.slides[3].shapes if s.has_text_frame)
            self.assertIn("本周违禁品查获 27×，上周 21×", slide4_text)
            self.assertTrue(any(s.has_chart for s in result.slides[3].shapes))

    def test_native_chart_updates_from_structured_data_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "history.pptx"
            out = Path(tmp) / "filled.pptx"
            self._make_history_template(template)
            diagnostics = fill_real_template_with_diagnostics(template, make_report(), out)
            self.assertGreaterEqual(diagnostics["updated_charts"], 1)
            result = Presentation(out)
            chart_shapes = [s for s in result.slides[4].shapes if s.has_chart]
            self.assertEqual(1, len(chart_shapes))
            categories = [str(c.label) for c in chart_shapes[0].chart.plots[0].categories]
            self.assertIn("门禁异常", categories)
            self.assertIn("未授权进入", categories)

    def test_marker_template_supports_page_specific_slots(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "marker.pptx"
            out = Path(tmp) / "filled.pptx"
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            title = slide.shapes.add_textbox(0, 0, Inches(5), Inches(.5))
            title.text_frame.text = "{{P4_TITLE}}"
            body = slide.shapes.add_textbox(0, Inches(1), Inches(10), Inches(2))
            body.text_frame.text = "{{P4_KEY_MESSAGE}}"
            prs.save(template)
            fill_real_template(template, make_report(), out)
            result = Presentation(out)
            texts = "\n".join(s.text for s in result.slides[0].shapes if s.has_text_frame)
            self.assertNotIn("{{", texts)
            self.assertIn("专区运营看板", texts)
            self.assertIn("本周违禁品查获 27×，上周 21×", texts)

    def test_table_capacity_and_static_image_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "history.pptx"
            out = Path(tmp) / "filled.pptx"
            self._make_history_template(template)
            report = make_report().replace("巡逻岗：8人", "巡逻岗：8人\n门岗：4人\n监控岗：3人\n机动岗：2人")
            diagnostics = fill_real_template_with_diagnostics(template, report, out)
            self.assertTrue(any("表格容量不足" in warning for warning in diagnostics["warnings"]))

    def test_standard_ppt_renders_chart_block_as_editable_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "standard.pptx"
            build_ppt(make_report(), out)
            result = Presentation(out)
            self.assertTrue(any(shape.has_chart for shape in result.slides[3].shapes))
            visible_text = "\n".join(
                shape.text for slide in result.slides for shape in slide.shapes if shape.has_text_frame
            )
            self.assertNotIn("类别|本周|上周", visible_text)
            self.assertNotIn("图表数据：", visible_text)

    def test_chart_data_block_is_not_counted_as_visible_body_overflow(self):
        report = make_report()
        issues = ppt_content_issues(report)
        self.assertEqual([], issues)


if __name__ == "__main__":
    unittest.main()
