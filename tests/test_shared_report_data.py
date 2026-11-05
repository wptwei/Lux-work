"""共享协议的兼容性契约：格式差异、校验失败及提供方请求保持不变。"""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from core import ppt_export, ppt_pipeline, ppt_template, providers, report_data
from core.weekly_report_agent import _visible_ppt_body


class SharedReportDataTests(unittest.TestCase):
    def setUp(self):
        self.chart = {"type": "column", "unit": "次", "categories": ["巡检"],
                      "series": [{"name": "本周", "values": [3]}]}

    def test_legacy_imports_keep_same_protocol(self):
        self.assertIs(ppt_export.parse_report, report_data.parse_report)
        self.assertIs(ppt_template._parse_chart_data, report_data._parse_chart_data)
        self.assertIs(ppt_pipeline.validate_chart, report_data.validate_chart)

    def test_json_report_preserves_chart_and_title(self):
        source = json.dumps({"pages": [{"title": "巡检", "body": "正文", "chart": self.chart}]})
        pages = report_data.parse_report(source)
        self.assertEqual(pages[0]["title"], "巡检")
        self.assertEqual(report_data._parse_chart_data(pages[0]["body"])["series"], [[3]])

    def test_chinese_blocks_resume_at_key_message(self):
        body = "正文\n图表数据：\n类别|数量\n巡检|3\nKey Message: 已完成\n补充"
        expected = "正文\nKey Message: 已完成\n补充"
        for fn in (ppt_export._visible_body, ppt_template._strip_structured_blocks, _visible_ppt_body):
            self.assertEqual(fn(body), expected)

    def test_english_labels_remain_template_only(self):
        body = "正文\nchart data:\nCategory|Count\nPatrol|3\n\n结论"
        self.assertEqual(ppt_export._visible_body(body), body)
        self.assertEqual(_visible_ppt_body(body), body)
        self.assertEqual(ppt_template._strip_structured_blocks(body), "正文\n结论")

    def test_length_checker_retains_json_legacy_behavior(self):
        body = '正文\nCHART_JSON:\n{}\nEND_CHART_JSON\n结论'
        self.assertEqual(_visible_ppt_body(body), body)
        self.assertEqual(ppt_export._visible_body(body), '正文\n结论')
        self.assertEqual(ppt_template._strip_structured_blocks(body), '正文\n结论')

    def test_chart_rows_preserve_units_and_multiseries(self):
        data = report_data._parse_chart_data("图表数据：\n类别|本周|上周\n巡检|3次|2次\n门禁|4次|1次")
        self.assertEqual(data, {"categories": ["巡检", "门禁"], "series_names": ["本周", "上周"], "series": [[3, 4], [2, 1]]})

    def test_inconsistent_rows_are_not_guessed(self):
        self.assertIsNone(report_data._parse_chart_data("图表数据：\n类别|数量\n巡检|3|4"))
        self.assertIsNone(report_data._parse_chart_data("巡检3次，门禁4次"))

    def test_json_chart_rejects_bool_missing_nonfinite(self):
        for value in (True, None, float('nan'), float('inf'), '3'):
            with self.subTest(value=value):
                chart = copy.deepcopy(self.chart)
                chart['series'][0]['values'] = [value]
                with self.assertRaisesRegex(ValueError, '有限数字'):
                    report_data.validate_chart(chart)

    def test_invalid_chart_in_report_keeps_page_error(self):
        self.chart['series'][0]['values'] = []
        with self.assertRaisesRegex(ValueError, 'P1：数值长度'):
            report_data.parse_report(json.dumps({'pages': [{'chart': self.chart}]}))

    def test_pie_requires_positive_total(self):
        self.chart['type'] = 'pie'
        self.chart['series'][0]['values'] = [0]
        with self.assertRaisesRegex(ValueError, '饼图'):
            report_data.validate_chart(self.chart)

    def test_template_block_stops_at_page_heading(self):
        body = '图表数据：\n类别|数量\n巡检|3\n## P3\n其他正文'
        self.assertEqual(report_data._extract_block(body, 'chart'), ['类别|数量', '巡检|3'])


class ProviderMessageCompatibilityTests(unittest.TestCase):
    def test_ollama_request_keeps_options_and_json_mode(self):
        with patch.object(providers, '_post_json', return_value={'message': {'content': '完成'}}) as post:
            self.assertEqual(providers.ollama_llm('任务', '规则', num_predict=91, json_mode=True), '完成')
        body = post.call_args.args[1]
        self.assertEqual(body['messages'], [{'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '任务'}])
        self.assertFalse(body['stream'])
        self.assertEqual(body['format'], 'json')
        self.assertEqual(body['options']['num_predict'], 91)

    def test_deepseek_keeps_protocol_and_no_empty_system(self):
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'test-only'}), patch.object(
            providers, '_post_json', return_value={'choices': [{'message': {'content': '完成'}}]}
        ) as post:
            self.assertEqual(providers.deepseek_llm('任务', num_predict=91, json_mode=True), '完成')
        body = post.call_args.args[1]
        self.assertEqual(body['messages'], [{'role': 'user', 'content': '任务'}])
        self.assertEqual(body['max_tokens'], 91)
        self.assertEqual(body['response_format'], {'type': 'json_object'})

    def test_stream_keeps_message_order_and_done_boundary(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = [
            b'{"message":{"content":"a"},"done":false}\n',
            b'{"message":{"content":"b"},"done":true}\n',
            b'{"message":{"content":"ignored"}}\n',
        ]
        with patch.object(providers.urllib.request, 'urlopen', return_value=response) as call:
            self.assertEqual(list(providers.ollama_llm_stream('任务', '规则')), ['a', 'b'])
        body = json.loads(call.call_args.args[0].data)
        self.assertTrue(body['stream'])
        self.assertEqual(body['messages'], [{'role': 'system', 'content': '规则'}, {'role': 'user', 'content': '任务'}])

    def test_requests_do_not_share_mutable_messages(self):
        first = providers._chat_messages('任务')
        first[0]['content'] = '修改'
        self.assertEqual(providers._chat_messages('任务'), [{'role': 'user', 'content': '任务'}])


if __name__ == '__main__':
    unittest.main()
