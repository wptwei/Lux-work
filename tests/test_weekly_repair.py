import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from core import providers
from core.weekly_report_agent import (extract_weekly_facts, extract_quantities, make_offline_analysis, normalize_weekly_analysis, restore_masked_quantity_markers, weekly_page_generation_issues)
from core.weekly_report_agent import build_weekly_compact_analysis_prompt
import app

SOURCE = '\n'.join(['锦溪 H区:楼栋3x','岗位3x','人力5x','日均通行数量166x','所服务BU：BU21','因生产办公需要，故本周违禁品数量有所增加，较上周增加了2x','上周未进行垃圾检查，本周垃圾检查工时为0.71h','本周稽核异常较上周增加2x，已对相关责任人宣导处理','专区异常较上周减少了1x','9/14日，完成现场跟进','9/15日，开展视察接待工作','9/16日，完成参观接待保障','下周计划：NPI架设缺失跟进','02、岗位管控更换'])

def payload():
    return {
        'executive_summary': {
            'text': '本期素材覆盖人力、运营、异常、重点工作和后续计划。',
            'fact_ids': ['F001', 'F004', 'F008', 'F010', 'F013'],
        },
        'page_plan': [
        {'page':1,'page_type':'cover','title':'封面','fact_ids':[]},
        {'page':2,'page_type':'manpower','title':'人力','fact_ids':['F001','F002','F003']},
        {'page':3,'page_type':'operations','title':'运营','fact_ids':['F004','F007']},
        {'page':4,'page_type':'anomalies','title':'异常','fact_ids':['F008','F009']},
        {'page':5,'page_type':'key_work','title':'重点工作','fact_ids':['F010','F011','F012']},
        {'page':6,'page_type':'next_plan','title':'下周计划','fact_ids':['F013']},
        {'page':7,'page_type':'closing','title':'结束','fact_ids':[]}]
    }

