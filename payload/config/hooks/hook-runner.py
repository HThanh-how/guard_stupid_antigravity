"""Run a hook with an external deadline on Windows, including blocked stdin/locks."""
import json
from pathlib import Path
import subprocess
import sys
import guard_platform as platform


def main():
    event = sys.argv[1]
    child = platform.spawn([sys.executable, '-X', 'utf8',
                            str(Path(__file__).with_name('review-gate.py')), event],
                           stdin=sys.stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = child.communicate(timeout=8)
        if child.returncode or not isinstance(json.loads(output), dict):
            raise ValueError('Hook did not produce a successful JSON object')
        sys.stdout.buffer.write(output)
        sys.stderr.buffer.write(errors)
    except Exception as exc:
        result = {'decision': 'deny' if event == 'pre' else 'allow',
                  'reason': 'Review guard unavailable: ' + type(exc).__name__ + '. Report BLOCKED; never claim PASS.'}
        print(json.dumps(result) if event in ('pre', 'stop') else '{}')
    finally:
        platform.cleanup(child)


if __name__ == '__main__':
    main()
