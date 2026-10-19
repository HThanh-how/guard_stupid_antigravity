#!/usr/bin/env python3
"""Antigravity review gates. State is private and scoped to a conversation."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import sys
import signal
import time
import uuid
import tempfile


def matches(pattern, text):
    return re.search(pattern, text, re.I) is not None


def command_signature(arguments, retry_budget=False):
    execution = {key: value for key, value in arguments.items()
                 if key not in ('toolAction', 'toolSummary', 'WaitMsBeforeAsync')}
    if retry_budget:
        # Conservative grouping only prevents re-launch of a BLOCKED review.
        # Approval identity must preserve shell quoting/expansion semantics.
        for field in ('CommandLine', 'command'):
            if isinstance(execution.get(field), str):
                execution[field] = shlex.join(shlex.split(execution[field]))
    return hashlib.sha256(json.dumps(execution, sort_keys=True).encode()).hexdigest()



def fingerprint(workspaces):
    """Include index, working diff and untracked content, without changing Git state."""
    digest = hashlib.sha256()
    roots = set()
    for workspace in workspaces:
        def git(*args):
            return subprocess.check_output(['git', '-C', workspace, *args], input=b'',
                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, stderr=subprocess.DEVNULL, timeout=4)
        try:
            root = Path(git('rev-parse', '--show-toplevel').decode().strip())
        except subprocess.CalledProcessError:
            continue  # A workspace container can contain the actual repository Cwd.
        if root in roots:
            continue
        roots.add(root)
        workspace = str(root)  # All returned paths must be relative to this root.
        digest.update(str(root).encode())
        try:
            base = git('rev-parse', '--verify', 'HEAD').decode().strip()
        except subprocess.CalledProcessError:
            base = git('hash-object', '-t', 'tree', '--stdin').decode().strip()
        digest.update(base.encode())
        digest.update(git('diff', '--no-ext-diff', '--no-textconv', '--binary', base))
        digest.update(git('diff', '--no-ext-diff', '--no-textconv', '--binary', '--cached', base))
        for name in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
            if name:
                file = root / os.fsdecode(name)
                digest.update(name)
                digest.update(os.fsencode(os.readlink(file)) if file.is_symlink() else file.read_bytes())
    if not roots:
        raise ValueError('No Git repository found: supply the actual repository Cwd for review')
    return digest.hexdigest()


def main():
    event = sys.argv[1]
    def expired(signum, frame):
        raise TimeoutError('Hook exceeded its own 8 second execution budget')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 8)
    data = json.load(sys.stdin)
    if event == 'pre' and os.environ.get('AGY_GUARD_ROLE') == 'intent-classifier':
        name = data.get('toolCall', {}).get('name', '')
        print(json.dumps({'decision': 'allow' if name == 'finish' else 'deny',
                          'reason': 'Intent classifier may only return its final classification; all tools are disabled.'}))
        return
    if event == 'pre' and os.environ.get('AGY_GUARD_ROLE') == 'ci-monitor':
        name = data.get('toolCall', {}).get('name', '')
        allowed = name == 'finish'
        if name == 'manage_task':
            allowed = data['toolCall'].get('args', {}).get('Action') == 'status'
        if name == 'run_command':
            raw = data['toolCall'].get('args', {})
            words = shlex.split(raw.get('CommandLine') or raw.get('command') or '')
            helper = str(Path(__file__).with_name('ci-monitor.py'))
            allowed = (len(words) >= 2 and Path(words[0]).name in ('python3', 'python') and words[1] == helper
                       and not any(token in (';', '&&', '||', '|', '&', '>', '<') for token in words)
                       and not any(mark in (raw.get('CommandLine') or raw.get('command') or '') for mark in ('\n', '$(', '`')))
            if allowed:
                try:
                    runpy.run_path(helper)['parse_args'](words[2:])
                except (ValueError, SystemExit, TypeError):
                    allowed = False
        print(json.dumps({'decision': 'allow' if allowed else 'deny', 'reason': 'CI agent may only run the exact bounded readonly ci-monitor.py helper and return its report; no edits, arbitrary shell, publication or delegation.'}))
        return
    if event == 'pre' and os.environ.get('AGY_GUARD_ROLE') == 'readonly-independent':
        name = data.get('toolCall', {}).get('name', '')
        permitted = name in ('view_file', 'grep_search', 'read_resource', 'finish')
        print(json.dumps({'decision': 'allow' if permitted else 'deny',
                          'reason': 'Independent agent is read-only: only file/resource reads/searches and final reporting are permitted. No edits, shell, browser, messaging or delegation.'}))
        return
    conv = data.get('conversationId')
    if not conv:
        raise ValueError('Missing conversationId; cannot safely track reviewer completion')
    directory = Path.home() / '.gemini/config/hooks/state'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(conv.encode()).hexdigest()
    lock = (directory / (key + '.lock')).open('a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    path = directory / (key + '.json')
    state = json.loads(path.read_text()) if path.exists() else {}
    if (state.get('kind') == 'test integrity' and state.get('status') in ('pending', 'queued', 'blocked')
            and state.get('command') and not runpy.run_path(str(Path(__file__).with_name('test-command.py')))['is_test_command'](state['command'])):
        state = {}  # Retire an old false-positive gate; this grants no test/Git action.
    if state.get('arguments'):
        # Display text/poll intervals cannot turn a failed review into a new one.
        state['signature'] = command_signature(state['arguments'])
        state['retry_signature'] = command_signature(state['arguments'], retry_budget=True)
    if state and state.get('version') != 2:
        # Earlier hooks treated successful spawn as review completion. Never reuse those grants.
        if all(name in state for name in ('signature', 'command', 'workspaces')):
            state.update(version=2, status='pending', attempts=0, token=None, notified=False, deadline=time.time() + 360)
            state.setdefault('arguments', {'CommandLine': state['command']})
        else:
            state = {}
    tool = data.get('toolCall', {}).get('name', '')
    args = data.get('toolCall', {}).get('args', {})
    command = args.get('CommandLine') or args.get('command') or ''
    file = args.get('TargetFile') or args.get('path') or args.get('AbsolutePath') or ''
    result = {}
    if state.get('kind') == 'comprehensive' and state.get('status') == 'reviewed' and state.get('consequence_review_version') != 3:
        state.update(status='pending', attempts=0, token=None, notified=False, deadline=time.time() + 360)
    if state.get('status') == 'pending' and time.time() > state.get('deadline', 0):
        state.update(status='blocked', failure='Review preparation exceeded its finite deadline. Report BLOCKED and stop; do not wait for an unstarted task.', notified=False)
    if state.get('status') in ('queued', 'running') and time.time() > state.get('deadline', 0):
        state.update(status='blocked',
                     failure='Independent reviewer exceeded the 6 minute deadline. Reassess scope/runtime before another attempt; do not automatically retry the same review or count it as PASS.', notified=False)
    if state.get('status') == 'pending' and state.get('attempts', 0) >= 2:
        state.update(status='blocked', failure='Two independent reviewer attempts failed; stop and report the blocker instead of polling or respawning forever.', notified=False)

    if event == 'dispatch' and state.get('status') == 'pending':
        try:
            state['fingerprint'] = fingerprint(state['workspaces'])
        except Exception as exc:
            state.update(status='blocked', failure='Cannot establish review baseline: ' + type(exc).__name__ + '. Diagnose or narrow the scope; do not retry blindly.', notified=False)

    if event == 'pre':
        result = {'decision': 'allow'}
        if tool in ('write_to_file', 'replace_file_content', 'multi_replace_file_content'):
            # Any edit invalidates even an in-flight review.
            if state.get('kind'):
                state['status'] = 'pending'
                state.update(attempts=0, token=None, notified=False, deadline=time.time() + 360)
            if matches(r'\.github/workflows/|\.gitlab-ci|\.circleci|Jenkinsfile|\.drone\.|pyproject\.toml|setup\.cfg|tox\.ini', file):
                result = {'decision': 'force_ask', 'reason': 'Sửa file CI/policy cần phê duyệt: ' + file}
        elif tool == 'invoke_subagent':
            result = {'decision': 'deny', 'reason': 'Unbounded native agent invocation is disabled. Use the bounded independent-agent.py author/review runner for test design/review; for other delegation use agy --agent <known-agent> --print-timeout 300s through run_command. Require a terminal result, never confuse a spawned/running task with completion.'}
        elif tool == 'ask_question' and 'questions' in args:
            # Native approvals carry the actual repo, branch and reason into the
            # question itself, so a later selected answer is scoped evidence.
            interactive = runpy.run_path(str(Path(__file__).with_name('interactive-permission.py')))
            offered = interactive['questions'](args['questions'])
            combined = json.dumps(offered, ensure_ascii=False).lower()
            if matches(r'\b(?:commit|push|merge)\b', combined) and (matches(r'\b(?:cho phép|đồng ý|allow|approve|authorize|approval|cấp quyền|duyệt)\b', combined) or 'actions:' in combined):
                if len(offered) != 1:
                    print(json.dumps({'decision': 'deny', 'reason': 'Use one scoped Git approval question with repo, branch, actions, reason and unverified checks. Other questions can be asked separately; no repeated human confirmation is needed.'}))
                    return
                priority = runpy.run_path(str(Path(__file__).with_name('user-priority.py')))
                existing = interactive['header'](offered[0].get('question', ''))
                scope_args = {'Cwd': existing['repo']} if existing else state.get('approval_arguments') or state.get('arguments', {})
                scope = priority['permission_scope']({'workspacePaths': state.get('approval_workspaces') or state.get('workspaces') or data.get('workspacePaths', []),
                    'toolCall': {'args': scope_args}})
                if 'merge' in interactive['proposed_actions'](offered):
                    scope['head'] = subprocess.check_output(['git', '-C', scope['repo'], 'rev-parse', 'HEAD'], timeout=2, text=True).strip()
                reason = state.get('approval_failure') or state.get('failure') or 'No completed independent review currently authorizes the pending Git action.'
                limits = state.get('report', {}).get('limitations', [])
                unverified = '; '.join(limits) or 'Independent review is incomplete; unrun tests, visual checks and CI must not be reported as passed.'
                existing = interactive['header'](offered[0].get('question', '')) if len(offered) == 1 else None
                if not existing or offered[0].get('is_multi_select') is not False or {key: existing[key] for key in ('repo', 'branch')} != {key: scope[key] for key in ('repo', 'branch')} or ('head' in scope and existing.get('head') != scope['head']):
                    result = {'decision': 'deny', 'reason': 'Git approval requires one complete scoped question with is_multi_select=false before display. Explain repository, branch, actions, blocker and unverified checks.'}
                    prepared = interactive['prepare'](args['questions'], scope, reason, unverified)
                    if prepared:
                        # Keep original recorded tool arguments identical to the
                        # displayed offer. Rewriting them here would leave some
                        # app builds logging only the unmodified question.
                        result = {'decision': 'deny', 'reason': 'Ask once with this complete scoped explanation and choices; do not ask the user to type a magic phrase. Retry ask_question with questions=' + json.dumps(prepared, ensure_ascii=False)}
        elif tool == 'run_command':
            merge_error = runpy.run_path(str(Path(__file__).with_name('merge-readiness.py')))['check'](command, args, data, state)
            if merge_error:
                state.update(version=2, approval_arguments=args, approval_workspaces=data.get('workspacePaths', []), approval_failure=merge_error)
                path.write_text(json.dumps(state))
                print(json.dumps({'decision': 'deny', 'reason': merge_error}))
                return
            normalized_ci = ' '.join(shlex.split(command))
            if matches(r'\bgh\s+run\s+(?:list|view|watch)\b|\bgh\s+pr\s+checks\b[^\n]*--watch\b', normalized_ci):
                print(json.dumps({'decision': 'deny', 'reason': 'Delegate CI status polling to one bounded readonly ci-status-monitor via independent-agent.py ci-monitor --repo owner/repo --sha <exact HEAD> --run-id <id> --cwd <repo> --seconds 600. Run asynchronously with WaitMsBeforeAsync <= 1000, continue independent implementation work, and consume its terminal report. Omit run-id for one readonly discovery at this SHA. Do not duplicate polling, change branches while it runs, wait indefinitely, or restart the same timed-out monitor automatically. Report any failed job IDs/URLs so the user can inspect them; never infer success from status polling.'}))
                return
            branch_check = runpy.run_path(str(Path(__file__).with_name('git-branch-policy.py')))['check']
            branch_error = branch_check(command, args, data.get('workspacePaths', []))
            if branch_error:
                print(json.dumps({'decision': 'deny', 'reason': branch_error}))
                return
            if matches(r'\bgit\s+(push\b[^\n]*(?:--force|-f\b)|reset\s+--hard|rebase\s+-i|stash\s+(?:drop|clear)|branch\s+-[Dd]\s|tag\s+-d\s)', command):
                result = {'decision': 'force_ask', 'reason': 'Lệnh Git nguy hiểm cần phê duyệt: ' + command}
            elif matches(r'\brm\s+[^\n]*(?:-rf?\b|--force)[^\n]*(?:migration|backup|\.db|schema|fixture)', command):
                result = {'decision': 'deny', 'reason': 'Xoá migration/backup/schema bị chặn: ' + command}
            if result['decision'] != 'deny':
                # ponytail: command heuristics cover common runners; opaque custom wrappers need explicit naming.
                normalized = ' '.join(shlex.split(command))
                words = shlex.split(command)
                git_gate = False
                git_operation = None
                for index, word in enumerate(words):
                    if Path(word).name != 'git':
                        continue
                    skip_value = False
                    for option in words[index + 1:]:
                        if skip_value:
                            skip_value = False
                        elif option in ('-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'):
                            skip_value = True
                        elif option.startswith('-'):
                            continue
                        else:
                            git_gate = option in ('add', 'commit', 'push')
                            git_operation = option if git_gate else None
                            break
                    if git_gate:
                        break
                # Detect nested shell commands too; these are rejected below.
                git_gate = git_gate or matches(r'\bgit\s+(?:add|commit|push)\b', normalized)
                test_gate = runpy.run_path(str(Path(__file__).with_name('test-command.py')))['is_test_command'](command)
                waived = git_operation in runpy.run_path(str(Path(__file__).with_name('user-priority.py')))['git_review_waiver'](data)
                signature = command_signature(args)
                retry_signature = command_signature(args, retry_budget=True)
                if git_gate or test_gate:
                    lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|<>')
                    lexer.whitespace_split = True
                    parts = list(lexer)
                    if len(parts) > 3 and parts[0] == 'cd' and parts[2] == '&&':
                        parts = parts[3:]
                    unsafe = (any(token and all(c in ';&|<>' for c in token) for token in parts)
                              or '\n' in command or '$(' in command or '`' in command
                              or any(matches(r'^GIT_(?:DIR|WORK_TREE|INDEX_FILE|CONFIG[^=]*)=', word) for word in words)
                              or (git_gate and any(word in ('--config-env', '--force') or word.startswith(('-c', '--git-dir', '--work-tree', '--config-env=', '--config=')) or (word.startswith('-') and not word.startswith('--') and 'f' in word[1:]) for word in words))
                              or matches(r'\b(?:bash|sh|zsh|eval)\s+(?:-\S*\s+)*-c\b', command))
                    if unsafe:
                        print(json.dumps({'decision': 'deny', 'reason': 'Run tests or git add/commit/push as a standalone command (an initial cd is allowed). Split mutating prefixes, pipes, redirection and nested shells; use git -C instead of alternate Git directories/index/config and do not force-add ignored files, so the review covers the repository that actually executes.'}))
                        return
                    if waived:
                        state = {}
                        result['reason'] = 'Explicit human instruction waives waiting for Git review on this working branch. Proceed; report verification limits honestly.'
                    elif (state.get('status') == 'reviewed' and state.get('signature') == signature
                            and state.get('fingerprint') == fingerprint(state['workspaces'])):
                        state = {}  # One completed review permits exactly one retry.
                    else:
                        same_failed_review = state.get('status') == 'blocked' and state.get('retry_signature') == retry_signature
                        if (state.get('signature') != signature and not same_failed_review) or state.get('status') == 'reviewed':
                            state = {'version': 2, 'status': 'pending', 'deadline': time.time() + 360, 'signature': signature, 'command': command,
                                     'kind': 'comprehensive' if git_gate else 'test integrity',
                                     'retry_signature': retry_signature,
                                     'workspaces': data.get('workspacePaths', []), 'arguments': args, 'artifact_directory': data.get('artifactDirectoryPath')}
                            cwd = args.get('Cwd') or args.get('cwd') or args.get('working_directory')
                            cwd = Path(cwd or (state['workspaces'][0] if state['workspaces'] else os.getcwd()))
                            if words[:1] == ['cd'] and len(words) > 2 and words[2] == '&&':
                                cwd = (cwd / words[1]).resolve()
                            targets = [str(cwd)]
                            for index, word in enumerate(words):
                                if word == '-C' and index + 1 < len(words):
                                    cwd = (cwd / words[index + 1]).resolve()
                                    targets.append(str(cwd))
                                elif word.startswith('-C') and len(word) > 2:
                                    cwd = (cwd / word[2:]).resolve()
                                    targets.append(str(cwd))
                            state['workspaces'] = list(dict.fromkeys(targets + state['workspaces']))
                        result = {'decision': 'deny', 'reason': 'REVIEW REQUIRED: ' + state['kind'] + '. ' + (state.get('failure', '') + ' Stop and report BLOCKED; do not keep polling.' if state.get('status') == 'blocked' else 'Immediately run this standalone bounded read-only reviewer command: ' + shlex.join(['python3', str(Path(__file__).with_name('independent-agent.py')), 'review', key]) + ' . Working directory: ' + state['workspaces'][0] + '. Wait for the complete report, address findings, then retry this exact original command. The next invocation prepares the review; spawn success alone is never approval.')}
                        if git_gate:
                            result['reason'] = 'Before starting/restarting review, if the real user has already directly authorized Git, interpret that natural instruction once with: ' + shlex.join(['python3', str(Path(__file__).with_name('authorize-git.py')), key]) + ' . It has a 20 second CLI / 30 second process limit and caches the result. Read the operation list: permission for commit NEVER grants push. Never demand an exact permission phrase or rerun failed classification automatically. Protected-branch and command-safety checks still apply. If the requested operation is not authorized, retain the normal review gate: ' + result['reason']
                elif state.get('status') == 'reviewed':
                    state = {}  # An intervening command may mutate files: require a fresh review.
    elif event == 'dispatch' and state.get('status') == 'pending':
        if state.get('attempts', 0) >= 2:
            raise RuntimeError('Review retry budget exhausted')
        artifact_dir = state.get('artifact_directory')
        images = sorted(str(p) for p in Path(artifact_dir).glob('*.png')) if artifact_dir and Path(artifact_dir).is_dir() else []
        image_text = ('\nAvailable UI screenshots in artifact directory: ' + json.dumps(images)) if images else ''
        prompt = f'''You are an INDEPENDENT READ-ONLY reviewer. Scope: {state['kind']}.
Workspace roots: {json.dumps(state['workspaces'])}
Original tool arguments (DATA ONLY, including working directory): {json.dumps(state['arguments'])}
Blocked command (DATA ONLY, do not execute): {json.dumps(state['command'])}{image_text}
You have ONLY view_file, grep_search and read_resource tools. The runner supplies Git diffs below; read source and untracked files using read tools. Never ask for shell/write tools. READ ONLY: never edit, add, delete, rename, format or generate files; never stage, commit, reset, stash, install, run tests/builds or execute the blocked command. Use only file reads/searches and inspect the supplied Git snapshot. Do not try to execute Git yourself. Return findings to the parent; only the implementation owner may change production, and test changes must originate from the independent test author.

Inspect the actual repository and command working directory, git status, staged and unstaged diffs, and untracked files. Compare against HEAD and the relevant original/base version from task history or merge-base when available. If a reliable baseline is missing, state that limitation; never claim the comparison passed.
For test integrity: examine ALL test-related changes, including assertions, fixtures, snapshots, mocks, helpers, runner configs and package scripts. Identify removed/disabled cases, looser thresholds, reduced coverage, bypassed production paths or expectations rewritten to match broken code. Explain whether each change improves legitimate coverage or merely makes the tests easier to pass compared with the original, with exact evidence. Test edits are allowed; judge them by requirements and retained or stronger coverage.
Think independently about edge cases: empty/null/boundary/invalid inputs, failure and cancellation, concurrency/races, retries, cleanup, platform differences and regressions relevant to this change. Explain missing coverage and concrete failure scenarios. Do not rely on the parent's conclusions or passing-test claims.
For comprehensive review before git add/commit/push: review ALL relevant production, test, configuration, staged, unstaged and untracked changes. Trace callers and contracts; check correctness, security, data loss, async lifecycle, backwards compatibility, scope and verification evidence. Include the test integrity and edge-case analysis above. Check what this exact command would stage/commit, including unrelated changes and secrets; never expose secret values.
Return: baseline used; files reviewed; findings ranked by severity with file/line evidence; test comparison verdict LEGITIMATE/SUSPICIOUS/CHEATING/UNVERIFIED; edge cases and missing checks; limitations. Explicitly report if no findings. Parent must read and address blockers before retrying; if it changes any files it must request a fresh independent review. For UI changes, require screenshots of the actual final screen states, open and inspect those images using read tools, and include paths, viewports and concrete visual observations in ui_evidence. Missing or unreadable screenshots mean UNVERIFIED. Never treat DOM/test success or a screenshot path alone as visual verification. You may only advise, never implement changes.'''
        state.update(status='queued', prompt=prompt, attempts=state.get('attempts', 0) + 1,
                     token=uuid.uuid4().hex, deadline=time.time() + 360, notified=False)
        # Do not inject steps: the installed app rejects the documented injection format.
        # PreToolUse's deny reason tells the model the exact bounded reviewer command.
        result = {}
    elif event == 'post':
        # Tool execution/spawn success cannot prove asynchronous agent completion.
        # Only independent-agent.py validates the terminal report and grants review.
        pass
    elif event == 'dispatch' and state.get('status') == 'blocked':
        result = {}
    elif event == 'stop' and runpy.run_path(str(Path(__file__).with_name('user-priority.py')))['git_review_waiver'](data):
        result = {'decision': 'allow', 'reason': 'The user explicitly waived waiting for Git review. Do not force a review wait or claim unrun checks passed.'}
    elif event == 'stop' and state.get('status') in ('pending', 'queued', 'running'):
        # Ending a turn is not executing the gated action. Enforce review in
        # PreToolUse, never by forcing another turn or re-launching a worker.
        result = {'decision': 'allow', 'reason': 'Review remains incomplete. End with a truthful pending/BLOCKED report; do not claim PASS or automatically restart the reviewer. The original test/Git command remains gated until a completed review or explicit applicable user waiver.'}
    elif event == 'stop' and state.get('status') == 'blocked':
        result = {'decision': 'allow', 'reason': 'Review is blocked; report the failure and stop instead of waiting indefinitely.'}

    if event == 'pre' and tool == 'run_command' and result.get('decision') != 'deny' and command:
        field = 'CommandLine' if args.get('CommandLine') else 'command'
        limits_path = Path(__file__).with_name('command-limits.json')
        limits = json.loads(limits_path.read_text()) if limits_path.exists() else {'seconds': 600, 'idle': 180}
        seconds, idle = limits['seconds'], limits['idle']
        if not isinstance(seconds, (int, float)) or not isinstance(idle, (int, float)) or not 0 < seconds <= 3600 or not 0 <= idle <= seconds:
            raise ValueError('Invalid finite command deadlines')
        wrapper = shlex.join(['python3', str(Path(__file__).with_name('bounded-command.py')), '--seconds', str(seconds), '--idle', str(idle), '--', command])
        result['overwrite'] = {field: wrapper}

    if state:
        state['conversation_id'] = conv
    descriptor, temporary = tempfile.mkstemp(dir=directory)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(state, stream)
    os.replace(temporary, path)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Fail closed for tool execution; keep lifecycle errors visible as instructions.
        if sys.argv[1] == 'pre':
            print(json.dumps({'decision': 'deny', 'reason': 'Review guard error: ' + str(exc)}))
        elif sys.argv[1] == 'stop':
            print(json.dumps({'decision': 'allow', 'reason': 'Review guard error; report BLOCKED and stop: ' + str(exc)}))
        else:
            print(json.dumps({}))