class WeeklyRepairTests(unittest.TestCase):
    def test_compact_local_prompt_uses_fixed_plan_without_requesting_page_plan(self):
        facts = extract_weekly_facts(SOURCE)
        fixed = make_offline_analysis(facts)['page_plan']
        compact = json.loads(build_weekly_compact_analysis_prompt(facts, fixed, '2026/10/19—2026/10/25'))
        self.assertEqual(len(fixed), len(compact['fixed_page_plan']))
        self.assertNotIn('page_plan', compact['required_json_schema'])
        self.assertLess(len(json.dumps(compact, ensure_ascii=False)), 6500)

    def test_screenshot_facts_routing_and_context(self):
        facts=extract_weekly_facts(SOURCE)
        self.assertEqual(14,len(facts))
        self.assertEqual('context',facts[4]['category'])
        self.assertEqual('operations',facts[5]['category'])
        self.assertTrue(facts[13]['needs_confirmation'])
        result, errors=normalize_weekly_analysis(payload(),facts)
        self.assertEqual([],errors)
        self.assertIn('F006',result['page_plan'][2]['fact_ids'])
        self.assertIn('F014',result['page_plan'][5]['fact_ids'])
        self.assertNotIn('F005',result['unassigned_fact_ids'])
        self.assertEqual([],app._critical_fact_coverage_issues(facts,result))
        self.assertTrue(result['next_week'][0]['needs_confirmation'])
        offline = make_offline_analysis(facts)
        self.assertTrue(offline['next_week'][-1]['text'].startswith('待确认候选计划：'))
        self.assertIn('岗位管控更换', offline['next_week'][-1]['text'])
        issues=weekly_page_generation_issues('NPI架设缺失跟进；岗位管控更换',result['page_plan'][5],facts,result)
        self.assertTrue(any('候选计划' in i for i in issues))

    def test_equivalent_hour_units_and_masked_count_are_normalized_safely(self):
        self.assertEqual([(0.71, 'hours'), (0.71, 'hours')], extract_quantities('0.71h；0.71小时'))
        facts = extract_weekly_facts('日均通行数量166x\n垃圾检查工时0.71h')
        restored = restore_masked_quantity_markers('日均通行数量166count；垃圾检查工时0.71小时', facts)
        self.assertEqual('日均通行数量166x；垃圾检查工时0.71小时', restored)
        page = {'page': 4, 'page_type': 'operations', 'fact_ids': ['F001', 'F002']}
        self.assertEqual([], weekly_page_generation_issues(restored, page, facts, {'verified_calculations': []}))

    def test_pipeline_accepts_model_unit_aliases_without_weakening_fact_gate(self):
        bodies=['楼栋3x，岗位3x，人力5x','日均通行数量166count；较上周增加2count；垃圾检查工时0.71小时','稽核异常较上周增加2count；专区异常较上周减少1count','完成现场跟进、视察接待和参观接待保障','NPI架设缺失跟进；待确认候选计划：岗位管控更换']
        calls=[{'status':'ok','output':json.dumps(payload(),ensure_ascii=False)}]+[{'status':'ok','output':b} for b in bodies]
        with patch.object(app.guard,'call_llm',side_effect=calls):
            report,_,tip,state=app.structure_ppt_source_ui(SOURCE, '2026/10/19—2026/10/25')
        self.assertEqual('completed', state['generation']['status'], tip)
        self.assertIn('日均通行数量166x', report)
        self.assertIn('垃圾检查工时为0.71h', report)
        self.assertNotIn('166count', report)

        altered = report.replace('日均通行数量166x', '日均通行数量999次')
        self.assertTrue(any('999' in issue for issue in app.validate_report_grounding(altered, state['facts'], state['analysis'])))

    def test_operations_can_be_assigned_to_anomalies(self):
        facts=extract_weekly_facts('运营数据查获2次')
        obj={'page_plan':[{'page':1,'page_type':'cover'}, {'page':2,'page_type':'anomalies','fact_ids':[]}, {'page':3,'page_type':'closing'}]}
        result,errors=normalize_weekly_analysis(obj,facts)
        self.assertEqual([],errors)
        self.assertEqual(['F001'],result['page_plan'][1]['fact_ids'])

    def test_all_critical_categories_are_routed_and_existing_ids_are_not_missing(self):
        facts = extract_weekly_facts(SOURCE)
        candidate = payload()
        for page in candidate['page_plan']:
            page['fact_ids'] = []
        candidate['missing_info'] = ['F008', 'F009、F010、F011、F012', '缺少异常原因说明']
        result, errors = normalize_weekly_analysis(candidate, facts)
        self.assertEqual([], errors)
        page_ids = {page['page_type']: page['fact_ids'] for page in result['page_plan']}
        self.assertEqual(['F001', 'F002', 'F003'], page_ids['manpower'])
        self.assertEqual(['F004', 'F006', 'F007'], page_ids['operations'])
        self.assertEqual(['F008', 'F009'], page_ids['anomalies'])
        self.assertEqual(['F010', 'F011', 'F012'], page_ids['key_work'])
        self.assertEqual(['F013', 'F014'], page_ids['next_plan'])
        self.assertEqual(['缺少异常原因说明'], result['missing_info'])
        self.assertNotIn('F005', result['unassigned_fact_ids'])
        self.assertEqual([], app._critical_fact_coverage_issues(facts, result))

    def test_invalid_json_reason_is_not_connection_failure(self):
        with patch.object(app.guard,'call_llm',return_value={'status':'ok','output':'not json'}):
            report,_,tip,state=app.structure_ppt_source_ui(SOURCE)
        self.assertEqual('',report)
        self.assertIn('[non_json]',tip)
        self.assertNotIn('请检查 Ollama',tip)
        self.assertEqual('failed',state['generation']['status'])

    def test_schema_failure_is_distinct(self):
        with patch.object(app.guard,'call_llm',return_value={'status':'ok','output':'{}'}):
            _,_,tip,state=app.structure_ppt_source_ui(SOURCE)
        self.assertIn('[schema]',tip)
        self.assertFalse(state['generation']['final_body_generated'])

    def test_progress_and_final_success(self):
        facts=extract_weekly_facts(SOURCE)
        bodies=['楼栋3x，岗位3x，人力5x','日均通行数量166x；较上周增加2x；垃圾检查工时0.71h','稽核异常较上周增加2x；专区异常较上周减少1x','完成现场跟进、视察接待和参观接待保障','NPI架设缺失跟进；待确认候选计划：岗位管控更换']
        calls=[{'status':'ok','output':json.dumps(payload(),ensure_ascii=False)}]+[{'status':'ok','output':b} for b in bodies]
        with patch.object(app.guard,'call_llm',side_effect=calls):
            updates=list(app.stream_structure_ppt_source_ui(SOURCE))
        self.assertIn(updates[-1][3]['generation']['status'], {'completed', 'needs_review'}, updates[-1][2])
        # 首条是“已接收素材”的占位更新，还没有页数；只核验引擎真正推进的进度。
        progress = [u[3]['generation']['completed_pages'] for u in updates[:-1]
                    if 'completed_pages' in u[3].get('generation', {})]
        self.assertEqual(list(range(progress[-1] + 1)), progress)
        self.assertIn('8/8',updates[-2][2])
        for update in updates[:-1]:
            self.assertIn('才可导出',app._weekly_grounding_warning(update[0],update[3]))

    def test_evidence_compiler_ignores_freeform_page_outputs(self):
        bodies = [
            '楼栋3x，岗位3x，人力5x；请补充数据',
            '日均通行数量166x；较上周增加2x；垃圾检查工时0.71h',
            '稽核异常较上周增加2x；专区异常较上周减少1x',
            '完成现场跟进、视察接待和参观接待保障',
            'NPI架设缺失跟进；待确认候选计划：岗位管控更换',
        ]
        calls = ([{'status':'ok','output':json.dumps(payload(),ensure_ascii=False)}]
                 + [{'status':'ok','output':body} for body in bodies])
        with patch.object(app.guard, 'call_llm', side_effect=calls) as model:
            report, _, tip, state = app.structure_ppt_source_ui(SOURCE, '2026/10/19—2026/10/25')
        self.assertEqual('completed', state['generation']['status'], tip)
        self.assertEqual([], state['generation']['auto_repaired_pages'])
        self.assertEqual([], state['generation']['quality_issues'])
        self.assertNotIn('请补充数据', report)
        self.assertIn('证据编译', tip)
        self.assertEqual(1, model.call_count)

    def test_missing_period_is_reported_as_user_input_not_blindly_repaired(self):
        bodies=['楼栋3x，岗位3x，人力5x','日均通行数量166x；较上周增加2x；垃圾检查工时0.71h','稽核异常较上周增加2x；专区异常较上周减少1x','完成现场跟进、视察接待和参观接待保障','NPI架设缺失跟进；待确认候选计划：岗位管控更换']
        calls=[{'status':'ok','output':json.dumps(payload(),ensure_ascii=False)}]+[{'status':'ok','output':b} for b in bodies]
        with patch.object(app.guard,'call_llm',side_effect=calls) as model:
            _,_,tip,state=app.structure_ppt_source_ui(SOURCE)
        self.assertEqual('needs_review', state['generation']['status'])
        self.assertIn('P1「封面」统计周期未确认', tip)
        self.assertIn('程序不会自行猜测日期', tip)
        self.assertEqual([], state['generation']['auto_repaired_pages'])
        self.assertEqual(1, model.call_count)

    def test_no_per_page_model_call_is_required(self):
        results=[{'status':'ok','output':json.dumps(payload())},RuntimeError('[timeout] 模型服务请求超时')]
        with patch.object(app.guard,'call_llm',side_effect=results) as model:
            updates=list(app.stream_structure_ppt_source_ui(SOURCE, '2026/10/19—2026/10/25'))
        last=updates[-1]
        self.assertEqual('completed',last[3]['generation']['status'],last[2])
        self.assertTrue(last[0])
        self.assertEqual(1, model.call_count)
        self.assertEqual(5, last[3]['generation']['evidence_compiled_pages'])

    def test_v8_exposes_trace_lineage_and_validation_matrix(self):
        with patch.object(app.guard, 'call_llm', return_value={'status':'ok','output':json.dumps(payload(),ensure_ascii=False)}):
            report, _, tip, state = app.structure_ppt_source_ui(SOURCE, '2026/10/19—2026/10/25')
        generation = state['generation']
        self.assertEqual('completed', generation['status'], tip)
        self.assertEqual(['素材理解', '第一阶段：AI 语义分析', '第二阶段：事实证据编译', '第三阶段：结构、来源与质量核验'], [row['stage'] for row in generation['trace']])
        self.assertEqual('evidence_compiler', generation['lineage']['P4']['compiler'])
        self.assertEqual(['F004', 'F006', 'F007'], generation['lineage']['P4']['fact_ids'])
        self.assertTrue(generation['validation_matrix']['structure_valid'])
        self.assertTrue(generation['validation_matrix']['grounding_valid'])
        self.assertTrue(generation['validation_matrix']['quality_valid'])
        self.assertGreaterEqual(generation['quality_score'], 80)
        self.assertIn('V8 Agent', tip)
        self.assertIn('166x', report)

