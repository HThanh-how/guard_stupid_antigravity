#!/usr/bin/env python3
"""Independent regression checks. Every child belongs to this check; no network."""
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

SCRIPT = Path(__file__).with_name('bounded-command.py')
HARNESS = "import runpy,sys; sys.exit(runpy.run_path(sys.argv[1])['run'](sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), grace=0.1))"


def launch(command, seconds=0.7, idle=0):
    return subprocess.Popen([sys.executable, '-c', HARNESS, str(SCRIPT), command,
                             str(seconds), str(idle)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            env={**os.environ, 'SHELL': '/bin/sh'})


def command(source):
    return shlex.join([sys.executable, '-u', '-c', source])


def alive(pid):
    result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                            capture_output=True, text=True, timeout=1)
    return bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')


def wait_file(path, child):
    deadline = time.monotonic() + 1
    while not path.exists() and time.monotonic() < deadline:
        assert child.poll() is None, 'runner exited before command started'
        time.sleep(0.01)
    assert path.exists(), 'command never started'


def check():
    for code in (0, 7):
        child = launch(command(f"import sys; print('stdout'); print('stderr', file=sys.stderr); sys.exit({code})"))
        out, err = child.communicate(timeout=2)
        assert child.returncode == code, (child.returncode, out, err)
        assert 'stdout' in out and 'stderr' in err, (out, err)

    start = time.monotonic()
    child = launch(command("import time\nwhile True:\n print('active', flush=True); time.sleep(0.02)"), seconds=0.4, idle=0.2)
    out, err = child.communicate(timeout=2)
    assert child.returncode == 124 and 'wall deadline' in err, (child.returncode, err)
    assert out.count('active') >= 2 and time.monotonic() - start < 2, out

    child = launch(command("import time; print('started', flush=True); time.sleep(30)"), seconds=0.9, idle=0.2)
    out, err = child.communicate(timeout=2)
    assert child.returncode == 124 and 'idle deadline' in err, (child.returncode, out, err)

    # Cancellation and timeout must clean the whole owned group, while an
    # unrelated same-executable process survives both cleanup paths.
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        with tempfile.TemporaryDirectory(prefix='bounded-check-') as temporary:
            for action in ('cancel', 'timeout'):
                pid_file = Path(temporary) / action
                source = ("import os,time\n"
                          "pid=os.fork()\n"
                          "if pid == 0:\n time.sleep(30); os._exit(0)\n"
                          f"open({str(pid_file)!r}, 'w').write(str(pid))\n"
                          "time.sleep(30)")
                child = launch(command(source), seconds=0.7)
                try:
                    wait_file(pid_file, child)
                    descendant = int(pid_file.read_text())
                    assert alive(descendant), 'descendant did not start'
                    if action == 'cancel':
                        child.send_signal(signal.SIGTERM)
                    out, err = child.communicate(timeout=2)
                    expected = 130 if action == 'cancel' else 124
                    assert child.returncode == expected, (action, child.returncode, out, err)
                    assert not alive(descendant), (action, 'owned descendant survived', descendant)
                    assert unrelated.poll() is None, 'cleanup killed unrelated process'
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=2)
                    if pid_file.exists() and alive(int(pid_file.read_text())):
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=2)
    print('PASS: exit codes, output, wall and idle deadlines, cancellation, descendants and unrelated process isolation')


if __name__ == '__main__':
    check()
