#!/usr/bin/env python3
"""Local branch checks only; never commits, pushes or contacts a remote."""
import fnmatch
import os
from pathlib import Path
import shlex
import subprocess

PROTECTED = ('main', 'master', 'develop', 'dev', 'production', 'prod', 'staging', 'release', 'release/*', 'releases/*')


def check(command, arguments, workspaces):
    words = shlex.split(command)
    cwd = Path(arguments.get('Cwd') or arguments.get('cwd') or arguments.get('working_directory') or (workspaces[0] if workspaces else os.getcwd()))
    if len(words) > 3 and words[0] == 'cd' and words[2] == '&&':
        cwd = (cwd / words[1]).resolve()
        words = words[3:]
    if not words or Path(words[0]).name != 'git':
        if any(Path(word).name == 'git' for word in words) and any(word in ('add', 'commit', 'push') for word in words):
            return 'Use a standalone git commit/push command with explicit Cwd; wrappers and command-local Git environment cannot be branch-verified.'
        return None
    index = 1
    unsupported = False
    while index < len(words) and words[index].startswith('-'):
        flag = words[index]
        if flag == '-C' and index + 1 < len(words):
            cwd = (cwd / words[index + 1]).resolve()
            index += 2
        elif flag.startswith('-C') and len(flag) > 2:
            cwd = (cwd / flag[2:]).resolve()
            index += 1
        elif flag in ('--no-pager', '--literal-pathspecs'):
            index += 1
        else:
            unsupported = True
            index += 2 if flag in ('-c', '--git-dir', '--work-tree', '--config-env', '--namespace') else 1
    if index >= len(words) or words[index] not in ('add', 'commit', 'push'):
        return None
    operation = words[index]
    rest = words[index + 1:]
    if unsupported:
        return 'Unsupported Git global options for commit/push. Use standalone git -C <repo> without alternate config/index/work-tree.'
    if any(word in ('&&', '||', ';', '|') for word in rest):
        return 'Run commit/push as a standalone command so its branch and target can be verified.'

    def git(*args):
        return subprocess.check_output(['git', '-C', str(cwd), *args], input=b'',
            env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, stderr=subprocess.DEVNULL, timeout=2).decode().strip()

    try:
        branch = git('symbolic-ref', '--quiet', '--short', 'HEAD')
    except subprocess.CalledProcessError:
        return 'Commit/push requires a named working branch; detached HEAD or repository lookup failed.'
    protected = set(PROTECTED)
    for line in git('for-each-ref', '--format=%(refname) %(symref)', 'refs/remotes').splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].endswith('/HEAD'):
            protected.add('/'.join(fields[1].split('/')[3:]))

    def important(name):
        return any(fnmatch.fnmatchcase(name.lower(), pattern.lower()) for pattern in protected)

    if important(branch):
        return 'Protected branch ' + branch + ': create/switch to a focused working branch before commit/push. Do not move existing user changes or bypass this guard.'
    if operation in ('add', 'commit'):
        if operation == 'commit' and ('--no-verify' in rest or any(word.startswith('-') and not word.startswith('--') and 'n' in word[1:] for word in rest)):
            return 'Do not bypass repository verification hooks for an automatic commit.'
        return None

    operands = []
    for word in rest:
        if word.startswith('-'):
            if word not in ('-u', '--set-upstream', '--dry-run', '--porcelain', '--verbose', '-v', '--no-follow-tags'):
                return 'Automatic push permits only a normal explicit branch push, without force, delete, tags, mirror or all-branch options.'
        else:
            operands.append(word)
    remotes = git('remote').splitlines()
    example_remote = 'origin' if 'origin' in remotes else (remotes[0] if remotes else 'origin')
    example = 'git push --no-follow-tags -u ' + shlex.quote(example_remote) + ' ' + shlex.quote('HEAD:refs/heads/' + branch)
    if len(operands) != 2 or operands[0] not in remotes:
        return 'Specify one configured remote and one explicit working-branch target. Use: ' + example
    for config_key in ('push.followTags', 'remote.' + operands[0] + '.mirror'):
        try:
            enabled = git('config', '--bool', '--get', config_key) == 'true'
        except subprocess.CalledProcessError:
            enabled = False
        if enabled and (config_key.endswith('.mirror') or '--no-follow-tags' not in rest):
            return 'Push config may publish extra refs/tags. Disable mirror mode; use --no-follow-tags for an automatic branch-only push. Example: ' + example
    target = operands[1]
    source, destination = target.split(':', 1) if ':' in target else (target, target)
    destination = destination.removeprefix('refs/heads/')
    if source not in ('HEAD', branch, 'refs/heads/' + branch) or destination != branch or important(destination):
        return 'Push may publish only the current non-protected branch to the same branch name. Use: ' + example
    return None