class ProviderRepairTests(unittest.TestCase):
    def test_ollama_json_mode_is_sent_to_api(self):
        captured = {}

        def fake_post(url, body, headers, timeout):
            captured.update(body)
            return {'message': {'content': '{}'}}

        with patch.object(providers, '_post_json', side_effect=fake_post):
            self.assertEqual('{}', providers.ollama_llm('test', json_mode=True))
        self.assertEqual('json', captured['format'])

    def test_preflight_model_found(self):
        with patch.object(providers.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps({'models':[{'name':providers.settings.ollama_model}]}).encode())) as call:
            self.assertEqual('ollama',providers.check_weekly_model()['provider'])
        self.assertTrue(call.call_args.args[0].endswith('/api/tags'))

    def test_preflight_missing_model(self):
        with patch.object(providers.urllib.request,'urlopen',return_value=io.BytesIO(b'{"models":[]}')):
            with self.assertRaisesRegex(providers.ModelProviderError,'model_missing'):
                providers.check_weekly_model()

    def test_preflight_invalid_schema(self):
        with patch.object(providers.urllib.request,'urlopen',return_value=io.BytesIO(b'[]')):
            with self.assertRaisesRegex(providers.ModelProviderError,'schema'):
                providers.check_weekly_model()

    def test_transport_errors(self):
        errors=[(TimeoutError(),'timeout'),(urllib.error.URLError(TimeoutError()),'timeout'),(urllib.error.URLError(ConnectionRefusedError()),'connection'),(urllib.error.HTTPError('http://localhost',500,'error',{},None),'http')]
        for exc,kind in errors:
            with self.subTest(kind=kind),patch.object(providers.urllib.request,'urlopen',side_effect=exc):
                with self.assertRaisesRegex(providers.ModelProviderError,kind):
                    providers._post_json('http://localhost',{}, {},1)

    def test_transport_non_json(self):
        with patch.object(providers.urllib.request,'urlopen',return_value=io.BytesIO(b'<html>error</html>')):
            with self.assertRaisesRegex(providers.ModelProviderError,'non_json'):
                providers._post_json('http://localhost',{}, {},1)

    def test_empty_content_is_not_success(self):
        with patch.object(providers,'_post_json',return_value={'message':{'content':None}}):
            with self.assertRaisesRegex(providers.ModelProviderError,'schema'):
                providers.ollama_llm('test')

    def test_preflight_runs_inside_guard_before_generation(self):
        with patch.object(app,'check_weekly_model',side_effect=providers.ModelProviderError('[model_missing] test')) as check,patch.object(app,'configured_llm') as llm:
            _,_,tip,state=app.structure_ppt_source_ui(SOURCE)
        check.assert_called_once()
        llm.assert_not_called()
        self.assertIn('[model_missing]',tip)

if __name__=='__main__':
    unittest.main()
