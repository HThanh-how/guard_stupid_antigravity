#!/usr/bin/env python3
"""Recognize common test invocations, not incidental test names in read commands."""
import ast
from pathlib import PurePath
import re
import shlex

RUNNERS = {'pytest', 'py.test', 'vitest', 'jest', 'mocha', 'ava', 'playwright', 'cypress', 'ctest', 'phpunit', 'rspec', 'nosetests'}
MANAGERS = {'npm', 'pnpm', 'yarn', 'bun'}


def strip_options(args, value_flags):
    while args and args[0].startswith('-'):
        if args[0] == '--':
            return args[1:]
        args = args[2:] if args[0] in value_flags else args[1:]
    return args


def test_script(path):
    name = PurePath(path).name.lower()
    return (bool(re.search(r'(^|[_.-])(?:tests?|spec)(?:[_.-]|$)', name))
            or any(part in ('test', 'tests', '__tests__', 'e2e', 'spec') for part in PurePath(path).parts))


def invocation(words):
    while words and re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]):
        words = words[1:]
    if not words:
        return False
    name = PurePath(words[0]).name
    args = words[1:]
    if name in ('env', 'command', 'exec', 'nohup'):
        while args and (args[0].startswith('-') or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', args[0])):
            skip = 2 if args[0] in ('-u', '--unset') else 1
            args = args[skip:]
        return invocation(args)
    if name in ('timeout', 'gtimeout'):
        while args and args[0].startswith('-'):
            args = args[2:] if args[0] in ('-k', '--kill-after', '-s', '--signal') else args[1:]
        return invocation(args[1:]) if args else False
    if name in ('sh', 'bash', 'zsh'):
        index = next((i for i, arg in enumerate(args) if re.fullmatch(r'-[A-Za-z]*c[A-Za-z]*', arg)), None)
        if index is not None:
            return is_test_command(args[index + 1]) if index + 1 < len(args) else False
    if name in RUNNERS or name.split('@', 1)[0] in RUNNERS:
        return not any(arg in ('--help', '-h', '--version') for arg in args)
    if name in MANAGERS or name in ('npx', 'bunx'):
        value_flags = {'--prefix', '--cwd', '-C', '--dir', '--filter', '-F', '--workspace', '-p', '--package'}
        if name == 'npm':
            value_flags.add('-w')
        args = strip_options(args, value_flags)
        if name == 'yarn' and args[:1] == ['workspace'] and len(args) > 2:
            return invocation(['yarn', *args[2:]])
        if args[:1] in (['exec'], ['run'], ['run-script'], ['dlx']):
            args = args[1:]
        args = strip_options(args, value_flags)
        if not args:
            return False
        return bool(re.match(r'^(?:test(?:[\w:.-]*)|coverage)$', args[0])) or invocation(args)
    if name in ('uv', 'poetry', 'pipenv'):
        value_flags = {'--directory', '--project', '--python', '--with', '--group', '-C', '-P'}
        args = strip_options(args, value_flags)
        return bool(args[:1] == ['run']) and invocation(strip_options(args[1:], value_flags))
    if name in ('go', 'cargo', 'dotnet', 'deno', 'swift', 'mix', 'flutter', 'dart'):
        return args[:1] == ['test']
    if name in ('make', 'just', 'task', 'rake', 'gradle', 'gradlew', 'mvn'):
        return any(re.match(r'^(?:test[\w:.-]*|check|verify)$', arg) or re.search(r'(?:^|[:_.-])tests?(?:[:_.-]|$)', arg)
                   for arg in args if not arg.startswith('-'))
    if re.fullmatch(r'python[\d.]*', name):
        if '-m' in args:
            index = args.index('-m')
            return (index + 1 < len(args) and args[index + 1] in ('pytest', 'unittest')
                    and not any(arg in ('--help', '-h', '--version') for arg in args[index + 2:]))
        if '-c' in args:
            index = args.index('-c')
            try:
                tree = ast.parse(args[index + 1])
            except (IndexError, SyntaxError):
                return False
            return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                       and isinstance(node.func.value, ast.Name) and node.func.value.id in ('pytest', 'unittest')
                       and node.func.attr == 'main' for node in ast.walk(tree))
    if name in ('node', 'nodejs') and any(arg == '--test' or arg.startswith('--test=') for arg in args):
        return not any(arg in ('--help', '-h', '--version') for arg in args)
    if name in ('node', 'nodejs') and any(arg in ('-e', '--eval', '-p', '--print') for arg in args):
        # Opaque dynamic execution is outside this recognizer; mere strings such
        # as readFileSync('vitest.config.ts') must not be treated as execution.
        return False
    if re.fullmatch(r'python[\d.]*|node|nodejs|sh|bash|zsh', name):
        script = next((arg for arg in args if not arg.startswith('-')), '')
        return bool(script) and test_script(script)
    return (words[0].startswith(('./', '/')) and test_script(words[0]))


def is_test_command(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars='();&|\n')
    lexer.whitespace = ' \t\r'
    lexer.whitespace_split = True
    segment = []
    for token in lexer:
        if token and all(character in '();&|\n' for character in token):
            if invocation(segment):
                return True
            segment = []
        else:
            segment.append(token)
    return invocation(segment)
