#!/usr/bin/env python3
"""Independent checks: disposable Git repositories, fake local CLI, no network."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

SCRIPT = Path(__file__).with_name('review-gate.py')


def check():
    with tempfile.TemporaryDirectory(prefix='agy-review-check-') as temporary:
        home = Path(temporary)
        repo = home / 'repo with spaces'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'source.txt').write_text('baseline\n')
        (repo / 'example.test.py').write_text('assert actual == expected\n')
        subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Check', '-c',
                        'user.email=check@example.invalid', 'commit', '-qm', 'baseline'], check=True)
        subprocess.run(['git', '-C', str(repo), 'checkout', '-qb', 'feature/smoke'], check=True)
        (repo / 'subdir').mkdir()
        tracked = repo / 'subdir/new.txt'
        tracked.write_text('untracked\n')
        baseline = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
        request_text = 'Review this plain-text fixture change against its original content before staging or committing.'
        request_id = hashlib.sha256(('antigravity:0:' + request_text).encode()).hexdigest()[:16]
        report = {'decision': 'PASS', 'baseline': baseline,
                  'files_reviewed': ['source.txt', 'example.test.py', 'subdir/new.txt'],
                  'findings': [], 'edge_cases': ['empty and boundary input'],
                  'test_integrity': 'LEGITIMATE', 'limitations': [],
                  'requirement_coverage': [{'request_id': request_id, 'requirement': request_text,
                      'status': 'VERIFIED', 'evidence': 'Compared source.txt baseline and complete working/index/untracked fixture snapshot.'}],
                  'consequence_checks': [
                      {'category': 'data-lifecycle', 'status': 'NOT_APPLICABLE',
                       'invariant': 'Plain-text fixture has no persisted asset ownership or deletion behavior.',
                       'scenario': 'Read and compare source.txt; no asset, collection, database or cleanup code is changed.',
                       'evidence': 'Fixture contents and reviewed diff contain only inert text and a synthetic assertion.'},
                      {'category': 'async-order', 'status': 'NOT_APPLICABLE',
                       'invariant': 'Plain-text fixture cannot accept an asynchronous response or mutate application state.',
                       'scenario': 'The fixture contains no requests, callbacks, promises, cancellation or concurrent state.',
                       'evidence': 'Inspected source.txt and synthetic test; neither implements an async operation.'}]}
        report_file, log = home / 'report.json', home / 'invocations.jsonl'
        binary = home / '.local/bin/agy'
        binary.parent.mkdir(parents=True)
        binary.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
with open(os.environ['FAKE_LOG'], 'a') as stream:
    stream.write(json.dumps({{'args': sys.argv[1:], 'role': os.environ.get('AGY_GUARD_ROLE')}}) + '\\n')
mode = os.environ.get('FAKE_MODE', 'report')
if mode == 'exit': sys.exit(9)
if mode == 'empty': sys.exit(0)
if mode == 'malformed': print('not JSON'); sys.exit(0)
if mode == 'timeout-success': print('[agy] print timeout reached; returning partial output PRIVATE_STDERR_SENTINEL', file=sys.stderr)
if mode == 'mutate': Path(os.environ['FAKE_REPO'], 'subdir/new.txt').write_text('changed during review')
if mode == 'pause':
    Path(os.environ['HOME'], 'ready').touch()
    deadline = time.monotonic() + 2
    while not Path(os.environ['HOME'], 'release').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
envelope = {{'status': 'SUCCESS', 'structured_output': json.loads(Path(os.environ['FAKE_REPORT']).read_text())}}
if mode == 'denied-success': envelope['denied_actions'] = [{{'tool': 'write_to_file', 'reason': 'read only PRIVATE_DENIED_SENTINEL'}}]
if mode == 'success-response': envelope = {{'status': 'SUCCESS', 'response': json.dumps(envelope['structured_output'])}}
if mode == 'malformed-response': envelope = {{'status': 'SUCCESS', 'response': 'Review passed: ' + json.dumps(envelope['structured_output'])}}
if mode == 'fenced-response': envelope = {{'status': 'SUCCESS', 'response': '```json\\n' + json.dumps(envelope['structured_output']) + '\\n```'}}
if mode == 'invalid-response': envelope = {{'status': 'SUCCESS', 'response': '{{not json'}}
print(json.dumps(envelope))
''')
        binary.chmod(0o700)
        report_file.write_text(json.dumps(report))
        env = {**os.environ, 'HOME': str(home), 'FAKE_REPORT': str(report_file),
               'FAKE_LOG': str(log), 'FAKE_REPO': str(repo)}

        # Staged and unstaged layers must both be present without charging the
        # same large staged patch twice against the bounded review context.
        layers = home / 'snapshot layers'
        subprocess.run(['git', 'init', '-q', str(layers)], check=True, env=env)
        large_file = layers / 'large.txt'
        large_file.write_text('baseline\n')
        subprocess.run(['git', '-C', str(layers), 'add', '.'], check=True, env=env)
        subprocess.run(['git', '-C', str(layers), '-c', 'user.name=Check', '-c',
                        'user.email=check@example.invalid', 'commit', '-qm', 'baseline'], check=True, env=env)
        subprocess.run(['git', '-C', str(layers), 'update-ref', 'refs/remotes/origin/main', 'HEAD'], check=True, env=env)
        subprocess.run(['git', '-C', str(layers), 'symbolic-ref', 'refs/remotes/origin/HEAD',
                        'refs/remotes/origin/main'], check=True, env=env)
        staged_payload = 'STAGED_ONLY_MARKER\n' + ('x' * 1000 + '\n') * 100 + 'tail\n' * 10
        large_file.write_text(staged_payload)
        subprocess.run(['git', '-C', str(layers), 'add', 'large.txt'], check=True, env=env)
        large_file.write_text(staged_payload + 'UNSTAGED_ONLY_MARKER\n')
        snapshot_code = "import runpy,sys; print(runpy.run_path(sys.argv[1])['snapshot_context']([sys.argv[2]]))"
        snapshot_process = subprocess.run([sys.executable, '-c', snapshot_code,
            str(SCRIPT.with_name('independent-agent.py')), str(layers)],
            text=True, capture_output=True, env=env, timeout=8, check=True)
        assert 100000 < len(snapshot_process.stdout) < 160000, 'large staged-only patch was doubled or omitted'
        layered = json.loads(snapshot_process.stdout)[0]
        assert '\n+STAGED_ONLY_MARKER\n' in layered['staged_diff'] and '\n+STAGED_ONLY_MARKER\n' not in layered['working_diff'], layered.keys()
        assert '\n+UNSTAGED_ONLY_MARKER\n' in layered['working_diff'] and '\n+UNSTAGED_ONLY_MARKER\n' not in layered['staged_diff'], layered.keys()
        assert 'outgoing_diff' not in layered and 'outgoing_base' not in layered, 'non-push review included outgoing history'

        def call(event, command=None, tool='run_command', args=None, conv='sample', role=None, **extra):
            transcript = home / '.gemini/antigravity/brain' / conv / '.system_generated/logs/transcript.jsonl'
            if not transcript.exists():
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(json.dumps({'source': 'USER_EXPLICIT', 'type': 'USER_INPUT',
                    'created_at': '2026-10-17T00:00:00Z', 'content': '<USER_REQUEST>' + request_text + '</USER_REQUEST>'}) + '\n')
            payload = {'conversationId': conv, 'workspacePaths': [str(home)], **extra}
            if event == 'pre':
                payload['toolCall'] = {'name': tool, 'args': args if args is not None else
                                       {'CommandLine': command, 'Cwd': str(repo / 'subdir')}}
            process = subprocess.run([sys.executable, str(SCRIPT), event], input=json.dumps(payload),
                                     text=True, capture_output=True,
                                     env={**env, **({'AGY_GUARD_ROLE': role} if role else {})}, timeout=3, check=True)
            result = json.loads(process.stdout)
            assert not {'injectSteps', 'ephemeralMessage', 'terminationBehavior'} & result.keys(), result
            if event in ('dispatch', 'post'):
                assert result == {}, 'lifecycle hook must return an empty object, never injected steps'
            return result

        def dispatch(conv):
            assert call('dispatch', conv=conv) == {}
            state = json.loads(state_path(conv).read_text())
            denial = call('pre', args=state['arguments'], conv=conv)
            assert denial['decision'] == 'deny', 'preparation must not grant the original command'
            marker = 'Immediately run this standalone bounded read-only reviewer command: '
            assert marker in denial['reason'], denial
            worker, remainder = denial['reason'].split(marker, 1)[1].split(' . Working directory: ', 1)
            expected = ['python3', str(SCRIPT.with_name('independent-agent.py')), 'review',
                        hashlib.sha256(conv.encode()).hexdigest()]
            assert shlex.split(worker) == expected, denial
            cwd, _ = remainder.split('. Wait for the complete report', 1)
            assert cwd == state['workspaces'][0], denial
            return {'CommandLine': worker, 'Cwd': cwd}

        def execute(args, mode='report'):
            return subprocess.run(args['CommandLine'], shell=True, env={**env, 'FAKE_MODE': mode},
                                  text=True, capture_output=True, timeout=4)

        def state_path(conv):
            return home / '.gemini/config/hooks/state' / (hashlib.sha256(conv.encode()).hexdigest() + '.json')

        # The installed app rejects injected-step objects. Even errors and
        # blocked lifecycles must keep the same empty response contract.
        for event in ('dispatch', 'post'):
            assert call(event, conv='empty-lifecycle') == {}
        call('pre', 'pytest', conv='broken-state')
        state_path('broken-state').write_text('{not valid JSON')
        for event in ('dispatch', 'post'):
            assert call(event, conv='broken-state') == {}
        assert call('pre', 'pytest', conv='broken-state')['decision'] == 'deny'

        # The exact denial command must work immediately, before a separate
        # PostInvocation/dispatch arrives; waiting on an unstarted task is a bug.
        denial = call('pre', 'pytest', conv='direct-worker')
        assert denial['decision'] == 'deny'
        marker = 'Immediately run this standalone bounded read-only reviewer command: '
        worker, remainder = denial['reason'].split(marker, 1)[1].split(' . Working directory: ', 1)
        assert shlex.split(worker) == ['python3', str(SCRIPT.with_name('independent-agent.py')), 'review',
                                      hashlib.sha256(b'direct-worker').hexdigest()]
        assert json.loads(state_path('direct-worker').read_text())['status'] == 'pending'
        result = execute({'CommandLine': worker, 'Cwd': remainder.split('. Wait for the complete report', 1)[0]})
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert call('pre', 'pytest', conv='direct-worker')['decision'] == 'allow'
        assert call('pre', 'pytest', conv='direct-worker')['decision'] == 'deny'

        call('pre', 'pytest', conv='pending-expired')
        path = state_path('pending-expired')
        state = json.loads(path.read_text())
        assert state['status'] == 'pending' and state['deadline'] > time.time(), state
        state['deadline'] = time.time() - 1
        path.write_text(json.dumps(state))
        assert call('stop', conv='pending-expired').get('decision') != 'continue', 'unstarted review traps Stop'
        assert call('dispatch', conv='pending-expired') == {}
        assert call('pre', 'pytest', conv='pending-expired')['decision'] == 'deny'

        # Ending a turn truthfully with unfinished review must never force an
        # endless continuation. Yielding must not create permission to execute.
        for status in ('pending', 'queued', 'running'):
            for operation, command in (('test', 'pytest'), ('git', 'git add .')):
                conv = 'stop-unfinished-' + status + '-' + operation
                assert call('pre', command, conv=conv)['decision'] == 'deny'
                if status != 'pending':
                    dispatch(conv)
                path = state_path(conv)
                state = json.loads(path.read_text())
                state.update(status=status, deadline=time.time() + 60)
                if status == 'running':
                    state['attempts'] = 2
                path.write_text(json.dumps(state))
                attempts = state.get('attempts', 0)
                for _ in range(3):
                    stopped = call('stop', conv=conv)
                    assert stopped.get('decision') == 'allow' and stopped.get('reason'), (status, command, stopped)
                    unchanged = json.loads(path.read_text())
                    assert unchanged['status'] == status, ('Stop minted or changed review status', unchanged)
                    assert unchanged.get('attempts', 0) == attempts, ('Stop consumed another review attempt', unchanged)
                    assert call('pre', command, conv=conv)['decision'] == 'deny', 'Stop granted an unfinished review'

        # Old heuristic falsely queued a read of vitest.config.ts. Clear only
        # obsolete non-running test gates; never grant an actual future test.
        read_command = shlex.join(['python3', '-c', "open('apps/docs/vitest.config.ts').read()"])
        for status in ('pending', 'queued', 'blocked', 'running'):
            conv = 'obsolete-read-' + status
            call('pre', 'pytest', conv=conv)
            dispatch(conv)
            path = state_path(conv)
            state = json.loads(path.read_text())
            state.update(status=status, command=read_command, kind='test integrity', deadline=time.time() + 60,
                         arguments={'CommandLine': read_command, 'Cwd': str(repo / 'subdir')})
            path.write_text(json.dumps(state))
            before = len(log.read_text().splitlines())
            allowed_read = call('pre', read_command, conv=conv)
            assert allowed_read['decision'] == 'allow', (status, allowed_read)
            after = json.loads(path.read_text())
            if status == 'running':
                assert after['status'] == 'running' and after['token'] == state['token'], 'cleanup discarded an in-flight review'
            else:
                assert after == {}, ('obsolete false-positive state remained', status, after)
            assert call('pre', 'pytest', conv=conv)['decision'] == 'deny', 'obsolete gate cleanup granted a real test'
            assert len(log.read_text().splitlines()) == before, 'state cleanup launched a reviewer'

        # Grants from the former spawn-equals-completion implementation must
        # never survive migration, even when signature and snapshot still match.
        for legacy_version in (None, 1):
            conv = 'legacy-' + str(legacy_version)
            call('pre', 'pytest', conv=conv)
            result = execute(dispatch(conv))
            assert result.returncode == 0, (result.stdout, result.stderr)
            path = state_path(conv)
            state = json.loads(path.read_text())
            assert state['status'] == 'reviewed', state
            if legacy_version is None:
                state.pop('version', None)
            else:
                state['version'] = legacy_version
            path.write_text(json.dumps(state))
            assert call('pre', 'pytest', conv=conv)['decision'] == 'deny', 'legacy grant reused'
            result = execute(dispatch(conv))
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert call('pre', 'pytest', conv=conv)['decision'] == 'allow', 'fresh review after migration failed'

        for number, command in enumerate(['npm test', 'pytest -q', 'swift test', './scripts/test.sh',
                                         'git add .', 'git --no-pager commit -m fix', f'git -C "{repo}" add .']):
            conv = 'command-' + str(number)
            assert call('pre', command, conv=conv)['decision'] == 'deny', command
            args = dispatch(conv)
            call('post', conv=conv, stepIdx=99)
            assert call('pre', command, conv=conv)['decision'] == 'deny', 'dispatch is not completion'
            result = execute(args)
            assert result.returncode == 0, (result.stdout, result.stderr)
            allowed = call('pre', command, conv=conv)
            assert allowed['decision'] == 'allow', command
            assert 'bounded-command.py' in allowed.get('overwrite', {}).get('CommandLine', ''), allowed
            assert call('pre', command, conv=conv)['decision'] == 'deny', 'grant must be single use'

        # A comprehensive Git review needs consequence evidence even if its
        # fake reviewer says PASS. Test-integrity review must avoid that extra
        # prerequisite so tests can run before comprehensive final verification.
        no_consequences = {key: value for key, value in report.items() if key != 'consequence_checks'}
        report_file.write_text(json.dumps(no_consequences))
        call('pre', 'git add .', conv='missing-consequence-checks')
        result = execute(dispatch('missing-consequence-checks'))
        assert result.returncode != 0 and json.loads(result.stdout)['decision'] == 'UNVERIFIED', (result.stdout, result.stderr)
        assert call('pre', 'git add .', conv='missing-consequence-checks')['decision'] == 'deny'
        old_test_report = {key: value for key, value in report.items() if key not in ('consequence_checks', 'requirement_coverage')}
        report_file.write_text(json.dumps(old_test_report))
        call('pre', 'pytest', conv='test-review-no-consequence-fields')
        result = execute(dispatch('test-review-no-consequence-fields'))
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert call('pre', 'pytest', conv='test-review-no-consequence-fields')['decision'] == 'allow'
        report_file.write_text(json.dumps(report))

        # Shell-equivalent tokenization is insufficient for approval: quotes
        # determine variable/glob expansion when the original command executes.
        for conv, reviewed_command, changed_command in (
                ('quote-variable', "pytest '$TARGET'", 'pytest "$TARGET"'),
                ('quote-glob', "git add '*.txt'", 'git add *.txt')):
            assert call('pre', reviewed_command, conv=conv)['decision'] == 'deny'
            result = execute(dispatch(conv))
            assert result.returncode == 0, (result.stdout, result.stderr)
            reviewed_state = json.loads(state_path(conv).read_text())
            assert reviewed_state['status'] == 'reviewed', reviewed_state
            assert call('pre', changed_command, conv=conv)['decision'] == 'deny', 'different shell quoting reused an approval'
            changed_state = json.loads(state_path(conv).read_text())
            assert changed_state['status'] == 'pending' and changed_state['signature'] != reviewed_state['signature'], changed_state

        logs = [json.loads(line) for line in log.read_text().splitlines()]
        assert all(item['role'] == 'readonly-independent' for item in logs), 'CLI missing enforced readonly marker'
        invocations = [item['args'] for item in logs]
        assert all(a[a.index('--agent') + 1] == 'genoffice-reviewer' for a in invocations)
        assert all(a[a.index('--print-timeout') + 1] == '300s' for a in invocations)
        for args in invocations:
            assert '--json-schema' not in args, 'native schema flag causes multi-turn formatting loops'
            assert args[args.index('--output-format') + 1] == 'json', args
            schema_start = args[-1].find('{"type": "object"')
            assert schema_start >= 0, 'literal schema is missing from reviewer prompt'
            schema, _ = json.JSONDecoder().raw_decode(args[-1][schema_start:])
            assert schema['additionalProperties'] is False
            assert set(schema['required']) == {'decision', 'baseline', 'files_reviewed', 'findings',
                                                'edge_cases', 'test_integrity', 'limitations'}, schema
            assert schema['properties']['decision']['enum'] == ['PASS', 'BLOCKED', 'UNVERIFIED'], schema
            assert schema['properties']['findings']['items']['additionalProperties'] is False, schema
        assert all('READ ONLY' in a[-1] and 'edge cases' in a[-1] and 'original' in a[-1] for a in invocations)

        for command in ['python mutate.py && git add .', 'npm test | cat', 'sh -c "git add ."', 'git --git-dir=.git add .']:
            assert call('pre', command, conv='unsafe')['decision'] == 'deny', command
        assert call('pre', tool='write_to_file', args={'TargetFile': str(repo / 'example.test.py')}, conv='edits')['decision'] == 'allow'
        assert call('pre', 'git push --force', conv='danger')['decision'] == 'deny'
        assert call('pre', 'rm -rf backup', conv='danger')['decision'] == 'deny'
        assert call('pre', tool='invoke_subagent', args={}, conv='native')['decision'] == 'deny'
        for tool in ('write_to_file', 'replace_file_content', 'multi_replace_file_content', 'run_command',
                     'invoke_subagent', 'browser', 'browser_subagent', 'send_message', 'unknown_tool'):
            assert call('pre', tool=tool, args={}, role='readonly-independent')['decision'] == 'deny', tool
        for tool in ('view_file', 'grep_search', 'read_resource', 'finish'):
            assert call('pre', tool=tool, args={}, role='readonly-independent')['decision'] == 'allow', tool
        for command in ('git -ccore.excludesfile=/tmp/ignored add .', 'git add -Af .',
                        'git add --force .', 'GIT_INDEX_FILE=/tmp/alternate git add .',
                        'GIT_DIR=/tmp/alternate git add .', 'GIT_CONFIG_COUNT=1 git commit -m x',
                        'git --work-tree=/tmp/alternate add .', 'git --config-env=core.excludesFile=FILE add .'):
            assert call('pre', command, conv='git-options')['decision'] == 'deny', command
            assert not any('toolCall' in s for s in call('dispatch', conv='git-options').get('injectSteps', [])), command
        ordinary = call('pre', 'echo hello', conv='ordinary')
        assert ordinary['decision'] == 'allow' and 'bounded-command.py' in ordinary['overwrite']['CommandLine'], ordinary

        # Native CLI may place exact report JSON in response rather than its
        # structured_output field. Completion and single-use rules still apply.
        call('pre', 'pytest', conv='success-response')
        args = dispatch('success-response')
        call('post', conv='success-response', stepIdx=199)
        assert call('pre', 'pytest', conv='success-response')['decision'] == 'deny'
        result = execute(args, 'success-response')
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert call('pre', 'pytest', conv='success-response')['decision'] == 'allow'
        assert call('pre', 'pytest', conv='success-response')['decision'] == 'deny'

        bad_reports = [{**report, 'decision': verdict} for verdict in ('BLOCKED', 'UNVERIFIED')]
        bad_reports += [{**report, 'test_integrity': verdict} for verdict in ('SUSPICIOUS', 'CHEATING', 'UNVERIFIED')]
        bad_reports += [{**report, 'files_reviewed': []}, {**report, 'edge_cases': []},
                        {**report, 'baseline': ''}, {'decision': 'PASS'},
                        {**report, 'findings': [{}]},
                        {**report, 'findings': [{'severity': 'P0', 'location': 'source.txt:1', 'reason': 'unknown severity'}]},
                        {**report, 'files_reviewed': [None]}, {**report, 'edge_cases': [None]},
                        {**report, 'unexpected': 'must reject'},
                        {**report, 'findings': [{'severity': 'P3', 'location': 'x:1', 'reason': 'valid', 'extra': 'reject'}]},
                        {**report, 'findings': [{'severity': 'P2', 'location': 'source.txt:1', 'reason': 'wrong result'}]}]
        for number, bad in enumerate(bad_reports):
            conv = 'bad-report-' + str(number)
            call('pre', 'pytest', conv=conv)
            report_file.write_text(json.dumps(bad))
            assert execute(dispatch(conv)).returncode != 0, bad
            assert call('pre', 'pytest', conv=conv)['decision'] == 'deny', bad
            # Malformed output may use its one retry; no malformed report can
            # turn Stop into an endless continuation after the budget is gone.
            state = json.loads(state_path(conv).read_text())
            if state['status'] == 'pending':
                assert execute(dispatch(conv)).returncode != 0, bad
            assert call('stop', conv=conv).get('decision') != 'continue', bad
        report_file.write_text(json.dumps(report))

        # A real reviewer exhausting its deadline or attempting denied actions
        # needs reassessment, never an identical automatic second agent run.
        for mode in ('timeout-success', 'denied-success'):
            call('pre', 'pytest', conv=mode)
            args = dispatch(mode)
            before = len(log.read_text().splitlines())
            result = execute(args, mode)
            assert result.returncode != 0, (mode, result.stdout, result.stderr)
            state = json.loads(state_path(mode).read_text())
            assert state['status'] == 'blocked' and state['attempts'] == 1, (mode, state)
            failure = state.get('failure', '')
            assert failure and ('300' in failure if mode == 'timeout-success' else 'denied' in failure.lower()), (mode, state)
            assert failure in result.stderr, ('safe specific failure hidden from parent', mode, result.stderr, failure)
            assert 'PRIVATE_STDERR_SENTINEL' not in result.stdout + result.stderr + failure
            assert 'PRIVATE_DENIED_SENTINEL' not in result.stdout + result.stderr + failure
            for _ in range(2):
                assert call('dispatch', conv=mode) == {}
                assert call('pre', 'pytest', conv=mode)['decision'] == 'deny'
                assert call('stop', conv=mode).get('decision') == 'allow'
            # Even replaying an old worker command cannot spawn a second
            # reviewer once this state has terminally failed.
            assert execute(args, mode).returncode != 0
            assert len(log.read_text().splitlines()) == before + 1, 'terminal failure reran the same reviewer'
            assert json.loads(state_path(mode).read_text())['attempts'] == 1

            # Cosmetic labels, polling intervals and shell-equivalent whitespace
            # must not erase the failure budget of an identical actual command.
            path = state_path(mode)
            state = json.loads(path.read_text())
            state['signature'] = hashlib.sha256(json.dumps(state['arguments'], sort_keys=True).encode()).hexdigest()
            path.write_text(json.dumps(state))  # old all-arguments signature
            cosmetic_args = {'CommandLine': '  pytest   ', 'Cwd': str(repo / 'subdir'),
                             'toolSummary': 'A different displayed title', 'toolAction': 'Retry now',
                             'WaitMsBeforeAsync': 17}
            assert call('pre', args=cosmetic_args, conv=mode)['decision'] == 'deny'
            still_blocked = json.loads(path.read_text())
            assert still_blocked['status'] == 'blocked' and still_blocked['attempts'] == 1, still_blocked
            assert call('dispatch', conv=mode) == {}
            assert len(log.read_text().splitlines()) == before + 1, 'cosmetic retry launched another reviewer'

            # A genuine execution change gets its own gate, rather than being
            # mistaken for the already failed operation.
            old_signature = still_blocked['signature']
            assert call('pre', args={'CommandLine': 'pytest -q', 'Cwd': str(repo / 'subdir')}, conv=mode)['decision'] == 'deny'
            changed = json.loads(path.read_text())
            assert changed['status'] == 'pending' and changed.get('attempts', 0) == 0 and changed['signature'] != old_signature, changed
            assert call('pre', args={'CommandLine': 'pytest -q', 'Cwd': str(repo)}, conv=mode)['decision'] == 'deny'
            changed_cwd = json.loads(path.read_text())
            assert changed_cwd['status'] == 'pending' and changed_cwd.get('attempts', 0) == 0 and changed_cwd['signature'] != changed['signature'], changed_cwd

        for mode in ('exit', 'empty', 'malformed',
                     'malformed-response', 'fenced-response', 'invalid-response'):
            call('pre', 'pytest', conv=mode)
            for _ in range(2):
                assert execute(dispatch(mode), mode).returncode != 0
            assert call('pre', 'pytest', conv=mode)['decision'] == 'deny'
            assert not any('toolCall' in s for s in call('dispatch', conv=mode).get('injectSteps', [])), mode
            assert call('stop', conv=mode).get('decision') != 'continue', 'exhausted review traps Stop'

        for moment in ('before', 'during', 'after'):
            conv = 'changed-' + moment
            call('pre', 'pytest', conv=conv)
            args = dispatch(conv)
            if moment == 'before': tracked.write_text('before reviewer')
            result = execute(args, 'mutate' if moment == 'during' else 'report')
            if moment == 'after':
                assert result.returncode == 0, result.stderr
                tracked.write_text('after reviewer')
            else:
                assert result.returncode != 0, (moment, result.stdout, result.stderr)
            assert call('pre', 'pytest', conv=conv)['decision'] == 'deny', moment

        # An editor request invalidates a running review even if its actual
        # write has not reached disk, so stale in-flight results cannot grant.
        call('pre', 'pytest', conv='edit-inflight')
        args = dispatch('edit-inflight')
        worker = subprocess.Popen(args['CommandLine'], shell=True, env={**env, 'FAKE_MODE': 'pause'},
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 2
            while not (home / 'ready').exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (home / 'ready').exists(), 'fake reviewer never started'
            assert call('pre', tool='replace_file_content', args={'TargetFile': str(tracked)}, conv='edit-inflight')['decision'] == 'allow'
            (home / 'release').touch()
            out, err = worker.communicate(timeout=3)
            assert worker.returncode != 0, (out, err)
            assert call('pre', 'pytest', conv='edit-inflight')['decision'] == 'deny'
        finally:
            (home / 'release').touch()
            if worker.poll() is None:
                worker.terminate()
                worker.communicate(timeout=3)

        # A leading cd must fingerprint the actual external/unborn repository,
        # including untracked files absent from every parent workspace.
        outside = home / 'external repo'
        outside.mkdir()
        subprocess.run(['git', 'init', '-q', str(outside)], check=True)
        outside_file = outside / 'new.test.py'
        outside_file.write_text('assert result == expected\n')
        external_command = f'cd "{outside}" && pytest'
        call('pre', external_command, conv='external')
        unborn_report = {**report, 'baseline': 'empty tree: new repository, no original tests',
                         'files_reviewed': ['new.test.py'], 'limitations': ['No HEAD; reviewed new tests against requirements']}
        report_file.write_text(json.dumps(unborn_report))
        result = execute(dispatch('external'))
        assert result.returncode == 0, (result.stdout, result.stderr)
        outside_file.write_text('assert True\n')
        assert call('pre', external_command, conv='external')['decision'] == 'deny'
        report_file.write_text(json.dumps(report))

        call('pre', 'pytest', conv='expired')
        dispatch('expired')
        path = state_path('expired')
        state = json.loads(path.read_text())
        state.update(deadline=time.time() - 1, attempts=2)
        path.write_text(json.dumps(state))
        assert call('pre', 'pytest', conv='expired')['decision'] == 'deny'
        assert call('stop', conv='expired').get('decision') != 'continue', 'expired review traps Stop'

        for status in ('queued', 'running'):
            conv = 'abandoned-first-' + status
            call('pre', 'pytest', conv=conv)
            args = dispatch(conv)
            path = state_path(conv)
            state = json.loads(path.read_text())
            state.update(status=status, attempts=1, deadline=time.time() - 1)
            path.write_text(json.dumps(state))
            before = len(log.read_text().splitlines())
            assert call('stop', conv=conv).get('decision') == 'allow'
            assert json.loads(path.read_text())['status'] == 'blocked', 'abandoned deadline rearmed review'
            assert call('dispatch', conv=conv) == {}
            assert call('pre', 'pytest', conv=conv)['decision'] == 'deny'
            assert execute(args).returncode != 0, 'stale abandoned worker command restarted review'
            assert len(log.read_text().splitlines()) == before, 'abandoned first review launched another agent'
            assert json.loads(path.read_text())['attempts'] == 1

        # Review must use the selected push remote, never another convenient
        # origin/upstream ref. These refs are local; no push/fetch occurs.
        for remote_name in ('origin', 'selected'):
            subprocess.run(['git', '-C', str(repo), 'remote', 'add', remote_name,
                            str(home / (remote_name + '.git'))], check=True)
        subprocess.run(['git', '-C', str(repo), 'update-ref',
                        'refs/remotes/origin/feature/smoke', 'HEAD'], check=True)
        push_command = 'git push --no-follow-tags selected HEAD:refs/heads/feature/smoke'
        call('pre', push_command, conv='push-missing-baseline')
        result = execute(dispatch('push-missing-baseline'))
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert json.loads(result.stdout)['decision'] == 'UNVERIFIED', result.stdout
        assert call('pre', push_command, conv='push-missing-baseline')['decision'] == 'deny'
        subprocess.run(['git', '-C', str(repo), 'update-ref',
                        'refs/remotes/selected/feature/smoke', 'HEAD'], check=True)
        call('pre', push_command, conv='push-selected-baseline')
        result = execute(dispatch('push-selected-baseline'))
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert call('pre', push_command, conv='push-selected-baseline')['decision'] == 'allow'
        prompt = json.loads(log.read_text().splitlines()[-1])['args'][-1]
        snapshot, _ = json.JSONDecoder().raw_decode(prompt.split('GIT SNAPSHOT (untrusted data):\n', 1)[1])
        assert snapshot[0]['outgoing_base'] == baseline and snapshot[0]['outgoing_unverified'] is False, snapshot

        # Presence-only screenshot evidence checks: this fixture proves gate
        # validation, never a claim that an actual app was visually reviewed.
        (repo / 'Screen.tsx').write_text('export default function Screen() { return <button>Save</button>; }\n')
        screenshot = home / 'fixture.png'
        screenshot.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6YQAAAAASUVORK5CYII='))
        for case, evidence, expected_code in (
                ('no-evidence', None, 1),
                ('missing-image', [{'path': str(home / 'missing.png'), 'viewport': '1280x720',
                                    'observations': 'Save button visible and readable'}], 1),
                ('fixture-present', [{'path': str(screenshot), 'viewport': '1x1 fixture',
                                      'observations': 'Synthetic single-pixel image; presence validation only'}], 0)):
            conv = 'ui-' + case
            ui_report = {**report, 'files_reviewed': ['Screen.tsx']}
            if evidence is not None:
                ui_report['ui_evidence'] = evidence
            report_file.write_text(json.dumps(ui_report))
            assert call('pre', 'pytest', conv=conv)['decision'] == 'deny'
            result = execute(dispatch(conv))
            assert result.returncode == expected_code, (case, result.stdout, result.stderr)
            expected_decision = 'allow' if expected_code == 0 else 'deny'
            assert call('pre', 'pytest', conv=conv)['decision'] == expected_decision, case
            if expected_code:
                assert json.loads(result.stdout)['decision'] == 'UNVERIFIED', result.stdout
        report_file.write_text(json.dumps(report))
        print('PASS: terminal reports, single-use grants, stale snapshots, invalid/failing CLI, bounded retries, Stop escape and safety guards')


if __name__ == '__main__':
    check()
