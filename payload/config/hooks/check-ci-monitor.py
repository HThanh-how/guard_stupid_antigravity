#!/usr/bin/env python3
"""Local CI-worker regression checks. Fake gh only; never contacts GitHub."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SHA = 'a' * 40


def check():
    module = runpy.run_path(str(HERE / 'ci-monitor.py'))
    with tempfile.TemporaryDirectory(prefix='ci-monitor-check-') as temporary:
        home = Path(temporary)
        binary = home / 'bin/gh'
        binary.parent.mkdir()
        log, ready = home / 'calls.jsonl', home / 'ready.json'
        binary.write_text(f'''#!{sys.executable}
import json, os, signal, sys, time
from pathlib import Path
log = Path(os.environ['FAKE_LOG'])
previous = log.read_text().splitlines() if log.exists() else []
with log.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')
args = sys.argv[1:]
assert args[:2] in (['run', 'view'], ['run', 'list']), args
assert args[args.index('--repo')+1] == 'owner/repo', args
mode = os.environ['FAKE_MODE']
if mode == 'unavailable': print('PRIVATE_RAW_GITHUB_ERROR', file=sys.stderr); sys.exit(4)
if mode == 'malformed': print('not JSON'); sys.exit(0)
if mode in ('hang', 'tree-hang'):
    if mode == 'tree-hang':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pid = os.fork()
        if pid == 0: time.sleep(30); os._exit(0)
        Path(os.environ['FAKE_READY']).write_text(json.dumps([os.getpid(), pid]))
    time.sleep(30)
if args[:2] == ['run', 'list']:
    assert args[args.index('--commit')+1] == '{SHA}'
    values = [] if mode == 'empty-discovery' else [{{'databaseId': 101, 'headSha': '{SHA}'}}]
    if mode == 'wide-discovery': values = [{{'databaseId': number+1, 'headSha': '{SHA}'}} for number in range(21)]
    if mode == 'wrong-discovery': values[0]['headSha'] = 'b'*40
    print(json.dumps(values)); sys.exit(0)
run_id = int(args[2])
run = {{'databaseId': run_id, 'headSha': '{SHA}', 'status': 'completed', 'conclusion': 'success',
        'url': 'https://example.invalid/run/'+str(run_id),
        'jobs': [{{'databaseId': run_id*10, 'name': 'unit', 'status': 'completed', 'conclusion': 'success'}}]}}
if mode == 'wrong-sha': run['headSha'] = 'b'*40
if mode == 'wrong-id': run['databaseId'] = run_id+1
if mode == 'no-jobs': run['jobs'] = []
if mode == 'skipped': run['jobs'][0]['conclusion'] = 'skipped'
if mode == 'neutral': run['conclusion'] = 'neutral'
if mode == 'failed': run['conclusion'] = run['jobs'][0]['conclusion'] = 'failure'
if mode == 'cancelled': run['conclusion'] = 'cancelled'
if mode == 'pending' or (mode == 'pending-once' and not previous):
    run.update(status='queued', conclusion='', jobs=[])
print(json.dumps(run))
''')
        binary.chmod(0o700)
        env = {**os.environ, 'HOME': str(home), 'PATH': str(binary.parent) + os.pathsep + os.environ['PATH'],
               'FAKE_LOG': str(log), 'FAKE_READY': str(ready)}

        def call(mode, run_ids=(101,), **limits):
            log.unlink(missing_ok=True)
            with patch.dict(os.environ, {**env, 'FAKE_MODE': mode}, clear=True):
                result = module['monitor']('owner/repo', SHA, list(run_ids), seconds=limits.get('seconds', 5),
                    interval=2, command_seconds=limits.get('command_seconds', 2))
            assert result['repo'] == 'owner/repo' and result['sha'] == SHA, result
            assert result['status'] in ('PASS', 'FAIL', 'BLOCKED', 'TIMED_OUT'), result
            return result

        success = call('success', (101, 202))
        assert success['status'] == 'PASS', (success, log.read_text())
        for mode in ('wrong-sha', 'wrong-id', 'no-jobs', 'skipped', 'neutral', 'unavailable', 'malformed'):
            result = call(mode)
            assert result['status'] == 'BLOCKED', (mode, result)
            assert 'PRIVATE_RAW_GITHUB_ERROR' not in json.dumps(result)
            assert len(log.read_text().splitlines()) == 1, ('unexpected retry', mode)
        for mode in ('failed', 'cancelled'):
            assert call(mode)['status'] == 'FAIL', mode
        assert call('pending', seconds=0.15)['status'] == 'TIMED_OUT'
        with patch('time.sleep', lambda seconds: None):
            progressed = call('pending-once')
        assert progressed['status'] == 'PASS' and progressed['polls'] == 2, progressed
        assert call('hang', command_seconds=0.1)['status'] == 'BLOCKED'
        assert call('hang', seconds=0.1, command_seconds=0.3)['status'] == 'TIMED_OUT'
        for mode in ('empty-discovery', 'wide-discovery', 'wrong-discovery'):
            assert call(mode, ())['status'] == 'BLOCKED', mode
            assert len(log.read_text().splitlines()) == 1, 'missing discovery was retried'
        discovered = call('success', ())
        assert discovered['status'] == 'PASS' and discovered['run_ids'] == [101], discovered
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert calls[0][:2] == ['run', 'list'] and calls[1][:2] == ['run', 'view'], calls

        base_args = ['--repo', 'owner/repo', '--sha', SHA]
        assert module['parse_args'](base_args).seconds == 600
        for extra in (['--seconds', '1801'], ['--sha', 'main'], ['--run-id', '0'],
                      ['--interval', '0.1'], ['--rerun'], ['--run-id', '101', '--run-id', '101']):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    module['parse_args'](base_args + extra)
                except SystemExit as exc:
                    assert exc.code != 0
                else:
                    raise AssertionError(('unsafe CLI accepted', extra))

        # One worker owns repo+SHA. Cancelling it cleans only its own gh tree.
        log.unlink(missing_ok=True)
        argv = [sys.executable, str(HERE / 'ci-monitor.py'), *base_args, '--run-id', '101',
                '--seconds', '10', '--command-seconds', '8']
        owner = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**env, 'FAKE_MODE': 'tree-hang'})
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), 'fake gh never started'
            duplicate = subprocess.run(argv, text=True, capture_output=True,
                env={**env, 'FAKE_MODE': 'success'}, timeout=2)
            assert duplicate.returncode == 2 and json.loads(duplicate.stdout)['status'] == 'BLOCKED', duplicate.stdout
            assert len(log.read_text().splitlines()) == 1, 'duplicate monitor polled again'
            owner.send_signal(signal.SIGTERM)
            time.sleep(0.03)
            owner.send_signal(signal.SIGTERM)
            out, err = owner.communicate(timeout=2)
            assert owner.returncode == 2 and json.loads(out)['status'] == 'BLOCKED', (out, err)
            assert unrelated.poll() is None, 'cancellation killed an unrelated process'
            for pid in json.loads(ready.read_text()):
                process = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=1)
                assert not process.stdout.strip() or process.stdout.strip().startswith('Z'), ('owned gh descendant survived', pid)
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.communicate(timeout=2)
            unrelated.terminate()
            unrelated.wait(timeout=2)
        print('PASS: exact SHA/run scope, nonempty successful jobs, discovery, terminal failures, absolute/command deadlines, one-worker lock and owned cancellation')


if __name__ == '__main__':
    check()
