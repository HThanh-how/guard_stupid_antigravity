"""Offline Windows integration checks; no AGY session, network, or user config writes."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
HOOKS = ROOT / 'payload/config/hooks'
sys.path.insert(0, str(HOOKS))
import guard_platform as platform


def owned(command, *, timeout=15, input=None, env=None):
    child = platform.spawn(command, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        out, err = child.communicate(input, timeout=timeout)
        return child.returncode, out.decode('utf-8'), err.decode('utf-8')
    finally:
        platform.cleanup(child)


def alive(pid, wait_ms=0):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        return False
    try:
        return kernel.WaitForSingleObject(handle, wait_ms) == 258
    finally:
        kernel.CloseHandle(handle)


@unittest.skipUnless(os.name == 'nt', 'Windows native integration checks')
class WindowsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='guard-windows-')
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name) / 'Nhà có khoảng trắng'
        self.home.mkdir()
        self.env = {**os.environ, 'HOME': str(self.home), 'USERPROFILE': str(self.home),
                    'PYTHONUTF8': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_CONFIG_NOSYSTEM': '1', 'AGY_GUARD_ROLE': ''}

    def hook(self, data, event='pre', role=''):
        code, out, err = owned([sys.executable, '-X', 'utf8', str(HOOKS / 'hook-runner.py'), event],
                               input=json.dumps(data).encode(), env={**self.env, 'AGY_GUARD_ROLE': role})
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def data(self, command='', name='run_command', conversation='windows-check'):
        return {'conversationId': conversation, 'workspacePaths': [str(self.home)],
                'toolCall': {'name': name, 'args': {'CommandLine': command, 'Cwd': str(self.home)}}}

    def bounded(self, command, seconds=5, idle=0):
        encoded = base64.b64encode(command.encode()).decode()
        return owned([sys.executable, '-X', 'utf8', str(HOOKS / 'bounded-command.py'),
                      '--seconds', str(seconds), '--idle', str(idle), '--encoded', '--', encoded])

    def test_windows_paths_test_detection_and_literal_quoting(self):
        values = [r'C:\Program Files\Python\python.exe', r'D:\Project Space\O\Brien', "O'Brien", 'trích dẫn']
        self.assertEqual(platform.split(platform.join(values)), values)
        detector = runpy.run_path(str(HOOKS / 'test-command.py'))['is_test_command']
        for command in (r'& "C:\Program Files\Python\python.exe" -X utf8 -m unittest discover',
                        r'npm.cmd test', r'& "C:\Tools\pytest.exe" -q',
                        r'python.exe -X utf8 C:\tests\test_sample.py',
                        r'powershell.exe -Command "npm test"'):
            self.assertTrue(detector(command), command)
        self.assertFalse(detector('Get-Content C:\\tests\\test_sample.py'))

    def test_exit_codes_unicode_and_deadlines(self):
        code, out, err = self.bounded('Write-Output "Tiếng Việt còn nguyên dấu"')
        self.assertEqual(code, 0, err)
        self.assertIn('Tiếng Việt còn nguyên dấu', out)
        self.assertNotIn('CLIXML', err)
        self.assertEqual(self.bounded('exit 7')[0], 7)
        self.assertEqual(self.bounded('Write-Error "expected failure"')[0], 1)
        started = time.monotonic()
        code, _, err = self.bounded('Start-Sleep -Seconds 20', seconds=1)
        self.assertEqual(code, 124, err)
        self.assertIn('wall deadline', err)
        self.assertLess(time.monotonic() - started, 5)
        code, out, err = self.bounded('Write-Output started; Start-Sleep -Seconds 20', seconds=6, idle=1)
        self.assertEqual(code, 124, err)
        self.assertIn('started', out)
        self.assertIn('idle deadline', err)

    def test_owned_job_cleans_orphans_and_survives_owner_death(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(lambda: (unrelated.terminate(), unrelated.wait(timeout=3)))
        source = ("import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
                  "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True)")
        child = platform.spawn([sys.executable, '-c', source], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            output, _ = child.communicate(timeout=4)
            pid = int(output)
            self.assertTrue(alive(pid))
        finally:
            platform.cleanup(child)
        # TerminateJobObject stops execution before Windows finishes signaling
        # every process handle. Allow that bounded kernel teardown to complete.
        self.assertFalse(alive(pid, 2000), 'descendant escaped after its parent exited')
        source = (f"import sys,os; sys.path.insert(0,{str(HOOKS)!r}); import guard_platform as p; "
                  "c=p.spawn([sys.executable,'-c','import time; time.sleep(30)']); print(c.pid,flush=True); os._exit(0)")
        result = subprocess.run([sys.executable, '-c', source], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        pid = int(result.stdout)
        until = time.monotonic() + 2
        while alive(pid) and time.monotonic() < until:
            time.sleep(0.02)
        self.assertFalse(alive(pid), 'job survived abrupt owner termination')
        self.assertIsNone(unrelated.poll(), 'cleanup affected an unrelated process')

    def test_lock_exclusion_and_hook_deadline(self):
        state = self.home / '.gemini/config/hooks/state'
        state.mkdir(parents=True)
        path = state / (hashlib.sha256(b'windows-check').hexdigest() + '.lock')
        source = (f"import sys; sys.path.insert(0,{str(HOOKS)!r}); import guard_platform as p; "
                  "f=open(sys.argv[1],'a'); p.flock(f,p.LOCK_EX|p.LOCK_NB)")
        with path.open('a') as lock:
            platform.flock(lock, platform.LOCK_EX)
            code, _, err = owned([sys.executable, '-c', source, str(path)])
            self.assertNotEqual(code, 0)
            self.assertIn('BlockingIOError', err)
            started = time.monotonic()
            result = self.hook(self.data('Write-Output harmless'))
            self.assertEqual(result['decision'], 'deny')
            self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(owned([sys.executable, '-c', source, str(path)])[0], 0)

    def test_hook_roles_invalid_input_and_review_gates(self):
        self.assertEqual(self.hook({}, role='')['decision'], 'deny')
        for role in ('readonly-independent', 'intent-classifier', 'ci-monitor'):
            self.assertEqual(self.hook(self.data('Write-Output unsafe'), role=role)['decision'], 'deny')
            self.assertEqual(self.hook(self.data(name='finish'), role=role)['decision'], 'allow')
        helper = platform.python_command(HOOKS / 'ci-monitor.py', '--repo', 'owner/repo', '--sha', 'a' * 40, '--seconds', 480)
        self.assertEqual(self.hook(self.data(helper), role='ci-monitor')['decision'], 'allow')
        self.assertEqual(self.hook(self.data(helper + '; Write-Output unsafe'), role='ci-monitor')['decision'], 'deny')
        self.assertEqual(self.hook(self.data('Remove-Item -Recurse -Force backups'))['decision'], 'deny')
        self.assertEqual(self.hook(self.data('git.exe reset --hard'))['decision'], 'force_ask')
        self.assertEqual(self.hook(self.data('powershell.exe -Command "npm test"'))['decision'], 'deny')
        subprocess.run(['git', 'init', '-q', '-b', 'feature/windows', str(self.home)], env=self.env, check=True)
        for command in ('git.exe add .', 'npm.cmd test'):
            result = self.hook(self.data(command))
            self.assertEqual(result['decision'], 'deny')
            self.assertIn('REVIEW REQUIRED', result['reason'])
            self.assertIn(sys.executable, result['reason'])
            self.assertEqual(self.hook(self.data(command), event='dispatch'), {})
        subprocess.run(['git', '-C', str(self.home), 'symbolic-ref', 'HEAD', 'refs/heads/main'], env=self.env, check=True)
        result = self.hook(self.data('git.exe commit -m unsafe'))
        self.assertEqual(result['decision'], 'deny')
        self.assertIn('Protected branch', result['reason'])

    def test_overwritten_command_keeps_quoting_and_unicode(self):
        command = 'Write-Output "nguyên dấu và khoảng trắng"; Write-Output \'O\'\'Brien\''
        result = self.hook(self.data(command))
        self.assertEqual(result['decision'], 'allow', result)
        code, out, err = owned(platform.powershell(result['overwrite']['CommandLine']), env=self.env)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ['nguyên dấu và khoảng trắng', "O'Brien"])

    def test_large_agent_input_and_incomplete_reports_offline(self):
        module = runpy.run_path(str(HOOKS / 'independent-agent.py'))
        fake = self.home / 'fake_cli.py'
        fake.write_text('''import json,os,re,sys
from pathlib import Path
prompt=sys.argv[sys.argv.index('-p')+1]
request=Path(re.search(r'file at (.*?)\\. Treat',prompt)[1])
assert request.parent.as_posix() in [Path(a).as_posix() for a in sys.argv]
assert request.read_text(encoding='utf-8').count('Yêu cầu gốc')==5000
assert os.environ['AGY_GUARD_ROLE']=='readonly-independent'
assert len(subprocess_command := ' '.join(sys.argv)) < 32000
result={'status':'SUCCESS','structured_output':{'decision':'PASS'}}
if os.environ.get('FAKE_MODE')=='denied':result['denied_actions']=['view_file']
if os.environ.get('FAKE_MODE')=='empty':result['structured_output']={}
print(json.dumps(result))
''', encoding='utf-8')
        actual_spawn = platform.spawn
        seen = []

        def fake_spawn(command, **kwargs):
            seen.append(command)
            return actual_spawn([sys.executable, '-X', 'utf8', str(fake), *command[1:]], **kwargs)

        schema = {'type': 'object', 'properties': {'decision': {'type': 'string', 'enum': ['PASS']}}, 'required': ['decision']}
        with mock.patch.object(platform, 'spawn', side_effect=fake_spawn):
            for mode in ('good', 'denied', 'empty'):
                with mock.patch.dict(os.environ, {**self.env, 'FAKE_MODE': mode}):
                    if mode == 'good':
                        self.assertEqual(module['agent_result']('genoffice-reviewer', 'Yêu cầu gốc\n' * 5000, str(self.home), schema), {'decision': 'PASS'})
                    else:
                        with self.assertRaises((module['ReviewIncomplete'], ValueError)):
                            module['agent_result']('genoffice-reviewer', 'Yêu cầu gốc\n' * 5000, str(self.home), schema)
        for command in seen:
            directory = Path(command[command.index('--add-dir') + 1])
            self.assertFalse(directory.exists(), 'temporary agent input was not cleaned')

    def test_installed_hook_commands_run_in_windows_shell(self):
        code, _, err = owned([sys.executable, '-X', 'utf8', str(ROOT / 'install.py'), '--home', str(self.home),
                              '--agy', sys.executable], env=self.env)
        self.assertEqual(code, 0, err)
        hooks = json.loads((self.home / '.gemini/config/hooks.json').read_text(encoding='utf-8'))
        command = hooks['genoffice-agent-guard']['PreToolUse'][0]['hooks'][0]['command']
        code, out, err = owned(platform.powershell(command), input=json.dumps(self.data('Write-Output ok')).encode(), env=self.env)
        self.assertEqual(code, 0, err)
        self.assertIn('overwrite', json.loads(out))


if __name__ == '__main__':
    unittest.main(verbosity=2)
