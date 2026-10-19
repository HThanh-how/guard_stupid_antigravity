#!/usr/bin/env python3
"""Supply authentic task history and reject incomplete consequence-review reports."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def request_context(conversation_id):
    context = {'requests': [], 'complete': True, 'limitations': []}
    if not isinstance(conversation_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', conversation_id):
        context.update(complete=False, limitations=['Missing valid originating conversation.'])
        return context
    records = []
    size = 0
    for product in ('antigravity', 'antigravity-cli'):
        directory = Path.home() / '.gemini' / product / 'brain' / conversation_id / '.system_generated/logs'
        full = directory / 'transcript_full.jsonl'
        path = full if full.exists() else directory / 'transcript.jsonl'
        if not path.is_file():
            if path == full:
                context['complete'] = False
                context['limitations'].append('Canonical full transcript is not a readable regular file.')
            continue
        human_records = 0
        with path.open() as stream:
            for number, line in enumerate(stream):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    context['complete'] = False
                    context['limitations'].append('Malformed transcript record; original requests may be missing.')
                    continue
                if not isinstance(item, dict):
                    context['complete'] = False
                    context['limitations'].append('Transcript contains a non-object record.')
                    continue
                if item.get('source') != 'USER_EXPLICIT' or item.get('type') != 'USER_INPUT':
                    continue
                human_records += 1
                text = item.get('content')
                if not isinstance(text, str):
                    context['complete'] = False
                    context['limitations'].append('Human request has no readable content.')
                    continue
                match = re.search(r'<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>', text)
                text = match.group(1) if match else text
                size += len(text)
                if size > 2000000:
                    context.update(complete=False)
                    context['limitations'].append('Human history exceeds the 2,000,000 character safety limit; original requests remain incomplete. No silent truncation approval.')
                    context['requests'] = [record[3] for record in sorted(records, key=lambda record: record[:3])]
                    return context
                key = hashlib.sha256(f'{product}:{number}:{text}'.encode()).hexdigest()[:16]
                complete = 'content' not in (item.get('truncated_fields') or []) and not re.search(r'<truncated\b|\[TRUNCATED\]', text, re.I)
                records.append((item.get('created_at', ''), product, number,
                                {'id': key, 'text': text, 'complete': bool(complete)}))
        if path == full and not human_records:
            context['complete'] = False
            context['limitations'].append('Canonical full transcript contains no authentic human requests.')
    context['requests'] = [record[3] for record in sorted(records, key=lambda record: record[:3])]
    if not context['requests']:
        context['complete'] = False
        context['limitations'].append('No authentic human task found; code review alone cannot establish task completion.')
    return context


def requirements_digest(context):
    # Continuations preserve the existing contract; they add no new acceptance clause.
    continuations = {'continue', 'ok', 'yes', 'tiếp tục', 'tiep tuc', 'làm tiếp', 'lam tiep'}
    active = [request for request in context['requests'] if ' '.join(request['text'].lower().split()) not in continuations]
    value = {**context, 'requests': active}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prompt_context(context, directory):
    """Index oversized history without summarizing or dropping its original text."""
    if len(json.dumps(context, ensure_ascii=False)) <= 120000:
        return context
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    indexed = {**context, 'requests': []}
    for request in context['requests']:
        content = request['text'].encode('utf-8')
        name = hashlib.sha256((request['id'] + '\0').encode() + content).hexdigest() + '.txt'
        path = directory / name
        descriptor, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), 0o444)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        indexed['requests'].append({**{key: value for key, value in request.items() if key != 'text'},
                                    'source': str(path), 'bytes': len(content)})
    if len(json.dumps(indexed, ensure_ascii=False)) > 120000:
        raise ValueError('Human request source index exceeds bounded prompt size; no requests were silently omitted')
    return indexed


def assess(report, context):
    """Completeness checks, not a claim that model-supplied evidence is true."""
    failures = (list(context['limitations']) or ['Original human task history is incomplete.']) if not context['complete'] else []
    requests = context['requests']
    order = {item['id']: index for index, item in enumerate(requests)}
    complete = {item['id']: item.get('complete', True) for item in requests}
    covered = set()
    coverage = report.get('requirement_coverage', [])
    if not isinstance(coverage, list):
        coverage = []
    for item in coverage:
        if not isinstance(item, dict):
            failures.append('Invalid requirement coverage entry.')
            continue
        key, status = item.get('request_id'), item.get('status')
        if key not in order or not all(isinstance(item.get(field), str) and item[field].strip() for field in ('requirement', 'evidence')):
            failures.append('Coverage requires a known human request and concrete requirement/evidence.')
            continue
        covered.add(key)
        if status == 'SUPERSEDED':
            later = item.get('superseded_by')
            if later not in order or order[later] <= order[key]:
                failures.append('Superseded requirements must cite a later real human request.')
        elif status == 'PENDING_PUBLICATION':
            sequence = ['add', 'commit', 'push', 'remote-ci']
            operation, pending = context.get('gated_operation'), item.get('pending_operation')
            if (operation not in sequence[:3] or pending not in sequence
                    or sequence.index(pending) < sequence.index(operation) or not complete[key]):
                failures.append('Pending publication must be a current/future Git step with verified readiness, not completed execution.')
        elif status != 'VERIFIED' or not complete[key]:
            failures.append('A requirement is unverified or its original prompt is truncated: ' + key)
    if set(order) - covered or not order:
        failures.append('Original human requests are missing from the requirement coverage report.')
    checks = report.get('consequence_checks', [])
    if not isinstance(checks, list):
        checks = []
    categories = []
    for item in checks:
        if not isinstance(item, dict):
            failures.append('Invalid consequence check.')
            continue
        categories.append(item.get('category'))
        if item.get('status') not in ('VERIFIED', 'NOT_APPLICABLE') or not all(
                isinstance(item.get(field), str) and item[field].strip() for field in ('invariant', 'scenario', 'evidence')):
            failures.append('Consequence review needs justified applicability or inspected fault-injection/order evidence.')
    if sorted(str(category) for category in categories) != ['async-order', 'data-lifecycle']:
        failures.append('Review both asset/data ownership and asynchronous result ordering exactly once.')
    if context.get('production_changed'):
        alignment = report.get('production_test_alignment', {})
        if not isinstance(alignment, dict) or alignment.get('status') not in ('JUSTIFIED', 'NOT_APPLICABLE') or not all(
                isinstance(alignment.get(field), str) and alignment[field].strip() for field in ('rationale', 'evidence')):
            failures.append('Production changes need an independent product-contract rationale; failing fixture expectations alone are not justification.')
    if context.get('production_changed'):
        flow = report.get('flow_verification', {})
        if not isinstance(flow, dict):
            flow = {}
        described = all(isinstance(flow.get(field), str) and flow[field].strip()
                        for field in ('entrypoint', 'path', 'outcome', 'evidence'))
        scenarios = flow.get('scenarios')
        described = described and isinstance(scenarios, list) and bool(scenarios) and all(isinstance(item, str) and item.strip() for item in scenarios)
        verified = flow.get('status') == 'VERIFIED' and isinstance(flow.get('execution_evidence'), str) and bool(flow['execution_evidence'].strip())
        alignment = report.get('production_test_alignment', {})
        exempt = flow.get('status') == 'NOT_APPLICABLE' and isinstance(alignment, dict) and alignment.get('status') == 'NOT_APPLICABLE'
        if not described or not (verified or exempt):
            failures.append('Changed production behavior needs the traced entrypoint-to-outcome flow and relevant executed scenarios, not grep hits or unrelated green CI.')
    if failures:
        if report.get('decision') != 'BLOCKED':
            report['decision'] = 'UNVERIFIED'
        report.setdefault('limitations', []).extend(dict.fromkeys(failures))
    return report
