#!/usr/bin/env python3
"""Independent command-classification examples; never executes shell commands."""
from pathlib import Path
import runpy
import shlex


def check():
    is_test_command = runpy.run_path(str(Path(__file__).with_name('test-command.py')))['is_test_command']
    actual_tests = [
        'pytest -q', 'py.test tests/', '/usr/local/bin/pytest -q',
        'vitest run', './node_modules/.bin/vitest run', '"/tmp/project space/vitest" run',
        'npx vitest run', 'npx --yes vitest run',
        'npx vitest@latest run', 'node --test',
        'npm test', 'npm run test:unit -- --runInBand',
        'npm --prefix apps/web run test:unit -- --runInBand',
        'npm run --workspace web test', 'npm exec --workspace web -- vitest run',
        'uv run pytest -q', 'poetry run pytest -q', 'pipenv run pytest -q',
        'pnpm --filter web test:unit', 'yarn test', 'bun test',
        'pnpm dlx vitest run', 'yarn dlx vitest run', 'yarn workspace web test',
        'python3 -m pytest -q', 'python3 -m unittest discover',
        'python3 ./test_x.py', 'node scripts/test-unit.js', './scripts/test.sh',
        'go test ./...', 'cargo test', 'swift test', 'make test', 'make integration-test',
        'sh -c "pytest -q"', 'bash -lc "npx vitest run"',
        'echo preparing && pytest -q', 'cat README.md; npm test',
        'pytest --help && pytest -q', 'cd apps/web && npm test',
        shlex.join(['python3', '-c', "import pytest; pytest.main(['-q'])"]),
        shlex.join(['python3', '-c', 'import unittest; unittest.main()']),
    ]
    mentions_only = [
        'echo pytest', "printf '%s' 'npx vitest run'", 'grep vitest package.json',
        "rg 'pytest.main' src", 'git diff -- tests/test_x.py',
        'git show HEAD:apps/docs/vitest.config.ts', 'cat tests/test_sample.py',
        'cat apps/docs/vitest.config.ts', 'node scripts/build.js',
        'npm --prefix apps/docs run build', 'python3 ./contest.py',
        'pytest --help', 'pytest -h', 'pytest --version',
        'vitest --help', 'vitest --version', 'python3 -m pytest --version',
        '# pytest -q', '# npx vitest run\necho hello', 'echo hello # pytest -q',
        'sh -c "echo pytest"', 'bash -lc "cat tests/test_sample.py"',
        shlex.join(['python3', '-c', "open('apps/docs/vitest.config.ts').read()"]),
        shlex.join(['python3', '-c', "print('pytest.main()')"]),
        shlex.join(['python3', '-c', "# pytest.main()\nprint('hello')"]),
        shlex.join(['node', '-e', "console.log('vitest run')"]),
    ]
    for command in actual_tests:
        assert is_test_command(command) is True, ('missed explicit test invocation', command)
    for command in mentions_only:
        assert is_test_command(command) is False, ('read/mention incorrectly gated as a test', command)
    print('PASS: explicit test runners, interpreter calls, shell segments, read-only mentions, quoted code and help/version')


if __name__ == '__main__':
    check()
