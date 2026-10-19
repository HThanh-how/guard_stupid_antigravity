#!/usr/bin/env python3
"""Recognize explicit human Git review waivers; never accept an agent's claim."""
import json
import hashlib
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import runpy
import shlex
import subprocess
import time
import unicodedata


def latest_human_request(data):
    conv = data.get('conversationId', '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', conv):
        return None
    candidates = [Path.home() / '.gemini' / product / 'brain' / conv /
                  '.system_generated/logs/transcript.jsonl' for product in ('antigravity', 'antigravity-cli')]
    requests = []
    native_reply = runpy.run_path(str(Path(__file__).with_name('interactive-permission.py')))['native_reply']
    for path in candidates:
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 2_000_000))
            lines = stream.read().decode(errors='replace').splitlines()
        previous = {}
        for sequence, line in enumerate(lines):
            try:
                item = json.loads(line)
            except ValueError:
                previous = {}
                continue
            if not isinstance(item, dict):
                previous = {}
                continue
            native = native_reply(previous, item, conv)
            previous = item
            if native:
                requests.append((*native, str(path), sequence))
            if item.get('source') != 'USER_EXPLICIT' or item.get('type') != 'USER_INPUT':
                continue
            content = item.get('content', '')
            if not isinstance(content, str) or 'content' in (item.get('truncated_fields') or []) or re.search(r'<truncated\b|\[TRUNCATED\]', content, re.I):
                content = None
            elif '<USER_REQUEST>' in content or '</USER_REQUEST>' in content:
                match = re.fullmatch(r'\s*<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>\s*(?:<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>[\s\S]*?</\2>\s*)*', content)
                content = match.group(1) if match else None
            stamp = str(item.get('created_at') or '')
            try:
                stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            except ValueError:
                pass
            requests.append((stamp, content, None, str(path), sequence))
    for timestamp, request, native, origin, sequence in sorted(requests, key=lambda row: (row[0], row[4]), reverse=True):
        tied = [row for row in requests if row[0] == timestamp]
        if len({row[3] for row in tied}) > 1 and any(row[:3] != tied[0][:3] for row in tied):
            return None
        if not isinstance(request, str):
            return None
        text = ''.join(char for char in unicodedata.normalize('NFKD', request.lower().replace('đ', 'd')) if not unicodedata.combining(char))
        text = ' '.join(text.split())
        if native:
            return {**native, 'text': request, 'normalized': text}
        if text in ('continue', 'tiep tuc', 'lam tiep', 'ok', 'yes'):
            continue
        # Quoted examples, pasted artifacts and agent/tool messages are not authorization.
        if request.strip().startswith(('\"', "'", '“', '‘')) or len(text) > 1000 or any(marker in request for marker in ('```', '<external_', '<document', '> ')) or re.search(r'\b(?:vi du|example|khong cho phep|do not authorize)\b', text):
            return None
        if '?' in request or '？' in request:
            return None
        if re.search(r"\b(?:(?:do not|don't|dont|never) (?:allow|authorize|skip)|khong(?: duoc)? cho(?: phep| ban| no)|khong(?: duoc)? bo qua|dung bo qua)\b", text):
            return None
        return {'text': request, 'normalized': text,
                'id': hashlib.sha256((conv + ':' + timestamp + ':' + request).encode()).hexdigest()}
    return None


def permission_scope(data):
    args = data.get('toolCall', {}).get('args', {})
    cwd = args.get('Cwd') or args.get('cwd') or args.get('working_directory') or next(iter(data.get('workspacePaths', [])), os.getcwd())
    words = shlex.split(args.get('CommandLine') or args.get('command') or '')
    if len(words) > 3 and words[0] == 'cd' and words[2] == '&&':
        cwd = str((Path(cwd) / words[1]).resolve())
        words = words[3:]
    if words and Path(words[0]).name == 'git':
        index = 1
        while index < len(words) and words[index].startswith('-'):
            flag = words[index]
            if flag == '-C' and index + 1 < len(words):
                cwd = str((Path(cwd) / words[index + 1]).resolve())
                index += 2
            elif flag.startswith('-C') and len(flag) > 2:
                cwd = str((Path(cwd) / flag[2:]).resolve())
                index += 1
            else:
                index += 2 if flag in ('-c', '--git-dir', '--work-tree', '--namespace', '--config-env') else 1
    def git(*words):
        return subprocess.check_output(['git', '-C', str(cwd), *words], stderr=subprocess.DEVNULL, timeout=2).decode().strip()
    return {'repo': git('rev-parse', '--show-toplevel'), 'branch': git('symbolic-ref', '--quiet', '--short', 'HEAD')}


def permission_cache(data, request):
    scope = permission_scope(data)
    key = hashlib.sha256(json.dumps([request['id'], scope], sort_keys=True).encode()).hexdigest()
    return Path.home() / '.gemini/config/hooks/authorization' / (key + '.json'), scope


def cached_authorization(data, request):
    try:
        path, scope = permission_cache(data, request)
        result = json.loads(path.read_text())
        if result.get('request_id') != request['id'] or result.get('scope') != scope or result.get('expires', 0) <= time.time():
            return set()
        report = result.get('report', {})
        if report.get('decision') != 'ALLOW':
            return set()
        operations = set(report.get('operations', []))
        evidence = report.get('evidence', {})
        if not operations or not operations <= {'commit', 'push'} or any(
                not isinstance(evidence.get(op), str) or not evidence[op].strip() or evidence[op] not in request['text'] for op in operations):
            return set()
        for operation in ('commit', 'push'):
            if re.search(r"\b(?:do not|don't|never|not|khong(?: duoc)?|dung|chua|ko|k)\s+" + operation + r'\b', request['normalized']):
                operations.discard(operation)
        return operations | ({'add'} if 'commit' in operations else set())
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()


def git_review_waiver(data):
    request = latest_human_request(data)
    if request:
        if request.get('source') == 'interactive':
            try:
                if request['expires'] <= time.time() or request['scope'] != permission_scope(data):
                    return set()
                operations = set(request['operations'])
                return operations | ({'add'} if 'commit' in operations else set())
            except (OSError, ValueError, subprocess.SubprocessError):
                return set()
        text = request['normalized']
        if re.search(r"\b(?:(?:do not|don't|dont|never) (?:allow|authorize|skip)|khong(?: duoc)? cho(?: phep| ban| no)|khong(?: duoc)? bo qua|dung bo qua)\b", text):
            return set()
        permission = re.match(r'^(?:(?:toi|minh|tao|i|please|ok|vang)\s+)?(?:cho phep|cho ban|cho no|allow|authorize|you may)\b', text)
        waiver = re.search(r'\b(?:(?:khong|ko|k)(?: can)? (?:doi|cho)(?: review| duyet)?|bo qua (?:review|duyet)|skip (?:the )?(?:review|approval)|without (?:waiting for )?(?:review|approval))\b', text)
        if not permission or not waiver:
            return cached_authorization(data, request)
        permission_clause = re.split(r'\b(?:but|however|nhung|tuy nhien)\b', text[:waiver.start()], maxsplit=1)[0]
        operations = set()
        if re.search(r'\bcommit\b', permission_clause):
            operations.update(('add', 'commit'))
        if re.search(r'\bpush\b', permission_clause):
            operations.add('push')
        for operation in ('commit', 'push'):
            if re.search(r"\b(?:do not|don't|never|not|khong(?: duoc)?|dung|chua|ko|k)\s+" + operation + r'\b', text):
                operations.discard(operation)
                if operation == 'commit':
                    operations.discard('add')
        return operations
    return set()
