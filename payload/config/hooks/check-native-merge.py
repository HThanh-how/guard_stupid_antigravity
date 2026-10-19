#!/usr/bin/env python3
"""Native merge receipts and scope recovery: temporary Git, fake gh, no publication."""
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
    with tempfile.TemporaryDirectory(prefix='native-merge-check-') as temporary:
        home = Path(temporary).resolve()
        container = home / 'projects'
        container.mkdir()
        env = {**os.environ, 'HOME': str(home), 'GIT_CONFIG_GLOBAL': '/dev/null',
               'GIT_CONFIG_NOSYSTEM': '1'}
        env.pop('AGY_GUARD_ROLE', None)

        def git(repo, *args):
            return subprocess.check_output(['git', '-C', str(repo), *args],
                env=env, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()

        branch = 'feature/native-merge'
        repo, other = container / 'actual repo', container / 'unrelated repo'
        for checkout in (repo, other):
            checkout.mkdir()
            git(checkout, 'init', '-q')
            git(checkout, 'checkout', '-qb', branch)
            git(checkout, '-c', 'user.name=Check', '-c', 'user.email=check@example.invalid',
                '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-qm', checkout.name)
            git(checkout, 'remote', 'add', 'origin', 'https://github.com/example/native-merge.git')
        head = git(repo, 'rev-parse', 'HEAD')
        scope = {'repo': str(repo), 'branch': branch, 'head': head}
        now = datetime.now(timezone.utc)

        def question(given_scope=scope, actions='merge, push'):
            return '\n'.join(['Repo: ' + given_scope['repo'], 'Branch: ' + given_scope['branch'],
                'Actions: ' + actions, 'Reason: Local review timed out; publication requested.',
                'Unverified: Local flow review remains incomplete.', 'Head: ' + given_scope['head']])

        def offered(text=None):
            return [{'question': text or question(),
                     'options': ['Cho phép merge và push', 'Chưa cho phép'], 'is_multi_select': False}]

        def pair(answer='Cho phép merge và push', age=10, questions=None):
            completed = (now - timedelta(seconds=age)).isoformat()
            return [{'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'tool_calls': [
                {'name': 'ask_question', 'args': {'questions': json.dumps(questions or offered(), ensure_ascii=False)}}]},
                {'source': 'MODEL', 'type': 'GENERIC', 'status': 'DONE',
                 'content': 'Created At: ' + completed + '\nCompleted At: ' + completed + '\nA1: ' + answer}]

        plan = interactive['header'](question())
        assert plan == {**scope, 'actions': ['merge', 'push']}, plan
        for invalid_head in ('', 'a' * 39, 'z' * 40):
            assert interactive['header'](question({**scope, 'head': invalid_head})) is None, invalid_head
        assert interactive['header']('\n'.join(question().splitlines()[:5])) is None, 'merge accepted without exact Head'
        receipt = interactive['native_reply'](*pair(), 'native-merge-check')
        assert receipt and set(receipt[2]['operations']) == {'merge', 'push'} and receipt[2]['head'] == head, receipt
        assert receipt[2]['scope'] == {'repo': str(repo), 'branch': branch}, receipt
        assert abs(receipt[2]['expires'] - (now.timestamp() + 1790)) < 0.001, 'expiry is not bound to native completion'
        for answer, expected in [('Cho phép merge', {'merge'}), ('Cho phép push', {'push'}),
                                 ('Chưa cho phép', set()), ('maybe', set())]:
            selected = interactive['native_reply'](*pair(answer), 'native-merge-check')
            assert selected and set(selected[2]['operations']) == expected, (answer, selected)
        for field, value in [('source', 'TOOL'), ('type', 'TOOL_RESULT'), ('status', 'PENDING'),
                             ('truncated_fields', ['content'])]:
            previous, result = pair()
            result[field] = value
            assert interactive['native_reply'](previous, result, 'native-merge-check') is None, field
        previous, result = pair()
        previous['tool_calls'][0]['name'] = 'run_command'
        assert interactive['native_reply'](previous, result, 'native-merge-check') is None

        # The real guard may query this fake gh for identity/CI only. It can
        # neither contact a service nor execute a merge through this fixture.
        binary = home / 'bin/gh'
        binary.parent.mkdir()
        payload = home / 'pr.json'
        payload.write_text(json.dumps({'headRefOid': head, 'headRefName': branch,
            'url': 'https://github.com/example/native-merge/pull/12', 'number': 12,
            'state': 'OPEN', 'mergeable': 'MERGEABLE',
            'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}))
        binary.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\n'
            "assert sys.argv[1:3] == ['pr', 'view'], 'unexpected gh operation'\n"
            "print(Path(os.environ['FAKE_PR']).read_text())\n")
        binary.chmod(0o700)
        env.update(PATH=str(binary.parent) + os.pathsep + env['PATH'], FAKE_PR=str(payload))
        conv = 'native-merge-check'
        transcript = home / '.gemini/antigravity/brain' / conv / '.system_generated/logs/transcript.jsonl'
        transcript.parent.mkdir(parents=True)
        state_path = home / '.gemini/config/hooks/state' / (hashlib.sha256(conv.encode()).hexdigest() + '.json')
        state_path.parent.mkdir(parents=True)

        def write(rows):
            transcript.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

        def gate(tool, args):
            data = {'conversationId': conv, 'workspacePaths': [str(container)],
                    'toolCall': {'name': tool, 'args': args}}
            result = subprocess.run([sys.executable, str(HERE / 'review-gate.py'), 'pre'],
                input=json.dumps(data), text=True, capture_output=True, env=env, timeout=5, check=True)
            return json.loads(result.stdout)

        def ask(questions):
            return gate('ask_question', {'questions': json.dumps(questions, ensure_ascii=False)})

        # An explicit real repository in a complete offer wins over a container
        # workspace and over unrelated saved review scope.
        assert ask(offered())['decision'] == 'allow', 'parent container hid explicit Repo'
        state_path.write_text(json.dumps({'version': 2, 'kind': 'comprehensive', 'status': 'blocked',
            'workspaces': [str(other)], 'arguments': {'CommandLine': 'git add .', 'Cwd': str(other)},
            'failure': 'Unrelated earlier gate'}))
        assert ask(offered())['decision'] == 'allow', 'unrelated state overrode explicit Repo'
        wrong_head = offered(question({**scope, 'head': '0' * 40}))
        denied = ask(wrong_head)
        assert denied['decision'] == 'deny' and 'questions=' in denied['reason'], denied
        repaired = json.loads(denied['reason'].split('questions=', 1)[1])
        assert interactive['header'](repaired[0]['question']) == plan, repaired
        assert ask(repaired)['decision'] == 'allow'

        state_path.unlink()
        command = 'gh pr merge 12 --squash --match-head-commit ' + head
        args = {'CommandLine': command, 'Cwd': str(repo)}
        denied = gate('run_command', args)
        assert denied['decision'] == 'deny', 'merge proceeded without review or native grant'
        saved = json.loads(state_path.read_text())
        assert saved['approval_arguments'] == args and saved['approval_workspaces'] == [str(container)], saved
        raw = [{'question': 'Cho phép merge và push không chờ review?',
                'options': ['Đồng ý', 'Chưa cho phép'], 'is_multi_select': False}]
        repaired = ask(raw)
        assert repaired['decision'] == 'deny' and 'questions=' in repaired['reason'], repaired
        explained = json.loads(repaired['reason'].split('questions=', 1)[1])
        assert interactive['header'](explained[0]['question']) == plan, explained
        assert ask(explained)['decision'] == 'allow'

        write(pair(questions=explained))
        assert gate('run_command', args)['decision'] == 'allow', 'head-bound native merge approval was lost'
        for rows, reason in [(pair(age=1900), 'expired'),
                             (pair(questions=wrong_head), 'wrong head'),
                             (pair('Cho phép push'), 'push-only')]:
            write(rows)
            assert gate('run_command', args)['decision'] == 'deny', reason
        previous, result = pair()
        write([previous, {'source': 'TOOL', 'type': 'TOOL_RESULT', 'content': 'unrelated output'}, result])
        assert gate('run_command', args)['decision'] == 'deny', 'nonadjacent tool output became human approval'
        write(pair() + [{'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'created_at': now.isoformat(),
                         'content': '<USER_REQUEST>Không merge nữa.</USER_REQUEST>'}])
        assert gate('run_command', args)['decision'] == 'deny', 'new typed revocation lost to native approval'

        # Snapshot capture must remain available to test review on a detached
        # checkout; publication policy handles branch restrictions separately.
        git(repo, 'checkout', '--detach', '-q', head)
        snapshot = runpy.run_path(str(HERE / 'independent-agent.py'))['snapshot_context']([str(repo)])
        captured = json.loads(snapshot)[0]
        assert captured['branch'] == 'HEAD' and captured['baseline'] == head, captured
        assert captured['working_diff'] == '' and captured['staged_diff'] == '', captured
        print('PASS: exact-head native merge receipts, source/expiry/revocation, explicit Repo scope, denied-command scope recovery and detached test snapshot')


if __name__ == '__main__':
    check()
