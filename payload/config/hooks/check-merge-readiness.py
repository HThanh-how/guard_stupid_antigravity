#!/usr/bin/env python3
"""Offline merge guard checks: disposable Git, fake gh, no network/publication."""
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import tempfile
import time


def check():
    module = runpy.run_path(str(Path(__file__).with_name('merge-readiness.py')))
    guard = module['check']
    subprocess = module['subprocess']
    with tempfile.TemporaryDirectory(prefix='merge-readiness-check-') as temp:
        home = Path(temp)
        repo = home / 'repo'
        repo.mkdir()
        old_home = os.environ.get('HOME')
        os.environ['HOME'] = str(home)
        env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        original_output = subprocess.check_output
        original_runpath = module['runpy'].run_path
        try:
            def git(*args):
                return original_output(['git', '-C', str(repo), *args], env=env,
                    stderr=subprocess.DEVNULL, text=True).strip()
            git('init', '-q')
            git('checkout', '-qb', 'feature/merge-check')
            git('-c', 'user.name=Check', '-c', 'user.email=check@example.invalid',
                '-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-qm', 'disposable baseline')
            git('remote', 'add', 'origin', 'https://github.com/example/project.git')
            head = git('rev-parse', 'HEAD')
            data = {'conversationId': 'merge-check', 'workspacePaths': [str(repo)]}
            transcript = home / '.gemini/antigravity/brain/merge-check/.system_generated/logs/transcript.jsonl'
            transcript.parent.mkdir(parents=True)
            human = {'source': 'USER_EXPLICIT', 'type': 'USER_INPUT',
                'created_at': '2026-10-18T00:00:00Z', 'content': 'Implement and verify the scoped change before publication.'}
            transcript.write_text(json.dumps(human) + '\n')
            consequences = original_runpath(str(Path(__file__).with_name('consequence-review.py')))
            task = consequences['request_context']('merge-check')
            assert task['complete'] is True and task['requests']
            requirements_digest = consequences['requirements_digest'](task)
            args = {'Cwd': str(repo)}
            command = 'gh pr merge 12 --squash --match-head-commit ' + head
            pr = {'headRefOid': head, 'headRefName': 'feature/merge-check',
                'url': 'https://github.com/example/project/pull/12', 'number': 12,
                'state': 'OPEN', 'mergeable': 'MERGEABLE',
                'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
            report = {'decision': 'PASS', 'test_integrity': 'LEGITIMATE', 'findings': [],
                'flow_verification': {'status': 'VERIFIED', 'entrypoint': 'entry', 'path': 'entry -> save',
                    'outcome': 'Persisted result', 'evidence': 'Inspected full functions',
                    'scenarios': ['Save then reopen', 'Failed save retains original'],
                    'execution_evidence': 'Disposable regression run: 2 passed'},
                'limitations': [], 'consequence_checks': [
                    {'category': category, 'status': 'NOT_APPLICABLE',
                     'invariant': 'No affected flow', 'scenario': 'Move only', 'evidence': 'Inspected exact diff'}
                    for category in ('data-lifecycle', 'async-order')]}
            evidence = {'repo': str(repo.resolve()), 'branch': pr['headRefName'],
                'reviewed_commit': head, 'consequence_review_version': 3,
                'requirements_digest': requirements_digest,
                'kind': 'comprehensive', 'status': 'reviewed', 'report': report}
            path = home / '.gemini/config/hooks/readiness' / (hashlib.sha256(b'merge-check').hexdigest() + '.json')
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(evidence))
            native = None
            required = None
            gh_calls = []
            def fake_output(call, **kwargs):
                if call[0] == 'gh':
                    assert call[1:3] in (['pr', 'view'], ['pr', 'checks']), 'guard attempted mutation'
                    assert kwargs['timeout'] <= 3
                    gh_calls.append(call)
                    if call[2] == 'checks':
                        assert '--required' in call and kwargs['timeout'] <= 1
                        return json.dumps(required if required is not None else [{'name': 'required test', 'bucket': 'fail'}])
                    return json.dumps(pr)
                assert call[0] == 'git' and kwargs['timeout'] <= 1
                return original_output(call, **kwargs)
            subprocess.check_output = fake_output
            def fake_runpath(path, *a, **kw):
                if str(path).endswith('user-priority.py'):
                    return {'latest_human_request': lambda data: native}
                return original_runpath(path, *a, **kw)
            module['runpy'].run_path = fake_runpath
            assert guard('git commit -m change', args, data, {}) is None
            assert guard(command, args, data, {}) is None
            assert len(gh_calls) == 1
            assert guard('gh pr ready 12', args, data, {}) is None
            no_production_report = {key: value for key, value in report.items() if key != 'flow_verification'}
            path.write_text(json.dumps({**evidence, 'production_changed': False, 'report': no_production_report}))
            assert guard(command, args, data, {}) is None, 'explicit nonproduction review incorrectly required production flow'
            path.write_text(json.dumps({**evidence, 'report': no_production_report}))
            assert guard(command, args, data, {}) is not None, 'missing production flag silently waived flow evidence'
            path.write_text(json.dumps(evidence))
            for bad in ('env GH_HOST=other gh pr merge 12', 'gh pr merge 12 --auto',
                        'gh pr merge 12 --admin', 'gh pr ready', 'gh pr ready 12 --undo',
                        command + ' && echo done', 'sh -c "gh pr ready 12"',
                        'gh pr merge 12 --squash'):
                assert guard(bad, args, data, {}) is not None, bad
            original = copy.deepcopy(pr)
            for field, value in [('headRefOid', '0' * 40), ('headRefName', 'other'),
                                  ('url', 'https://github.com/other/repo/pull/12'),
                                  ('number', 13), ('state', 'CLOSED'), ('mergeable', 'UNKNOWN'),
                                  ('statusCheckRollup', [])]:
                pr[field] = value
                assert guard(command, args, data, {}) is not None, field
                pr.clear(); pr.update(copy.deepcopy(original))
            for value in ('FAILURE', 'SKIPPED', 'NEUTRAL', 'CANCELLED', 'TIMED_OUT', None):
                pr['statusCheckRollup'] = [{'status': 'COMPLETED', 'conclusion': value}]
                assert guard(command, args, data, {}) is not None, value
            pr['statusCheckRollup'] = [{'status': 'IN_PROGRESS', 'conclusion': 'SUCCESS'}]
            assert guard(command, args, data, {}) is not None
            pr['statusCheckRollup'] = [{'state': 'SUCCESS'}]
            assert guard(command, args, data, {}) is None
            for field, value in [('reviewed_commit', '0' * 40), ('repo', '/wrong'),
                                 ('branch', 'other'), ('consequence_review_version', 2),
                                 ('status', 'blocked'), ('requirements_digest', 'outdated')]:
                changed = {**evidence, field: value}
                path.write_text(json.dumps(changed))
                assert guard(command, args, data, evidence) is not None, field
            for field, value in [('decision', 'UNVERIFIED'), ('test_integrity', 'SUSPICIOUS'),
                                 ('findings', [{'severity': 'P2'}]), ('limitations', ['UI not seen']),
                                 ('consequence_checks', []), ('flow_verification', {}),
                                 ('flow_verification', {**report['flow_verification'], 'execution_evidence': ' '}),
                                 ('flow_verification', {**report['flow_verification'], 'scenarios': []}),
                                 ('flow_verification', {**report['flow_verification'], 'status': 'NOT_APPLICABLE'})]:
                path.write_text(json.dumps({**evidence, 'report': {**report, field: value}}))
                assert guard(command, args, data, evidence) is not None, field
            not_applicable = {**report, 'flow_verification': {**report['flow_verification'], 'status': 'NOT_APPLICABLE'},
                'production_test_alignment': {'status': 'NOT_APPLICABLE', 'rationale': 'Move only', 'evidence': 'Exact diff preserves behavior'}}
            path.write_text(json.dumps({**evidence, 'report': not_applicable}))
            assert guard(command, args, data, {}) is None
            path.write_text(json.dumps(evidence))
            continuation = {**human, 'created_at': '2026-10-18T00:00:30Z', 'content': 'Continue'}
            transcript.write_text(json.dumps(human) + '\n' + json.dumps(continuation) + '\n')
            assert guard(command, args, data, {}) is None, 'neutral Continue invalidated existing review'
            newer_human = {**human, 'created_at': '2026-10-18T00:01:00Z',
                'content': 'Also verify cancellation preserves the original saved content.'}
            transcript.write_text(json.dumps(human) + '\n' + json.dumps(newer_human) + '\n')
            assert guard(command, args, data, {}) is not None, 'new human requirement reused same-head PASS'
            transcript.write_text(json.dumps(human) + '\n')
            assert guard(command, args, data, {}) is None
            pr['statusCheckRollup'] = [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'},
                                     {'status': 'COMPLETED', 'conclusion': 'SKIPPED'}]
            required = [{'name': 'required test', 'bucket': 'pass', 'state': 'SUCCESS'}]
            assert guard(command, args, data, {}) is None, 'optional skipped blocked successful required checks'
            for bucket in ('fail', 'pending', 'skipping', 'cancel'):
                required[0]['bucket'] = bucket
                assert guard(command, args, data, {}) is not None, bucket
            required = []
            assert guard(command, args, data, {}) is not None, 'empty required result claimed CI green'
            required = None
            pr.update(original)
            path.unlink()
            assert guard(command, args, data, evidence) is not None, 'ephemeral state must not grant merge'
            native = {'source': 'interactive', 'scope': {'repo': str(repo.resolve()), 'branch': original['headRefName']},
                'operations': ['commit', 'push'], 'expires': time.time() + 100, 'head': head}
            assert guard(command, args, data, {}) is not None, 'Git waiver leaked into merge'
            native['operations'] = ['merge']
            assert guard(command, args, data, {}) is None
            for field, value in [('expires', time.time() - 1), ('expires', time.time() + 4000),
                                 ('scope', {'repo': '/other', 'branch': original['headRefName']}),
                                 ('source', 'agent'), ('head', '0' * 40), ('head', None)]:
                saved = native[field]; native[field] = value
                assert guard(command, args, data, {}) is not None, field
                native[field] = saved
            pr['statusCheckRollup'] = []
            assert guard(command, args, data, {}) is not None, 'native approval waived CI'
            pr.update(original)
            pr['mergeable'] = 'UNKNOWN'
            assert guard('gh pr ready 12', args, data, {}) is None
            assert guard(command, args, data, {}) is not None
            pr.update(original)
            (repo / 'tracked').write_text('before')
            # Use the real process only for staging disposable test data.
            original_output(['git', '-C', str(repo), 'add', 'tracked'], env=env)
            assert guard(command, args, data, {}) is not None, 'dirty index was accepted'
            print('PASS: exact PR/repo/head, required CI vs optional skipped, flow evidence, fresh original requirements, head-bound native exemption and dirty index')
        finally:
            subprocess.check_output = original_output
            module['runpy'].run_path = original_runpath
            if old_home is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = old_home


if __name__ == '__main__':
    check()
