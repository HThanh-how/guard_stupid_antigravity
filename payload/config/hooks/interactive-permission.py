#!/usr/bin/env python3
"""Recognize completed native Git approval questions, never arbitrary tool output."""
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata


def normalize(text):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', text.lower().replace('đ', 'd'))
                            if not unicodedata.combining(c)).split())


def questions(value):
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, list) else []


def header(question):
    fields = {}
    for expected, line in zip(('Repo', 'Branch', 'Actions', 'Reason', 'Unverified'), question.splitlines()[:5]):
        key, separator, value = line.partition(':')
        if key != expected or not separator or not value.strip():
            return None
        fields[key] = value.strip()
    if len(fields) != 5:
        return None
    actions = set(re.split(r'[,\s]+', fields['Actions']))
    if not actions or not actions <= {'commit', 'push', 'merge'}:
        return None
    plan = {'repo': fields['Repo'], 'branch': fields['Branch'], 'actions': sorted(actions)}
    if 'merge' in actions:
        lines = question.splitlines()
        if len(lines) < 6 or not re.fullmatch(r'Head: [a-f0-9]{40}', lines[5]):
            return None
        plan['head'] = lines[5][6:]
    return plan


def native_reply(previous, result, conversation):
    if not isinstance(previous, dict) or not isinstance(result, dict):
        return None
    calls = previous.get('tool_calls', [])
    if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
        return None
    if (previous.get('source') != 'MODEL' or previous.get('type') != 'PLANNER_RESPONSE'
            or len(calls) != 1 or calls[0].get('name') != 'ask_question'
            or result.get('source') != 'MODEL' or result.get('type') != 'GENERIC' or result.get('status') != 'DONE'
            or result.get('truncated_fields') or previous.get('truncated_fields') and 'tool_calls' in previous['truncated_fields']):
        return None
    try:
        offered = questions(calls[0].get('args', {}).get('questions'))
        if len(offered) != 1 or offered[0].get('is_multi_select') is not False:
            return None
        question = offered[0]['question']
        plan = header(question)
        content = result.get('content', '')
        match = re.fullmatch(r'Created At: [^\n]+\nCompleted At: ([^\n]+)\nA1: ([^\n]+)\s*', content)
        if not plan or not match:
            return None
        answer = match.group(2).strip()
        label = normalize(re.sub(r'^\(Recommended\)\s*', '', answer, flags=re.I))
        label = re.sub(r' (?:luon )?(?:khong (?:can )?doi duyet|bo qua duyet)$', '', label)
        choices = {'cho phep': plan['actions'], 'dong y': plan['actions'], 'allow': plan['actions'],
                   'approve': plan['actions'], 'yes': plan['actions'],
                   'cho phep commit': ['commit'], 'cho phep push': ['push'],
                   'cho phep commit va push': ['commit', 'push'], 'cho phep merge': ['merge']}
        choices['cho phep ' + ' va '.join(plan['actions'])] = plan['actions']
        # A negative choice is a newer human decision too; it revokes the offer.
        operations = choices.get(label, [])
        if not set(operations) <= set(plan['actions']):
            operations = []
        completed = datetime.fromisoformat(match.group(1).replace('Z', '+00:00'))
        if completed.tzinfo is None:
            return None
        completed = completed.astimezone(timezone.utc)
        timestamp = completed.isoformat(timespec='microseconds').replace('+00:00', 'Z')
        return (timestamp, answer, {'source': 'interactive', 'scope': {'repo': plan['repo'], 'branch': plan['branch']},
                'operations': operations, 'head': plan.get('head'), 'expires': completed.timestamp() + 1800,
                'id': hashlib.sha256((conversation + ':' + timestamp + ':' + question + ':' + answer).encode()).hexdigest()})
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def proposed_actions(offered):
    if len(offered) != 1 or not isinstance(offered[0], dict):
        return []
    original = offered[0].get('question', '')
    explicit = re.search(r'^Actions: ([^\n]+)$', original, re.M)
    if explicit:
        actions = set(re.split(r'[,\s]+', explicit[1].strip()))
        return sorted(actions) if actions and actions <= {'commit', 'push', 'merge'} else []
    labels = ' '.join(option for option in offered[0].get('options', []) if isinstance(option, str) and
                      re.match(r'^(?:\(recommended\)\s*)?(?:cho phep|allow|approve)\b', normalize(option)))
    source = labels or original
    return sorted(set(re.findall(r'\b(?:commit|push|merge)\b', normalize(source))))


def prepare(value, scope, reason, unverified):
    offered = questions(value)
    if len(offered) != 1 or not isinstance(offered[0], dict):
        return None
    original = offered[0].get('question', '')
    combined = normalize(original + ' ' + ' '.join(offered[0].get('options', [])))
    actions = proposed_actions(offered)
    if not actions or not (header(original) or re.search(r'\b(?:cho phep|dong y|allow|approve|authorize|approval|cap quyen|duyet)\b', combined)):
        return None
    clean = lambda text: ' '.join(str(text).split())
    question = '\n'.join(['Repo: ' + scope['repo'], 'Branch: ' + scope['branch'], 'Actions: ' + ', '.join(actions),
        'Reason: ' + clean(reason), 'Unverified: ' + clean(unverified)])
    if 'merge' in actions:
        question += '\nHead: ' + scope['head']
    question += '\n\n' + original + '\n\nĐồng ý sẽ cho phép các thao tác được chọn mà không chờ review; không xác nhận các kiểm tra chưa chạy là PASS.'
    label = 'Cho phép ' + ' và '.join(actions)
    options = [label]
    if actions == ['commit', 'push']:
        options.append('Cho phép commit')
    options.append('Chưa cho phép')
    return [{'question': question, 'options': options, 'is_multi_select': False}]
