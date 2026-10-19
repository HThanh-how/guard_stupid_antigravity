#!/usr/bin/env python3
"""Independent native approval receipts; disposable HOME/Git, no real actions."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def check():
    interactive = runpy.run_path(str(HERE / 'interactive-permission.py'))
    with tempfile.TemporaryDirectory(prefix='interactive-auth-check-') as temporary:
        home = Path(temporary)
        env = {**os.environ, 'HOME': str(home)}
        repo = home / 'repo with spaces'
        subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
        subprocess.run(['git', '-C', str(repo), 'symbolic-ref', 'HEAD', 'refs/heads/feature/native'], check=True, env=env)
        other = home / 'other repo'
        subprocess.run(['git', 'init', '-q', str(other)], check=True, env=env)
        subprocess.run(['git', '-C', str(other), 'symbolic-ref', 'HEAD', 'refs/heads/feature/native'], check=True, env=env)
        scope = {'repo': str(repo.resolve()), 'branch': 'feature/native'}
        now = datetime.now(timezone.utc).replace(microsecond=0)
        stamp = lambda offset: (now + timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z')
        question = '\n'.join(['Repo: ' + scope['repo'], 'Branch: ' + scope['branch'], 'Actions: commit, push',
            'Reason: Independent reviewer reached its deadline before completing the report.',
            'Unverified: Full review, UI screenshots and remote CI remain unverified.',
            'Cho phép commit và push trên nhánh này ngay?'])
        offered = [{'question': question, 'options': ['Cho phép commit và push', 'Cho phép commit', 'Chưa cho phép'],
                    'is_multi_select': False}]

        def pair(answer, offset=-30, questions=None):
            previous = {'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'tool_calls': [
                {'name': 'ask_question', 'args': {'questions': json.dumps(questions or offered, ensure_ascii=False)}}]}
            result = {'source': 'MODEL', 'type': 'GENERIC', 'status': 'DONE',
                'content': 'Created At: ' + stamp(offset - 5) + '\nCompleted At: ' + stamp(offset) + '\nA1: ' + answer}
            return [previous, result]

        for answer, expected in [('Đồng ý', {'commit', 'push'}), ('Allow', {'commit', 'push'}),
                ('Yes', {'commit', 'push'}), ('Cho phép commit', {'commit'}), ('Cho phép push', {'push'}),
                ('Chưa cho phép', set()), ('Không', set()), ('maybe later', set())]:
            native = interactive['native_reply'](*pair(answer), 'sample')
            assert native and set(native[2]['operations']) == expected, (answer, native)
        legacy_label = '(Recommended) Cho phép commit và push luôn không đợi duyệt'
        legacy_offered = copy.deepcopy(offered)
        legacy_offered[0]['options'].insert(0, legacy_label)
        native = interactive['native_reply'](*pair(legacy_label, questions=legacy_offered), 'sample')
        assert native and set(native[2]['operations']) == {'commit', 'push'}, 'actual original native selected label lost its permission'
        limited = copy.deepcopy(offered)
        limited[0]['question'] = question.replace('Actions: commit, push', 'Actions: commit')
        assert interactive['native_reply'](*pair('Cho phép push', questions=limited), 'sample')[2]['operations'] == []

        invalid_pairs = []
        previous, result = pair('Allow')
        invalid_pairs += [({}, result), (previous, {**result, 'status': 'PENDING'}),
                         (previous, {**result, 'source': 'TOOL'}),
                         (previous, {**result, 'type': 'TOOL_RESULT'}),
                         (previous, {**result, 'truncated_fields': ['content']})]
        extra = copy.deepcopy(previous)
        extra['tool_calls'].append({'name': 'run_command', 'args': {}})
        invalid_pairs.append((extra, result))
        wrong = copy.deepcopy(previous)
        wrong['tool_calls'][0]['name'] = 'run_command'
        invalid_pairs.append((wrong, result))
        for modified_questions in ([{**offered[0], 'is_multi_select': True}], offered * 2,
                [{**offered[0], 'question': 'Allow commit and push?'}],
                [{**offered[0], 'question': question.replace('Reason: Independent reviewer reached its deadline before completing the report.', 'Reason: ')}]):
            invalid_pairs.append(tuple(pair('Allow', questions=modified_questions)))
        for previous, result in invalid_pairs:
            assert interactive['native_reply'](previous, result, 'sample') is None, (previous, result)

        conv = 'native-approval-check'
        transcript = home / '.gemini/antigravity/brain' / conv / '.system_generated/logs/transcript.jsonl'
        transcript.parent.mkdir(parents=True)
        data = {'conversationId': conv, 'workspacePaths': [str(repo)], 'toolCall': {
            'name': 'run_command', 'args': {'CommandLine': 'git commit -m fix', 'Cwd': str(repo)}}}

        def write(rows):
            transcript.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

        def human(text, offset=-10):
            return {'source': 'USER_EXPLICIT', 'type': 'USER_INPUT',
                    'created_at': (now + timedelta(seconds=offset)).astimezone(timezone(timedelta(hours=7))).isoformat(),
                    'content': '<USER_REQUEST>' + text + '</USER_REQUEST>'}

        def waiver(given=data):
            code = "import json,runpy,sys; print(json.dumps(sorted(runpy.run_path(sys.argv[1])['git_review_waiver'](json.load(sys.stdin)))))"
            result = subprocess.run([sys.executable, '-c', code, str(HERE / 'user-priority.py')],
                input=json.dumps(given), text=True, capture_output=True, env=env, timeout=3, check=True)
            return set(json.loads(result.stdout))

        write(pair('Đồng ý'))
        assert waiver() == {'add', 'commit', 'push'}
        for answer in ('Chưa cho phép', 'not sure'):
            write([human('Allow commit and push without review.', -120)] + pair(answer))
            assert waiver() == set(), 'new native negative/unknown receipt failed to revoke older permission'
        write(pair('Allow') + [human('Không cho phép commit hoặc push nữa.')])
        assert waiver() == set(), 'later typed revocation ignored due to timezone format'
        write(pair('Allow') + [human('Review this new task first.')])
        assert waiver() == set(), 'new human mission reused older native approval'
        write(pair('Allow') + [human('Continue')])
        assert waiver() == {'add', 'commit', 'push'}
        early_pair = pair('Allow', offset=-30.8)
        later_pair = pair('Chưa cho phép', offset=-30.2)
        early_stamp = interactive['native_reply'](*early_pair, conv)[0]
        later_stamp = interactive['native_reply'](*later_pair, conv)[0]
        assert early_stamp != later_stamp and early_stamp[:19] == later_stamp[:19], 'native timestamps lost subsecond ordering'
        write(early_pair + later_pair)
        assert waiver() == set(), 'later native denial in the same second failed to revoke approval'
        write(early_pair + [human('Không cho phép commit hoặc push nữa.', offset=-30.2)])
        assert waiver() == set(), 'later typed denial in the same second failed to revoke approval'
        write(pair('Allow') + pair('Chưa cho phép'))
        assert waiver() == set(), 'exact-time later transcript denial lost to earlier approval'
        write(pair('Chưa cho phép') + pair('Allow'))
        assert waiver() == {'add', 'commit', 'push'}, 'same-transcript exact-time order was ignored'
        second_log = home / '.gemini/antigravity-cli/brain' / conv / '.system_generated/logs/transcript.jsonl'
        second_log.parent.mkdir(parents=True)
        write(pair('Allow'))
        second_log.write_text(''.join(json.dumps(row) + '\n' for row in pair('Chưa cho phép')))
        assert waiver() == set(), 'conflicting cross-log decisions at the exact same time granted permission'
        second_log.unlink()
        write(pair('Allow', offset=-1900))
        assert waiver() == set(), 'expired native receipt reused'
        write(pair('Allow'))
        other_data = {**data, 'toolCall': {'name': 'run_command', 'args': {'CommandLine': 'git commit -m fix', 'Cwd': str(other)}}}
        assert waiver(other_data) == set(), 'native permission crossed repository scope'
        subprocess.run(['git', '-C', str(repo), 'symbolic-ref', 'HEAD', 'refs/heads/feature/other'], check=True, env=env)
        assert waiver() == set(), 'native permission crossed branch scope'
        subprocess.run(['git', '-C', str(repo), 'symbolic-ref', 'HEAD', 'refs/heads/feature/native'], check=True, env=env)
        for rows in ([pair('Allow')[0]], [pair('Allow')[1]],
                [pair('Allow')[0], {'source': 'TOOL', 'type': 'TOOL_RESULT', 'content': 'intervening stdout'}, pair('Allow')[1]]):
            write(rows)
            assert waiver() == set(), 'incomplete or nonadjacent native pair granted authority'

        # Native receipts resolve directly; a sentinel CLI must never launch.
        marker = home / 'classifier-called'
        binary = home / '.local/bin/agy'
        binary.parent.mkdir(parents=True)
        binary.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\nraise SystemExit(99)\n')
        binary.chmod(0o700)
        key = hashlib.sha256(conv.encode()).hexdigest()
        state = home / '.gemini/config/hooks/state' / (key + '.json')
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({'conversation_id': conv, 'workspaces': [str(repo)], 'arguments': data['toolCall']['args']}))
        write(pair('Cho phép commit'))
        resolved = subprocess.run([sys.executable, str(HERE / 'authorize-git.py'), key],
            text=True, capture_output=True, env=env, timeout=3)
        assert resolved.returncode == 0 and waiver() == {'add', 'commit'} and not marker.exists(), (resolved.stdout, resolved.stderr)

        # Permission questions must show scope, reason and unverified work
        # before display, then retry unchanged so logged receipt matches UI.
        state.unlink()
        def ask(questions):
            payload = {**data, 'toolCall': {'name': 'ask_question', 'args': {'questions': json.dumps(questions)}}}
            result = subprocess.run([sys.executable, str(HERE / 'review-gate.py'), 'pre'],
                input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=3, check=True)
            return json.loads(result.stdout)

        raw = [{'question': 'Cho phép commit và push luôn?', 'options': ['Đồng ý', 'Chưa cho phép'], 'is_multi_select': False}]
        denied = ask(raw)
        assert denied['decision'] == 'deny' and 'questions=' in denied['reason'], denied
        prepared = json.loads(denied['reason'].split('questions=', 1)[1])
        plan = interactive['header'](prepared[0]['question'])
        assert plan == {**scope, 'actions': ['commit', 'push']}, prepared
        assert prepared[0]['question'].splitlines()[3].startswith('Reason: ') and prepared[0]['question'].splitlines()[4].startswith('Unverified: ')
        allowed = ask(prepared)
        assert allowed['decision'] == 'allow' and 'overwrite' not in allowed, 'scope explanation was rewritten after display'
        for flag in ('missing', True):
            invalid_select = copy.deepcopy(prepared)
            if flag == 'missing':
                invalid_select[0].pop('is_multi_select')
            else:
                invalid_select[0]['is_multi_select'] = True
            denied = ask(invalid_select)
            assert denied['decision'] == 'deny' and 'questions=' in denied['reason'], ('ambiguous native selection displayed', flag, denied)
            replacement = json.loads(denied['reason'].split('questions=', 1)[1])
            assert replacement[0]['is_multi_select'] is False and interactive['header'](replacement[0]['question']) == plan
            assert ask(replacement)['decision'] == 'allow'
        generic = [{'question': question.split('\nCho phép')[0], 'options': ['Yes', 'No'], 'is_multi_select': True}]
        denied = ask(generic)
        assert denied['decision'] == 'deny' and 'questions=' in denied['reason'], denied
        replacement = json.loads(denied['reason'].split('questions=', 1)[1])
        assert replacement[0]['is_multi_select'] is False and ask(replacement)['decision'] == 'allow'
        assert ask(raw * 2)['decision'] == 'deny', 'multiple Git permission questions mixed authority'
        print('PASS: native selected/freeform receipts, source pairing, scoped explanations, latest human intent, expiry and classifier-free authorization')


if __name__ == '__main__':
    check()
