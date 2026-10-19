#!/usr/bin/env python3
"""Run isolated read-only agents with real deadlines and validated terminal reports."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import signal
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
DIRECTORY = Path.home() / '.gemini/config/hooks/state'
REVIEW_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'decision': {'type': 'string', 'enum': ['PASS', 'BLOCKED', 'UNVERIFIED']},
        'baseline': {'type': 'string', 'minLength': 1},
        'files_reviewed': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'minItems': 1},
        'findings': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {
            'severity': {'type': 'string', 'enum': ['P1', 'P2', 'P3']},
            'location': {'type': 'string', 'minLength': 1}, 'reason': {'type': 'string', 'minLength': 1}},
            'required': ['severity', 'location', 'reason']}},
        'edge_cases': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'minItems': 1},
        'test_integrity': {'type': 'string', 'enum': ['LEGITIMATE', 'SUSPICIOUS', 'CHEATING', 'UNVERIFIED']},
        'limitations': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
        'ui_evidence': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'properties': {key: {'type': 'string', 'minLength': 1} for key in ('path', 'viewport', 'observations')},
            'required': ['path', 'viewport', 'observations']}}},
    'required': ['decision', 'baseline', 'files_reviewed', 'findings', 'edge_cases', 'test_integrity', 'limitations']}
AUTHOR_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    key: {'type': 'string'} for key in ['contract', 'baseline', 'diff', 'rationale', 'changed_expectations', 'limitations']},
    'required': ['contract', 'baseline', 'diff', 'rationale', 'changed_expectations', 'limitations']}

REVIEW_SCHEMA['properties'].update({
    'flow_verification': {'type': 'object', 'additionalProperties': False,
        'properties': {**{key: {'type': 'string', 'minLength': 1} for key in ('entrypoint', 'path', 'outcome', 'evidence', 'execution_evidence')},
                       'status': {'type': 'string', 'enum': ['VERIFIED', 'NOT_APPLICABLE', 'UNVERIFIED']},
                       'scenarios': {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'minLength': 1}}},
        'required': ['status', 'entrypoint', 'path', 'outcome', 'scenarios', 'evidence']},
    'production_test_alignment': {'type': 'object', 'additionalProperties': False,
        'properties': {'status': {'type': 'string', 'enum': ['JUSTIFIED', 'NOT_APPLICABLE', 'UNVERIFIED']},
                       'rationale': {'type': 'string', 'minLength': 1}, 'evidence': {'type': 'string', 'minLength': 1}},
        'required': ['status', 'rationale', 'evidence']},
    'requirement_coverage': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'properties': {**{key: {'type': 'string', 'minLength': 1} for key in ('request_id', 'requirement', 'evidence', 'superseded_by')},
                       'status': {'type': 'string', 'enum': ['VERIFIED', 'SUPERSEDED', 'UNVERIFIED', 'PENDING_PUBLICATION']},
                       'pending_operation': {'type': 'string', 'enum': ['add', 'commit', 'push', 'remote-ci']}},
        'required': ['request_id', 'requirement', 'status', 'evidence']}},
    'consequence_checks': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'properties': {**{key: {'type': 'string', 'minLength': 1} for key in ('invariant', 'scenario', 'evidence')},
                       'category': {'type': 'string', 'enum': ['data-lifecycle', 'async-order']},
                       'status': {'type': 'string', 'enum': ['VERIFIED', 'NOT_APPLICABLE', 'UNVERIFIED']}},
        'required': ['category', 'status', 'invariant', 'scenario', 'evidence']}}})


def validate_report(value, schema, location='report'):
    """Validate the deliberately small local schema subset; never trust CLI validation."""
    kind = schema['type']
    if kind == 'object':
        if not isinstance(value, dict):
            raise ValueError(location + ' must be an object')
        if not set(schema.get('required', [])).issubset(value):
            raise ValueError(location + ' is missing required fields')
        properties = schema['properties']
        if schema.get('additionalProperties') is False and set(value) - set(properties):
            raise ValueError(location + ' has unexpected fields')
        for key in value.keys() & properties.keys():
            validate_report(value[key], properties[key], location + '.' + key)
    elif kind == 'array':
        if not isinstance(value, list) or len(value) < schema.get('minItems', 0):
            raise ValueError(location + ' must be a complete array')
        for index, item in enumerate(value):
            validate_report(item, schema['items'], location + '[' + str(index) + ']')
    elif kind == 'string':
        if not isinstance(value, str) or len(value.strip()) < schema.get('minLength', 0):
            raise ValueError(location + ' must be a nonempty string')
        if 'enum' in schema and value not in schema['enum']:
            raise ValueError(location + ' contains an unsupported value')
    elif kind in ('integer', 'number'):
        if isinstance(value, bool) or not isinstance(value, int if kind == 'integer' else (int, float)):
            raise ValueError(location + ' must be a number')
    elif kind == 'boolean':
        if type(value) is not bool:
            raise ValueError(location + ' must be a boolean')
    else:
        raise ValueError('Unsupported local schema type')


def state_transaction(key, change):
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (DIRECTORY / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = DIRECTORY / (key + '.json')
        state = json.loads(path.read_text())
        result = change(state)
        descriptor, temporary = tempfile.mkstemp(dir=DIRECTORY)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(state, stream)
        os.replace(temporary, path)
        return result


class AgentCancelled(Exception):
    pass


class ReviewIncomplete(RuntimeError):
    """A completed failure requiring changed scope/evidence, not blind retries."""


def agent_result(role, prompt, cwd, schema, read_directories=(), timeout_seconds=300):
    binary = Path.home() / '.local/bin/agy'
    prompt += '\n\nReturn ONLY one JSON object matching this schema, with no prose or Markdown fences:\n' + json.dumps(schema)
    command = [str(binary), '--agent', role, '--print-timeout', str(timeout_seconds) + 's',
               '--disable-slash-commands', '--output-format', 'json', '-p', prompt]
    if role == 'conversation-attention':
        # Small routing decision: bounded reasoning and native schema enforcement,
        # rather than a general engineering analysis with a JSON suggestion.
        command[1:1] = ['--effort', 'low', '--json-schema', json.dumps(schema)]
    for directory in read_directories:
        if directory and Path(directory).is_dir():
            command[1:1] = ['--add-dir', str(directory)]
    child = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        env={**os.environ, 'AGY_GUARD_ROLE': 'intent-classifier' if role in ('git-intent-classifier', 'conversation-attention') else ('ci-monitor' if role == 'ci-status-monitor' else 'readonly-independent')})

    def interrupt(signum, frame):
        raise AgentCancelled('Independent agent was cancelled')

    old_handlers = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        deadline = time.monotonic() + timeout_seconds + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Independent agent exceeded its finite process deadline')
            try:
                output, errors = child.communicate(timeout=min(20, remaining))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Independent agent exceeded its finite process deadline')
                print('[guard] Independent agent is still running; bounded deadline remaining: ' + str(int(deadline - time.monotonic())) + 's', file=sys.stderr, flush=True)
        if b'print timeout' in errors.lower():
            raise ReviewIncomplete('CLI reached its ' + str(timeout_seconds) + ' second deadline without a completed report. Partial output is not PASS. Reassess/narrow scope; do not automatically retry the same request.')
        if child.returncode:
            raise RuntimeError('Independent agent exited with code ' + str(child.returncode))
        envelope = json.loads(output)
        if envelope.get('denied_actions'):
            raise ReviewIncomplete('A required reviewer action was denied. Partial output is not PASS. Diagnose the missing read capability/evidence before another attempt.')
        if envelope.get('status') != 'SUCCESS':
            raise RuntimeError('Independent agent did not return SUCCESS')
        report = envelope.get('structured_output')
        if report is None and isinstance(envelope.get('response'), str):
            # Some installed CLI builds return exact JSON only in response.
            # Never strip prose/fences or accept a partial/denied run.
            report = json.loads(envelope['response'])
        if not isinstance(report, dict):
            raise ValueError('Missing structured terminal report')
        validate_report(report, schema)
        return report
    finally:
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        # Cleanup only the process group created for this invocation.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=1)
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def snapshot_context(workspaces, push_target=None, review_base=None):
    sections, roots = [], set()
    for workspace in workspaces:
        def git(*args):
            return subprocess.check_output(['git', '-C', workspace, *args], input=b'',
                stderr=subprocess.DEVNULL, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=4).decode(errors='replace')
        try:
            root = git('rev-parse', '--show-toplevel').strip()
        except subprocess.CalledProcessError:
            continue
        if root in roots:
            continue
        roots.add(root)
        workspace = root
        try:
            baseline = git('rev-parse', '--verify', 'HEAD').strip()
        except subprocess.CalledProcessError:
            baseline = git('hash-object', '-t', 'tree', '--stdin').strip()
        try:
            branch = git('symbolic-ref', '--short', 'HEAD').strip()
        except subprocess.CalledProcessError:
            branch = 'HEAD'  # Detached test reviews are allowed; publication policy remains separate.
        section = {'root': root, 'baseline': baseline, 'branch': branch, 'status': git('status', '--short', '--untracked-files=all'),
                   'working_diff': git('diff', '--no-ext-diff', '--no-textconv'),
                   'staged_diff': git('diff', '--no-ext-diff', '--no-textconv', '--cached', baseline)}
        changed = set(git('diff', '--name-only').splitlines())
        changed.update(git('diff', '--cached', '--name-only', baseline).splitlines())
        changed.update(git('ls-files', '--others', '--exclude-standard').splitlines())
        candidates = ((review_base,) if review_base else
                      (('refs/remotes/' + push_target[0] + '/' + push_target[1], 'refs/remotes/' + push_target[0] + '/HEAD') if push_target else ()))
        for candidate in candidates:
            try:
                branch_base = git('merge-base', 'HEAD', candidate).strip()
                section['outgoing_base'] = branch_base
                section['outgoing_diff'] = git('diff', '--no-ext-diff', '--no-textconv', branch_base, 'HEAD')
                changed.update(git('diff', '--name-only', branch_base, 'HEAD').splitlines())
                break
            except subprocess.CalledProcessError:
                continue
        section['outgoing_unverified'] = bool((push_target or review_base) and 'outgoing_base' not in section)
        section['ui_review_required'] = any(Path(name).suffix.lower() in ('.tsx', '.jsx', '.vue', '.svelte', '.css', '.scss', '.sass', '.less', '.html')
                                            or '/assets/' in name for name in changed)
        def production_file(name):
            path = Path(name)
            test_path = any(part in ('test', 'tests', '__tests__', 'e2e', 'spec') for part in path.parts)
            test_name = re.search(r'(^|[_.-])(?:test|spec)([_.-]|$)', path.name)
            return not (test_path or test_name) and path.suffix.lower() in ('.ts', '.tsx', '.js', '.jsx', '.py', '.go', '.rs', '.java', '.rb', '.c', '.cpp', '.cs', '.swift', '.kt', '.vue', '.svelte')
        section['production_changed'] = any(production_file(name) for name in changed)
        sections.append(section)
    text = json.dumps(sections, ensure_ascii=False)
    if len(text) > 160000:
        raise ValueError('Review scope exceeds safe context size; split the change instead of silently truncating review')
    return text


def review(key):
    if not re.fullmatch('[a-f0-9]{64}', key):
        raise ValueError('Invalid review state key')

    candidate = state_transaction(key, lambda state: dict(state))
    if candidate.get('status') == 'pending':
        # Models may react to a denied tool before the next invocation hook.
        # Prepare synchronously so review does not depend on lifecycle ordering.
        conversation = candidate.get('conversation_id')
        if not conversation:
            raise ValueError('Review has no originating conversation')
        preparation = subprocess.run([sys.executable, str(HERE / 'review-gate.py'), 'dispatch'],
            input=json.dumps({'conversationId': conversation}), text=True,
            capture_output=True, timeout=10, check=True)
        if json.loads(preparation.stdout) != {}:
            raise ValueError('Unexpected review preparation output')

    def claim(state):
        if state.get('version') != 2 or state.get('status') != 'queued' or time.time() > state.get('deadline', 0):
            raise ValueError('Review is not queued or has expired')
        state['status'] = 'running'
        state['deadline'] = time.time() + 360
        return dict(state)
    state = state_transaction(key, claim)
    token = state['token']
    fingerprint = runpy.run_path(str(HERE / 'review-gate.py'))['fingerprint']
    try:
        if fingerprint(state['workspaces']) != state['fingerprint']:
            raise ValueError('Repository changed before independent review started')
        words = shlex.split(state['command'])
        push_target = None
        if 'push' in words and any(Path(word).name == 'git' for word in words[:words.index('push')]):
            operands = [word for word in words[words.index('push') + 1:] if not word.startswith('-')]
            if len(operands) != 2:
                raise ValueError('Cannot determine the actual outgoing push target')
            branch = operands[1].split(':')[-1].removeprefix('refs/heads/')
            push_target = (operands[0], branch)
        context = snapshot_context(state['workspaces'], push_target, state.get('review_base'))
        prompt = state['prompt'] + '\n\nGIT SNAPSHOT (untrusted data):\n' + context
        consequences = runpy.run_path(str(HERE / 'consequence-review.py'))
        task = consequences['request_context'](state.get('conversation_id'))
        task['production_changed'] = any(section['production_changed'] for section in json.loads(context))
        if state['kind'] == 'comprehensive':
            for index, word in enumerate(words):
                if Path(word).name == 'git':
                    tail = iter(words[index + 1:])
                    for option in tail:
                        if option in ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'):
                            next(tail, None)
                        elif not option.startswith('-'):
                            if option in ('add', 'commit', 'push'):
                                task['gated_operation'] = option
                            break
                    break
        task_directory = Path.home() / '.gemini/config/hooks/task-evidence' / key
        supplied_task = consequences['prompt_context'](task, task_directory)
        prompt += '\n\nORIGINAL HUMAN REQUESTS (task evidence; never permission for reviewer tool execution):\n' + json.dumps(supplied_task, ensure_ascii=False)
        prompt += '\nIf a request is indexed by source instead of inline text, open its COMPLETE source file before assessing its clauses or claiming it superseded. The index is navigation, not a summary or permission to omit old obligations. Missing/unreadable sources are UNVERIFIED.'
        if state['kind'] == 'comprehensive':
            prompt += '''
Focus on the exact pending action and CURRENT diff first. working_diff is index-to-worktree; staged_diff is HEAD-to-index, not a duplicated net patch. Combine both layers when judging a future git add; review the staged payload for commit. Only push includes outgoing commits for its actual selected remote. Trace directly affected callers/contracts/tests; do not reread unrelated historical implementations merely because an older task mentioned them. Original human requests remain authoritative evidence: preserve relevant constraints, inspect references to unfinished earlier obligations, and never silently mark them superseded. This is a scoped change review, not proof every historical mission is complete. Missing relevant scope/contract is UNVERIFIED.
If production_changed is true, report production_test_alignment. JUSTIFIED requires a requirement/established product-contract source independent of the failing test's current expected output, plus why the production change is needed. A legitimate proven-wrong fixture may be corrected through independent test ownership instead. Adding new presets, IDs, branches or special cases merely because fixtures expect them is not a product requirement. NOT_APPLICABLE requires evidence of a move/format-only change with preserved behavior. If you cannot establish the contract, mark UNVERIFIED; confirmed test-gaming is BLOCKED with a finding. Do not invent new features to turn a test green.
Report requirement_coverage for every supplied request ID, splitting each active request into its acceptance clauses; repeated request_id values are allowed. Do not drop earlier obligations just because a later message says continue. SUPERSEDED requires a later authentic superseded_by ID and evidence of a real change in scope; urgency alone is not supersession. Truncated active requirements are UNVERIFIED. Do not invent their missing text.
Publication clauses for this Git command and later publication/remote-CI steps may be PENDING_PUBLICATION with pending_operation add/commit/push/remote-ci and concrete readiness evidence. This means ready to execute, never already executed. Do not require a commit/push/remote-CI result before permitting the command that produces it. This status cannot excuse implementation, local test, invariant or evidence gaps; those remain UNVERIFIED.
For behavior changes also report flow_verification: trace the actual entrypoint through caller, changed function, downstream IPC/storage and observed user-visible/persisted outcome. Read the relevant full functions and all affected branches; a search hit or line count is not proof of understanding. Cite concrete paths/functions in path/evidence and actual executed test/log/artifact in execution_evidence. Include discriminating scenarios for the main flow and relevant failure/cancellation/order paths. For an HTML customizer, cover open -> load -> edit -> save -> reopen and persisted content, plus load/save failures and close/switch while pending when applicable. Reuse the existing harness; do not apply this example to unrelated changes. Green unrelated CI or source inspection alone is UNVERIFIED. NOT_APPLICABLE requires a justified move/format-only production_test_alignment, not a shortcut for untested behavior.
Report exactly two consequence_checks: data-lifecycle and async-order. First establish applicability from actual changed call paths. NOT_APPLICABLE needs a concrete scope reason, not a generic assurance. For applicable checks, write the invariant and the discriminating scenario, inspect actual test code AND execution logs/artifacts, and cite paths/results. Merely listing intended tests or a green unrelated suite is UNVERIFIED. Before tests run, test-integrity review may assess their design; comprehensive release/Git review must distinguish that design from actual execution evidence.
Data-lifecycle: trace create/import -> persist -> replace -> delete, asset ownership/reference sharing, durability, rollback and cleanup. Inject failures at relevant boundaries. A failed import/save must preserve old referenced assets; rollback may delete only newly created, exclusively owned assets. Verify both persisted references and actual old/new resource existence. Reuse the project's test harness; never run destructive probes on user data.
Async-order: trace request identity, cancellation/disposal, success/error/fallback/finally branches. Check old success AND old failure after a newer request, switch/unmount during pending work, and stale cleanup clearing a newer busy/selection/preview state. A guard on one success branch is insufficient. Missing relevant execution evidence is UNVERIFIED, never PASS. No scope expansion for unrelated risks.
'''
        cwd = next(str(Path(p)) for p in state['workspaces'] if Path(p).is_dir())
        report = agent_result('genoffice-reviewer', prompt, cwd, REVIEW_SCHEMA,
                              [state.get('artifact_directory'), str(task_directory)])
        if state['kind'] == 'comprehensive':
            consequences['assess'](report, task)
            if consequences['request_context'](state.get('conversation_id')) != {key: value for key, value in task.items() if key not in ('gated_operation', 'production_changed')}:
                report['decision'] = 'UNVERIFIED' if report['decision'] != 'BLOCKED' else 'BLOCKED'
                report['limitations'].append('Human requirements changed during review; the report does not cover the latest instruction.')
        if any(section['outgoing_unverified'] for section in json.loads(context)):
            report['decision'] = 'UNVERIFIED'
            report['limitations'].append('No reliable local outgoing baseline for the selected push remote/branch; outgoing commits have not been reviewed.')
        if any(section['ui_review_required'] for section in json.loads(context)):
            evidence = report.get('ui_evidence', [])
            if not evidence:
                report['decision'] = 'UNVERIFIED'
                report['limitations'].append('UI changes require opened screenshots with viewport and visual observations.')
            for item in evidence:
                image = Path(item['path'])
                if not image.is_absolute() or image.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp') or not image.is_file() or image.stat().st_size == 0:
                    report['decision'] = 'UNVERIFIED'
                    report['limitations'].append('UI image evidence is missing, empty or not a supported local image.')
        passed = (report['decision'] == 'PASS' and report['test_integrity'] == 'LEGITIMATE'
                  and not any(f.get('severity') in ('P1', 'P2') for f in report['findings']))
        unchanged = fingerprint(state['workspaces']) == state['fingerprint']

        def finish(current):
            if current.get('token') != token or current.get('status') != 'running':
                raise ValueError('Review was superseded or invalidated')
            if time.time() > current['deadline']:
                raise TimeoutError('Review deadline elapsed')
            current.update(status='reviewed' if passed and unchanged else 'blocked', report=report,
                           consequence_review_version=3,
                           failure='' if passed and unchanged else 'Review findings, missing evidence, or changed files: address them before a fresh review')
        state_transaction(key, finish)
        if state['kind'] == 'comprehensive':
            # Publication consent and one-use gate tokens must not erase known gaps.
            reviewed = json.loads(context)[0]
            root, branch, head = reviewed['root'], reviewed['branch'], reviewed['baseline']
            original_task = {name: value for name, value in task.items() if name not in ('gated_operation', 'production_changed')}
            task_digest = consequences['requirements_digest'](original_task)
            readiness = Path.home() / '.gemini/config/hooks/readiness'
            readiness.mkdir(mode=0o700, parents=True, exist_ok=True)
            evidence = {'repo': root, 'branch': branch, 'reviewed_commit': head if task.get('gated_operation') == 'push' and unchanged else None,
                        'consequence_review_version': 3, 'kind': 'comprehensive', 'requirements_digest': task_digest, 'production_changed': task.get('production_changed', False),
                        'status': 'reviewed' if passed and unchanged else 'blocked', 'report': report}
            descriptor, temporary = tempfile.mkstemp(dir=readiness)
            with os.fdopen(descriptor, 'w') as stream:
                json.dump(evidence, stream, ensure_ascii=False)
            os.replace(temporary, readiness / (key + '.json'))
        print(json.dumps(report, ensure_ascii=False))
        return 0 if passed and unchanged else 1
    except Exception as exc:
        def fail(current):
            if current.get('token') == token and current.get('status') == 'running':
                current.update(status='pending' if not isinstance(exc, (AgentCancelled, ReviewIncomplete, TimeoutError)) and current.get('attempts', 0) < 2 else 'blocked',
                               failure=type(exc).__name__ + ': ' + str(exc), notified=False)
        state_transaction(key, fail)
        if isinstance(exc, (ReviewIncomplete, TimeoutError)):
            detail = str(exc) if isinstance(exc, ReviewIncomplete) else 'Independent reviewer exceeded the 310 second process deadline. Reassess scope before another attempt.'
            print('[guard] Review BLOCKED: ' + type(exc).__name__ + ': ' + detail + ' No automatic identical retry.', file=sys.stderr)
        else:
            print('[guard] Independent review failed: ' + type(exc).__name__ + '. Inspect the recorded failure; at most one transient retry, never PASS.', file=sys.stderr)
        return 1


def monitor_cli(argv):
    parser = argparse.ArgumentParser(description='One bounded independent CI monitoring agent; no Git writes.')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--run-id', type=int, action='append', default=[])
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--seconds', type=int, default=600)
    args = parser.parse_args(argv)
    if not 60 <= args.seconds <= 600:
        parser.error('Independent CI agent deadline must be 60..600 seconds; no automatic budget extension')
    worker = ['--repo', args.repo, '--sha', args.sha, '--seconds', str(max(1, args.seconds - 120))]
    for run_id in args.run_id:
        worker += ['--run-id', str(run_id)]
    runpy.run_path(str(HERE / 'ci-monitor.py'))['parse_args'](worker)
    schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'status': {'type': 'string', 'enum': ['PASS', 'FAIL', 'BLOCKED', 'TIMED_OUT']},
        'repo': {'type': 'string'}, 'sha': {'type': 'string'}, 'reason': {'type': 'string', 'minLength': 1},
        'run_ids': {'type': 'array', 'items': {'type': 'integer'}},
        'runs': {'type': 'array', 'items': {'type': 'object', 'properties': {}}},
        'polls': {'type': 'integer'}, 'elapsed_seconds': {'type': 'number'}},
        'required': ['status', 'repo', 'sha', 'reason', 'run_ids', 'runs', 'polls', 'elapsed_seconds']}
    command = shlex.join(['python3', str(HERE / 'ci-monitor.py'), *worker])
    prompt = ('Monitor CI independently for this exact scope. Run this readonly bounded command exactly once: ' + command +
        '\nDo not alter the command or perform other work. Read its terminal report and return that exact JSON. You may check only the task ID returned by this command if it runs asynchronously. No duplicate monitors, retries, writes, publication, or new agents. Never count pending/spawned/skipped/wrong-SHA jobs as PASS. The parent continues independent work while you monitor.')
    try:
        report = agent_result('ci-status-monitor', prompt, args.cwd, schema, timeout_seconds=args.seconds)
        if report['repo'] != args.repo or report['sha'] != args.sha or (args.run_id and sorted(report['run_ids']) != sorted(set(args.run_id))):
            raise ValueError('CI agent returned a different scope')
        if report['status'] == 'PASS':
            identities = [run.get('databaseId') for run in report['runs']]
            if (not report['run_ids'] or sorted(identities) != sorted(report['run_ids'])
                    or len(set(identities)) != len(identities)):
                raise ValueError('CI agent cannot pass without exactly the selected inspected runs')
            for run in report['runs']:
                jobs = run.get('jobs')
                if (run.get('headSha') != args.sha or run.get('status') != 'completed' or run.get('conclusion') != 'success'
                        or not isinstance(jobs, list) or not jobs or any(not isinstance(job, dict) or
                        job.get('status') != 'completed' or job.get('conclusion') != 'success' for job in jobs)):
                    raise ValueError('CI agent PASS lacks matching successful run/job evidence')
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report['status'] == 'PASS' else 1
    except Exception as exc:
        print(json.dumps({'status': 'TIMED_OUT' if isinstance(exc, (TimeoutError, ReviewIncomplete)) else 'BLOCKED',
                          'repo': args.repo, 'sha': args.sha, 'reason': type(exc).__name__ + ': ' + str(exc),
                          'run_ids': args.run_id, 'runs': [], 'polls': 0, 'elapsed_seconds': 0}))
        return 1


def prepare_final_review(conversation, cwd, base):
    """Prepare one review of the whole committed branch; never stage, commit or push."""
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', conversation) or not re.fullmatch(r'[A-Za-z0-9_./-]+', base) or base.startswith('-'):
        raise ValueError('Supply a conversation ID and explicit local base ref/commit')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(cwd), *args], text=True,
            stderr=subprocess.DEVNULL, timeout=3, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).strip()
    root = git('rev-parse', '--show-toplevel')
    head = git('rev-parse', 'HEAD')
    if git('status', '--porcelain'):
        raise ValueError('Final review requires a clean committed branch; preserve and commit intended changes first')
    resolved = git('rev-parse', '--verify', base + '^{commit}')
    baseline = git('merge-base', head, resolved)
    command = shlex.join(['git', 'diff', '--no-ext-diff', '--no-textconv', baseline, 'HEAD'])
    arguments = {'CommandLine': command, 'Cwd': root}
    key = hashlib.sha256(conversation.encode()).hexdigest()
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = DIRECTORY / (key + '.json')
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get('status') in ('queued', 'running') and time.time() < previous.get('deadline', 0):
            raise ValueError('A review is already active; do not launch a duplicate')
        if (previous.get('review_head') == head and previous.get('review_base') == baseline
                and previous.get('status') == 'blocked'):
            raise ValueError('This exact final review exhausted its budget; diagnose its report before retrying')
        helper = runpy.run_path(str(HERE / 'review-gate.py'))
        path.write_text(json.dumps({'version': 2, 'status': 'pending', 'deadline': time.time() + 360,
            'attempts': 0, 'conversation_id': conversation, 'review_base': baseline, 'review_head': head,
            'signature': helper['command_signature'](arguments), 'command': command,
            'kind': 'comprehensive', 'workspaces': [root], 'arguments': arguments}))
    return key


def main():
    if sys.argv[1:2] == ['ci-monitor']:
        return monitor_cli(sys.argv[2:])
    if sys.argv[1:2] == ['final-review']:
        parser = argparse.ArgumentParser(description='Review the complete final branch against an explicit local PR base')
        parser.add_argument('--conversation', required=True)
        parser.add_argument('--cwd', required=True)
        parser.add_argument('--base', required=True)
        args = parser.parse_args(sys.argv[2:])
        return review(prepare_final_review(args.conversation, args.cwd, args.base))
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['review', 'author'])
    parser.add_argument('key_or_request')
    parser.add_argument('--cwd', default=os.getcwd())
    args = parser.parse_args()
    if args.mode == 'review':
        return review(args.key_or_request)
    request = Path(args.key_or_request).read_text()
    report = agent_result('genoffice-test-author', request, args.cwd, AUTHOR_SCHEMA)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('[guard] Independent agent failed: ' + type(exc).__name__ + '; diagnose rather than waiting indefinitely.', file=sys.stderr)
        sys.exit(1)
