"""对两个源代码目录运行同一确定性语料，对比返回值及异常类型/消息。

用法：python scripts/verify_refactor_compat.py 原版目录 重构目录
只导入解析模块，不导入 app、不连接模型、不访问业务数据库。
"""
from __future__ import annotations
import argparse
import copy
import itertools
import json
from pathlib import Path
import random
import subprocess
import sys


def snapshot(root):
    sys.path.insert(0, str(Path(root).resolve()))
    from core import ppt_export as export, ppt_template as template, ppt_pipeline as pipeline
    from core.weekly_report_agent import _visible_ppt_body
    rng = random.Random(20261026)
    lines = ['正文', '', '图表数据：', '表格数据:', 'chart data:', 'table data',
             '类别|本周|上周', '巡检|3|2', 'Key Message: 结论', 'keymessage：',
             '## P3', 'CHART_JSON:', '{}', 'END_CHART_JSON', '  保留空格  ']
    corpus = ['', '正文', '\n', '图表数据\n\n类别|数量\n巡检|3',
              '图表数据：\n类别|数量\n巡检|3\n表格数据\n项目|值']
    corpus += ['\n'.join(rng.choices(lines, k=rng.randrange(1, 18))) for _ in range(600)]
    corpus += ['\n'.join(x) for x in itertools.product(['图表数据:', 'chart data:', '表格数据：'],
                  ['类别|数量\n巡检|3', '类别,数量\n巡检,3', '类别\t数量\n巡检\t3', '类别|数量\n巡检|不明'],
                  ['', '\nKey Message: 结论', '\n\n后文', '\n## P3'])]
    out = []
    def capture(name, fn, value):
        try:
            result = {'value': fn(value)}
        except Exception as exc:
            result = {'error': type(exc).__name__, 'message': str(exc)}
        out.append({'function': name, 'input': value, 'result': result})
    functions = [('_visible_body', export._visible_body),
                 ('_strip_structured_blocks', template._strip_structured_blocks),
                 ('_visible_ppt_body', _visible_ppt_body),
                 ('_extract_block', lambda body: template._extract_block(body, 'chart')),
                 ('_parse_chart_data', template._parse_chart_data),
                 ('strip_json', pipeline.strip_json)]
    for body in corpus:
        for name, fn in functions:
            capture(name, fn, body)
    reports = ['', '无页面', '## P1 [type:cover] | 封面\n封面\n日期\n## P2 | 正文\n巡检3次',
               '---\nmeta: value\n---\n```\n## P1\n封面\n~~~',
               '【P1】\n标题\n正文\n第2页\n下页\n正文']
    for body in corpus[:100]:
        reports.append('## P1 [type:summary] | 概要\n' + body)
    spec = {'type': 'column', 'unit': '次', 'categories': ['巡检'], 'series': [{'name': '本周', 'values': [3]}]}
    specs = [None, [], {}, spec]
    for key, values in {'type': ['pie','bar','line','scatter'], 'categories': [[],[''],[1]],
                         'unit': [None, 3], 'series': [[], [{}], [{'name':'', 'values':[1]}]]}.items():
        for value in values:
            changed = copy.deepcopy(spec); changed[key] = value; specs.append(changed)
    for value in [True, None, '3', -1, 0, 3.2]:
        changed = copy.deepcopy(spec); changed['series'][0]['values'] = [value]; specs.append(changed)
    for chart in specs:
        capture('validate_chart', pipeline.validate_chart, chart)
        reports.append(json.dumps({'pages': [{'title': '测试', 'body': '正文', 'chart': chart}]}, ensure_ascii=False))
    reports += ['{', '{}', '{"pages": null}']
    for report in reports:
        capture('parse_report', export.parse_report, report)
    for page in [{'title':'标题','body':'标题\n正文'}, {'title':'标题','body':'# 标题\n正文'}, {}, {'title':'标题','body':'其他'}]:
        capture('split_title_body', export.split_title_body, page)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline')
    parser.add_argument('refactored', nargs='?')
    parser.add_argument('--snapshot', action='store_true')
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(snapshot(args.baseline), ensure_ascii=False, sort_keys=True))
        return
    if not args.refactored:
        parser.error('请提供原版与重构目录')
    outputs = []
    for root in (args.baseline, args.refactored):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(Path(root).resolve()), '--snapshot'],
                                check=True, capture_output=True, text=True)
        outputs.append(json.loads(result.stdout))
    before, after = outputs
    mismatches = [i for i, (a,b) in enumerate(zip(before, after)) if a != b]
    success = not mismatches and len(before) == len(after)
    print(json.dumps({'baseline_cases': len(before), 'refactored_cases': len(after),
                      'mismatch_count': len(mismatches), 'equal_length': len(before) == len(after),
                      'status': 'PASS' if success else 'FAIL', 'first_mismatch_indices': mismatches[:10]},
                     ensure_ascii=False, indent=2))
    if not success:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
