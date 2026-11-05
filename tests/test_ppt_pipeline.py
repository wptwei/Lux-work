import json, os, tempfile, unittest
from pathlib import Path

# 导出会启动 PowerShell 调 PowerPoint 逐页渲染；单元测试不需要，装没装 Office 都不该等它。
os.environ.setdefault("LUX_SKIP_PPT_PREVIEW", "1")
from pptx import Presentation
from core.ppt_export import build_ppt, parse_report
from core.ppt_template import build_ppt_from_template, fill_real_template_with_diagnostics
from core.ppt_pipeline import validate_chart

class StructuredExportTests(unittest.TestCase):
    def test_modes(self):
        spec={'type':'line','unit':'件','categories':['甲','乙'],'series':[{'name':'数量','values':[2,4]}]}
        doc=json.dumps({'pages':[{'type':'cover','title':'封面','body':'测试'},{'type':'operations','title':'趋势','body':'内容','chart':spec}]},ensure_ascii=False)
        with tempfile.TemporaryDirectory() as d:
            template=Path(d)/'template.pptx'
            prs=Presentation()
            for i in range(2):
                slide=prs.slides.add_slide(prs.slide_layouts[6])
                slide.shapes.add_textbox(100000,100000,3000000,2000000).text='图表（每周更新）' if i else '封面'
            prs.save(template)
            for mode in ('default','blank','historical','reference'):
                out=Path(d)/(mode+'.pptx')
                if mode=='default': build_ppt(doc,out)
                elif mode=='blank': build_ppt_from_template(template,doc,out)
                elif mode=='reference': build_ppt(doc,out,template)
                else: fill_real_template_with_diagnostics(template,doc,out)
                deck=Presentation(out)
                charts=[s.chart for s in deck.slides[1].shapes if s.has_chart]
                self.assertEqual(len(charts),1,mode)
                self.assertEqual(list(charts[0].series[0].values),[2,4])
                self.assertEqual(charts[0].chart_type,4)
                self.assertNotIn('CHART_JSON',' '.join(s.text for s in deck.slides[1].shapes if s.has_text_frame))
                self.assertTrue(out.with_suffix('.qa.json').exists())
    def test_single_series_page_does_not_repeat_chart_values(self):
        """单系列图表旁的“核心发现”不再逐条复述每根柱子，也不再露出脱敏标记 x。"""
        report = (
            "## P1 [type:cover] | 封面\n统计周期：2026/10/19—2026/10/25\n\n"
            "## P2 [type:operations] | 专区运营看板\n"
            "日均通行数量166x\n违禁品携带 9wk1 21 9wk2 27\n"
            "图表数据：\n类别|违禁品携带\n9wk1|21\n9wk2|27\n9wk3|25"
        )
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "one.pptx"
            build_ppt(report, out)
            deck = Presentation(out)
            texts = [s.text_frame.text for s in deck.slides[1].shapes if getattr(s, "has_text_frame", False)]
            body = "\n".join(texts)
            self.assertNotIn("9wk1：违禁品携带 21", body)
            self.assertIn("日均通行数量166", body)
            self.assertNotIn("166x", body)
            self.assertTrue(any(getattr(s, "has_chart", False) for s in deck.slides[1].shapes))

    def test_bad_data(self):
        for vals in ([1], [True,2], [float('nan'),2]):
            with self.assertRaises(ValueError):
                validate_chart({'type':'line','unit':'件','categories':['甲','乙'],'series':[{'name':'n','values':vals}]})
    def test_legacy(self):
        from core.ppt_template import _parse_chart_data
        self.assertEqual(_parse_chart_data('图表数据：\n类别|本周\n甲|3')['series'],[[3]])

if __name__=='__main__': unittest.main()
