#!/usr/bin/env python3
"""Bounded read-only GitHub Actions polling. This deterministic worker is not AI."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import shlex


def foreground_check(command, arguments, data, limit):
    """Allow a few status observations, then hand repetitive polling to the worker."""
    words = shlex.split(command)
    cwd = arguments.get('Cwd') or arguments.get('cwd') or next(iter(data.get('workspacePaths', [])), os.getcwd())
    if len(words) > 3 and words[0] == 'cd' and words[2] == '&&':
        cwd = str((Path(cwd) / words[1]).resolve())
        words = words[3:]
    if not words or Path(words[0]).name != 'gh':
        return None
    guidance = ('Use one bounded ci-status-monitor for the exact Git-derived SHA and selected runs. '
                'Do not replace it with timers, gh api polling, duplicate agents or automatic reruns. '
                'If only CI remains, report PENDING CI and end the turn; failed diagnostics/logs may still be read.')
    if words[1:3] == ['run', 'watch'] or words[1:3] == ['pr', 'checks'] and '--watch' in words:
        return guidance
    status_read = words[1:3] in (['run', 'list'], ['run', 'view'], ['pr', 'checks'])
    if '--log' in words or '--log-failed' in words:
        return None  # Reading a concrete failure is investigation, not a status wait.
    if words[1:2] == ['api']:
        if any(word.upper() in ('POST', 'PATCH', 'PUT', 'DELETE') for word in words):
            return None
        endpoints = [word for word in words[2:] if '/actions/' in word]
        if any(word.rstrip('/').endswith('/logs') for word in endpoints):
            return None
        status_read = bool(endpoints)
    if not status_read:
        return None
    try:
        root, head = subprocess.check_output(
            ['git', '-C', str(cwd), 'rev-parse', '--show-toplevel', 'HEAD'],
            stderr=subprocess.DEVNULL, timeout=2, text=True).splitlines()
    except (OSError, ValueError, subprocess.SubprocessError):
        return 'Cannot bind foreground CI observation to a local Git HEAD. ' + guidance
    key = hashlib.sha256(json.dumps([data['conversationId'], root, head]).encode()).hexdigest()
    directory = Path.home() / '.gemini/config/hooks/ci-monitor'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ('foreground-' + key + '.json')
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {'reads': 0}
        if state['reads'] >= limit:
            return 'Foreground CI observation budget reached for HEAD ' + head + '. ' + guidance
        state['reads'] += 1
        path.write_text(json.dumps(state))
    return None


class MonitorCancelled(Exception):
    pass


def gh_json(arguments, timeout):
    child = subprocess.Popen(['gh', *arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        env={**os.environ, 'GH_PROMPT_DISABLED': '1', 'GH_PAGER': 'cat', 'NO_COLOR': '1'})
    try:
        output, _ = child.communicate(timeout=timeout)
        if child.returncode:
            raise RuntimeError('Run unavailable or GitHub CLI failed (exit ' + str(child.returncode) + ').')
        return json.loads(output)
    finally:
        handlers = {sig: signal.signal(sig, signal.SIG_IGN)
                    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        try:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    break
                if sig == signal.SIGTERM:
                    try:
                        child.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        pass
            child.wait(timeout=1)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def validate(repo, sha, run_ids, seconds, interval, command_seconds):
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            or not re.fullmatch(r'[a-fA-F0-9]{40}', sha)
            or len(set(run_ids)) != len(run_ids) or len(run_ids) > 20
            or any(type(value) is not int or value <= 0 for value in run_ids)
            or not 0 < seconds <= 1800 or not 2 <= interval <= 60 or not 0 < command_seconds <= 60):
        raise ValueError('Require owner/repo, full commit SHA, up to20 unique positive run IDs, finite <=1800s budget, 2..60s interval and <=60s command budget.')


def monitor(repo, sha, run_ids=None, seconds=600, interval=15, command_seconds=15):
    run_ids = [] if run_ids is None else run_ids
    validate(repo, sha, run_ids, seconds, interval, command_seconds)
    directory = Path.home() / '.gemini/config/hooks/ci-monitor'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256((repo + ':' + sha.lower()).encode()).hexdigest()
    with (directory / (key + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'BLOCKED', 'repo': repo, 'sha': sha.lower(), 'run_ids': list(run_ids),
                    'runs': [], 'polls': 0, 'elapsed_seconds': 0,
                    'reason': 'An independent monitor already owns this repository/commit; do not launch another.'}
        return poll(repo, sha, run_ids, seconds, interval, command_seconds)


def poll(repo, sha, run_ids, seconds, interval, command_seconds):
    sha = sha.lower()
    start = time.monotonic()
    deadline = start + seconds
    report = {'status': 'BLOCKED', 'repo': repo, 'sha': sha, 'run_ids': list(run_ids),
              'runs': [], 'polls': 0, 'reason': '', 'elapsed_seconds': 0}

    def finish(status, reason):
        report.update(status=status, reason=reason, elapsed_seconds=round(time.monotonic() - start, 3))
        return report

    failures = {'failure', 'cancelled', 'timed_out', 'startup_failure', 'action_required', 'stale'}
    pending = {'queued', 'in_progress', 'requested', 'waiting', 'pending'}
    try:
        if not run_ids:
            try:
                discovered = gh_json(['run', 'list', '--repo', repo, '--commit', sha, '--limit', '21',
                    '--json', 'databaseId,headSha,status,conclusion,url'], min(command_seconds, max(0.001, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                return finish('TIMED_OUT' if time.monotonic() >= deadline else 'BLOCKED',
                              'Bounded CI discovery command timed out; no automatic retry.')
            if not isinstance(discovered, list) or not discovered or len(discovered) > 20:
                return finish('BLOCKED', 'No runs discovered or discovery scope exceeds20; missing/partial discovery is not PASS.')
            if any(not isinstance(run, dict) or type(run.get('databaseId')) is not int or run['databaseId'] <= 0
                   or not isinstance(run.get('headSha'), str) or run['headSha'].lower() != sha for run in discovered):
                return finish('BLOCKED', 'Discovery returned missing run identity or a different commit SHA.')
            run_ids = [run['databaseId'] for run in discovered]
            if len(set(run_ids)) != len(run_ids):
                return finish('BLOCKED', 'Duplicate discovery run IDs prevent reliable scope verification.')
            report['run_ids'] = run_ids
        while time.monotonic() < deadline:
            report['polls'] += 1
            snapshots = []
            for run_id in run_ids:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return finish('TIMED_OUT', 'Absolute CI monitoring deadline elapsed; pending checks are not PASS.')
                print('[ci-monitor] Inspecting run ' + str(run_id) + '; deadline remaining ' + str(int(remaining)) + 's.', file=sys.stderr, flush=True)
                try:
                    run = gh_json(['run', 'view', str(run_id), '--repo', repo, '--json',
                                   'databaseId,headSha,status,conclusion,jobs,url'], min(command_seconds, remaining))
                except subprocess.TimeoutExpired:
                    status = 'TIMED_OUT' if time.monotonic() >= deadline else 'BLOCKED'
                    return finish(status, 'Bounded GitHub read command timed out; no automatic retry.')
                if not isinstance(run, dict) or run.get('databaseId') != run_id or run.get('headSha', '').lower() != sha:
                    return finish('BLOCKED', 'Returned run identity/commit does not match the requested run and exact SHA.')
                snapshots.append(run)
                report['runs'] = snapshots
            all_passed = True
            for run in snapshots:
                status, conclusion, jobs = run.get('status'), run.get('conclusion'), run.get('jobs')
                if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
                    return finish('BLOCKED', 'Run has missing or malformed job evidence.')
                if conclusion in failures or any(job.get('conclusion') in failures for job in jobs):
                    return finish('FAIL', 'A requested run/job failed, was cancelled, or timed out; no automatic retry.')
                if status in pending:
                    all_passed = False
                    continue
                if status != 'completed' or conclusion != 'success' or not jobs:
                    return finish('BLOCKED', 'Completed run lacks successful nonempty job evidence (missing, skipped or neutral is not PASS).')
                for job in jobs:
                    if job.get('status') != 'completed' or job.get('conclusion') != 'success':
                        return finish('BLOCKED', 'Skipped, neutral, incomplete or unknown job is not a passing check.')
            if all_passed:
                return finish('PASS', 'Every explicitly requested run and its nonempty jobs succeeded for the exact commit.')
            print('[ci-monitor] Pending CI for ' + repo + ' @ ' + sha + '; finite deadline remains.', file=sys.stderr, flush=True)
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
        return finish('TIMED_OUT', 'Absolute CI monitoring deadline elapsed; pending/missing completion is not PASS.')
    except MonitorCancelled:
        return finish('BLOCKED', 'CI monitoring cancelled; no approval or automatic retry.')
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError) as exc:
        return finish('BLOCKED', 'Read-only CI evidence unavailable (' + type(exc).__name__ + '); no automatic retry.')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--run-id', type=int, action='append')
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--interval', type=float, default=15)
    parser.add_argument('--command-seconds', type=float, default=15)
    args = parser.parse_args(argv)
    try:
        validate(args.repo, args.sha, args.run_id or [], args.seconds, args.interval, args.command_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main():
    args = parse_args()
    def cancel(signum, frame):
        raise MonitorCancelled()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancel)
    report = monitor(args.repo, args.sha, args.run_id, args.seconds, args.interval, args.command_seconds)
    print(json.dumps(report))
    return {'PASS': 0, 'FAIL': 1, 'BLOCKED': 2, 'TIMED_OUT': 124}[report['status']]


if __name__ == '__main__':
    sys.exit(main())
