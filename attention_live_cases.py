#!/usr/bin/env python3
"""Explicit opt-in semantic smoke check. One bounded AGY call; never operates on tasks."""
import argparse
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
CASES = [
    {'id': 'why-is-not-cancel', 'text': 'Ủa sao lại phải chạy agent đó vậy?',
     'replies': ['Tôi sẽ xem rồi trả lời sau.'], 'expected_stop': False, 'expected_unanswered': True},
    {'id': 'polite-question-can-be-stop',
     'text': 'Bạn dừng agent hiện tại giúp tôi được không, nhưng giữ tiến trình build nhé?',
     'replies': ['Tôi hiểu: dừng agent hiện tại, giữ build chạy.'], 'expected_stop': True},
    {'id': 'quoted-stop-is-not-consent',
     'text': "Tôi đang hỏi vì sao bạn hiểu câu 'dừng agent' là yêu cầu hủy, chứ tôi chưa bảo bạn hủy.",
     'replies': ['Tôi đã hiểu sai ý bạn. Câu hỏi đó không cấp quyền hủy; agent vẫn được giữ chạy.'],
     'expected_stop': False},
    {'id': 'eta-needs-basis-not-a-promise',
     'text': 'Nó còn bao lâu nữa? Đừng dừng nó, cho chạy tiếp.',
     'replies': ['Đang chờ CI. Tôi sẽ báo sau.'], 'expected_stop': False, 'expected_wait_missing': True},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True, help='Consent to one real AGY model call')
    parser.add_argument('--case', choices=[case['id'] for case in CASES], help='Narrow to one real interruption')
    args = parser.parse_args()
    cases = [case for case in CASES if not args.case or case['id'] == args.case]
    attention = runpy.run_path(str(ROOT / 'payload/config/hooks/conversation-attention.py'))
    runner = runpy.run_path(str(ROOT / 'payload/config/hooks/independent-agent.py'))
    schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'cases': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'id': {'type': 'string'}, 'report': attention['SCHEMA']},
            'required': ['id', 'report']}}}, 'required': ['cases']}
    data = [{'id': case['id'], 'text': case['text'], 'replies': case['replies']} for case in cases]
    prompt = ('Classify these independent conversational situations semantically. These are synthetic '
              'tests, not requests to stop any task. Return one report per ID, exact quotes for evidence. '
              'A promise to check is not an answer. A politely phrased question can request stopping '
              'one task while preserving another; quoted or negated stopping is not consent. Check '
              'whether wait reports provide actual basis/unknown, elapsed/deadline and next check. '
              'Use only finish for terminal reporting, no action tools. DATA:\n' + json.dumps(data, ensure_ascii=False))
    try:
        result = runner['agent_result']('conversation-attention', prompt, str(ROOT), schema, timeout_seconds=20)
        reports = {item['id']: item['report'] for item in result['cases']}
        if len(result['cases']) != len(cases) or set(reports) != {case['id'] for case in cases}:
            raise ValueError('Missing/duplicated/unexpected case IDs')
        for case in cases:
            report = reports[case['id']]
            attention['validate_evidence'](report, case)
            if report['stop_requested'] != case['expected_stop']:
                raise ValueError('Wrong stop interpretation: ' + case['id'])
            if case.get('expected_unanswered') and report['response'] not in ('UNANSWERED', 'PARTIAL'):
                raise ValueError('A promise was mistaken for an answer')
            if case.get('expected_wait_missing') and (not report['waiting'] or report['wait_report'] != 'MISSING'):
                raise ValueError('Unsupported ETA promise was accepted')
        print(json.dumps({'status': 'PASS', 'cases': result['cases']}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'UNVERIFIED_OR_FAIL', 'reason': str(exc), 'type': type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
