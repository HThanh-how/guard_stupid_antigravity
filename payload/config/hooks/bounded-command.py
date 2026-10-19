#!/usr/bin/env python3
"""Run one shell command with a wall deadline, idle deadline and owned process group."""
import argparse
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


class CommandCancelled(Exception):
    pass


def run(command, seconds=600, idle=180, grace=2):
    child = subprocess.Popen([os.environ.get('SHELL', '/bin/zsh'), '-lc', command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    selector = selectors.DefaultSelector()
    for stream, output in [(child.stdout, sys.stdout), (child.stderr, sys.stderr)]:
        selector.register(stream, selectors.EVENT_READ, output.fileno())
    start = last_output = time.monotonic()
    timeout_reason = None

    def kill_group(sig):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass

    def cancel(signum, frame):
        raise CommandCancelled(signum)

    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        while selector.get_map() or child.poll() is None:
            now = time.monotonic()
            if now - start >= seconds or (idle and now - last_output >= idle):
                timeout_reason = 'wall deadline' if now - start >= seconds else 'no output / idle deadline'
                break
            for key, _ in selector.select(timeout=min(0.2, seconds - (now - start))):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if chunk:
                    last_output = time.monotonic()
                    os.write(key.data, chunk)
                else:
                    selector.unregister(key.fileobj)
        if timeout_reason:
            print('\n[guard] TIMEOUT: ' + timeout_reason + '; terminating only this command process group. Diagnose before retrying; this is not a passing check.', file=sys.stderr, flush=True)
            return 124
        code = child.wait(timeout=grace)
        return code if code >= 0 else 128 - code
    except CommandCancelled:
        return 130
    finally:
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        # The group is ours, including any subprocesses inheriting its pipes.
        kill_group(signal.SIGTERM)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # Give descendants grace even if the shell already exited.
        end = time.monotonic() + grace
        while time.monotonic() < end:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        kill_group(signal.SIGKILL)
        child.wait(timeout=grace)
        selector.close()
        child.stdout.close()
        child.stderr.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--idle', type=float, default=180)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    words = args.command[1:] if args.command[:1] == ['--'] else args.command
    if len(words) != 1 or not 0 < args.seconds <= 3600 or not 0 <= args.idle <= args.seconds:
        parser.error('Provide one quoted command; wall limit 0..3600 seconds, idle 0..wall limit')
    sys.exit(run(words[0], args.seconds, args.idle))
