#!/usr/bin/env python3
"""Bounded, read-only PR publication readiness; Git waivers are not merge grants."""
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import guard_platform as platform
import subprocess
import time
from urllib.parse import urlsplit


def repository(value):
    value = value.removesuffix('.git').rstrip('/')
    if '://' not in value:
        match = re.fullmatch(r'(?:[^@/]+@)?([^:/]+):([^/]+/[^/]+)', value)
        if match:
            return (match[1].lower(), match[2].lower())
        return None
    url = urlsplit(value)
    pieces = url.path.strip('/').split('/')
    if not url.hostname or len(pieces) < 2:
        return None
    return (url.hostname.lower(), '/'.join(pieces[:2]).lower())


def check(command, args, data, state):
    """Return None to allow or a factual denial; never mutate state or remote data."""
    try:
        words = platform.split(command)
    except ValueError:
        return 'Malformed command: merge readiness cannot determine the requested PR action.'
    # ponytail: shell heuristics cover visible gh commands, not opaque aliases/scripts.
    candidate = re.search(r'\bgh\b[\s\S]*\bpr\b[\s\S]*\b(?:merge|ready)\b', command + ' ' + ' '.join(words))
    if not candidate:
        return None
    if (len(words) < 4 or platform.basename(words[0]) != 'gh' or words[1:3] not in (['pr', 'merge'], ['pr', 'ready'])
            or any(c in command for c in ('\n', ';', '&', '|', '<', '>', '`', '$'))):
        return 'Use standalone gh pr merge/ready with an explicit PR number or URL and repository Cwd; wrappers, expansion and compound commands cannot be verified.'
    operation = words[2]
    selector = None
    repo_option = None
    match_head = None
    index = 3
    while index < len(words):
        word = words[index]
        if word in ('-R', '--repo', '--match-head-commit') and index + 1 < len(words):
            value = words[index + 1]
            if word == '--match-head-commit':
                match_head = value
            else:
                repo_option = value
            index += 2
        elif operation == 'merge' and word in ('--squash', '--merge', '--rebase', '--delete-branch', '-d'):
            index += 1
        elif not word.startswith('-') and selector is None:
            selector = word
            index += 1
        else:
            return 'Unsupported merge/ready option. Use an explicit PR, normal merge strategy, optional --delete-branch/--match-head-commit and --repo; automatic/admin overrides are not verified.'
    if not selector or not (selector.isdecimal() or re.fullmatch(r'https://[^/]+/[^/]+/[^/]+/pull/\d+/?', selector)):
        return 'Specify the exact PR number or full PR URL; branch/implicit PR selection is not verified.'
    cwd = args.get('Cwd') or args.get('cwd') or args.get('working_directory') or next(iter(data.get('workspacePaths', [])), os.getcwd())
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GH_PROMPT_DISABLED='1')
    try:
        def git(*items):
            return subprocess.check_output(['git', '-C', str(cwd), *items], env=env,
                stderr=subprocess.DEVNULL, timeout=1, text=True).strip()
        root, head, branch = git('rev-parse', '--show-toplevel', 'HEAD', '--abbrev-ref', 'HEAD').splitlines()
        if branch == 'HEAD':
            return 'Merge/ready requires a named local branch, not detached HEAD.'
        if git('status', '--porcelain', '--untracked-files=no'):
            return 'Tracked staged/working changes remain; commit and review the exact final head before merge/ready.'
        origin = repository(git('remote', 'get-url', 'origin'))
        if not origin:
            return 'Cannot identify the local origin repository; exact PR repository binding is unverified.'
        fields = 'headRefOid,headRefName,url,number,statusCheckRollup,mergeable,state'
        call = ['gh', 'pr', 'view', selector, '--json', fields]
        if repo_option:
            call += ['--repo', repo_option]
        pr = json.loads(subprocess.check_output(call, cwd=root, env=env,
            stderr=subprocess.DEVNULL, timeout=3, text=True))
        url = pr.get('url', '')
        expected_number = selector.rstrip('/').split('/')[-1]
        if repository(url) != origin or str(pr.get('number')) != expected_number:
            return 'The selected PR does not match the local origin repository and exact PR number.'
        if selector.startswith('https://') and selector.rstrip('/') != url.rstrip('/'):
            return 'PR URL returned by GitHub differs from the selected exact PR URL.'
        if pr.get('headRefOid') != head or pr.get('headRefName') != branch:
            return 'PR head SHA/branch differs from local HEAD/branch; review and CI must cover the exact published head.'
        if match_head and match_head != head:
            return '--match-head-commit does not equal the verified local/PR head SHA.'
        if operation == 'merge' and match_head != head:
            return 'Use --match-head-commit ' + head + ' so a concurrent PR update cannot merge an unreviewed head.'
        if pr.get('state') != 'OPEN':
            return 'The PR is not OPEN; merge/ready readiness is not established.'
        checks = pr.get('statusCheckRollup')
        if not isinstance(checks, list) or not checks:
            return 'Exact-head CI evidence is missing: no checks were returned. Native approval cannot turn missing CI into PASS.'
        def succeeded(item):
            return (isinstance(item, dict) and
                ((item.get('status') == 'COMPLETED' and item.get('conclusion') == 'SUCCESS')
                 or (not item.get('status') and item.get('state') == 'SUCCESS')))
        if not all(succeeded(item) for item in checks):
            required_call = ['gh', 'pr', 'checks', url, '--required', '--json', 'name,bucket,state']
            required = json.loads(subprocess.check_output(required_call, cwd=root, env=env,
                stderr=subprocess.DEVNULL, timeout=1, text=True))
            if not isinstance(required, list) or not required or not all(
                    isinstance(item, dict) and item.get('bucket') == 'pass' for item in required):
                return 'Required CI is failing, pending, skipped or unverified. Optional checks retain their actual status; native approval cannot waive required CI.'
        if operation == 'merge' and pr.get('mergeable') != 'MERGEABLE':
            return 'PR mergeability is conflicting or unknown; wait for a MERGEABLE exact-head result.'
        conversation = data.get('conversationId')
        if not isinstance(conversation, str) or not conversation:
            return 'Missing conversation identity: local merge review/approval cannot be verified.'
        key = hashlib.sha256(conversation.encode()).hexdigest()
        receipt = Path.home() / '.gemini/config/hooks/readiness' / (key + '.json')
        try:
            evidence = json.loads(receipt.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            evidence = {}
        report = evidence.get('report', {})
        valid = (evidence.get('kind') == 'comprehensive' and evidence.get('status') == 'reviewed'
            and evidence.get('repo') == root and evidence.get('branch') == branch
            and evidence.get('reviewed_commit') == head
            and isinstance(evidence.get('consequence_review_version'), int)
            and evidence['consequence_review_version'] >= 3
            and isinstance(report, dict) and report.get('decision') == 'PASS'
            and report.get('test_integrity') == 'LEGITIMATE'
            and report.get('limitations') == []
            and isinstance(report.get('findings'), list)
            and all(isinstance(f, dict) for f in report['findings'])
            and not any(f.get('severity') in ('P1', 'P2') for f in report.get('findings', [])))
        flows = report.get('consequence_checks', []) if isinstance(report, dict) else []
        valid = valid and isinstance(flows, list) and len(flows) == 2 and {
            f.get('category') for f in flows if isinstance(f, dict)
        } == {'data-lifecycle', 'async-order'} and all(
            isinstance(f, dict) and f.get('status') in ('VERIFIED', 'NOT_APPLICABLE')
            and all(isinstance(f.get(field), str) and f[field].strip()
                    for field in ('invariant', 'scenario', 'evidence')) for f in flows)
        flow = report.get('flow_verification', {}) if isinstance(report, dict) else {}
        alignment = report.get('production_test_alignment', {}) if isinstance(report, dict) else {}
        described = isinstance(flow, dict) and all(
            isinstance(flow.get(field), str) and flow[field].strip()
            for field in ('entrypoint', 'path', 'outcome', 'evidence'))
        scenarios = flow.get('scenarios') if isinstance(flow, dict) else None
        described = described and isinstance(scenarios, list) and bool(scenarios) and all(
            isinstance(item, str) and item.strip() for item in scenarios)
        executed = isinstance(flow, dict) and flow.get('status') == 'VERIFIED' and isinstance(
            flow.get('execution_evidence'), str) and bool(flow['execution_evidence'].strip())
        exempt = (isinstance(flow, dict) and flow.get('status') == 'NOT_APPLICABLE'
            and isinstance(alignment, dict) and alignment.get('status') == 'NOT_APPLICABLE'
            and all(isinstance(alignment.get(field), str) and alignment[field].strip()
                    for field in ('rationale', 'evidence')))
        valid = valid and (evidence.get('production_changed') is False or (described and (executed or exempt)))
        if valid:
            consequences = runpy.run_path(str(Path(__file__).with_name('consequence-review.py')))
            task = consequences['request_context'](conversation)
            digest = consequences['requirements_digest'](task)
            valid = task.get('complete') is True and evidence.get('requirements_digest') == digest
        if valid:
            return None
        priority = runpy.run_path(str(Path(__file__).with_name('user-priority.py')))
        request = priority['latest_human_request'](data)
        if (isinstance(request, dict) and request.get('source') == 'interactive'
                and request.get('scope') == {'repo': root, 'branch': branch}
                and 'merge' in request.get('operations', []) and request.get('head') == head
                and time.time() < request.get('expires', 0) <= time.time() + 1800):
            return None
        detail = report.get('decision', 'MISSING') if isinstance(report, dict) else 'INVALID'
        return ('Local comprehensive merge review is ' + str(detail)
            + ' or does not cover this exact repository/branch/head with consequence review v3. '
            + 'Complete the review after the final commit, or obtain one explained native merge approval scoped to this repository/branch; commit/push permission does not authorize merge. '
            + 'That approval waives local review/evidence gaps only, never exact-head identity, CI or mergeability.')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return 'Merge readiness lookup failed or exceeded its bounded deadline; PR identity, CI and review are UNVERIFIED. Diagnose the lookup instead of claiming PASS.'
