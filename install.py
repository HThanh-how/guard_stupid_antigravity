#!/usr/bin/env python3
"""Install the exported policy with backups; never copy runtime authorization."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
BEGIN = '<!-- antigravity-control-kit:begin -->'
END = '<!-- antigravity-control-kit:end -->'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home(), help='Destination user home')
    parser.add_argument('--agy', type=Path, help='Existing official AGY CLI executable')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('This snapshot requires macOS/Linux (Bash, fcntl and POSIX signals).')
    if sys.version_info < (3, 9):
        parser.error('Python 3.9 or newer is required.')
    home = args.home.expanduser().resolve()
    manifest = json.loads((ROOT / 'SHA256SUMS.json').read_text())
    for name, expected in manifest.items():
        path = ROOT / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            parser.error(f'Export integrity mismatch: {name}')
    cli = home / '.local/bin/agy'
    chosen = args.agy.expanduser().resolve() if args.agy else cli
    if not chosen.is_file() or not os.access(chosen, os.X_OK):
        parser.error('Install/login to the official AGY CLI first. Then use --agy /absolute/path/to/agy.')
    if cli.exists() and cli.resolve() != chosen.resolve():
        parser.error(f'{cli} already points to another executable; refusing to replace it.')
    if cli.is_symlink() and not cli.exists():
        parser.error(f'{cli} is a broken symlink; resolve it before installation.')

    target = home / '.gemini'
    writes = {}
    for src in sorted((ROOT / 'payload/config').rglob('*')):
        if src.is_file() and src.name != 'hooks.json':
            writes[target / src.relative_to(ROOT / 'payload')] = src.read_bytes()
    hooks_path = target / 'config/hooks.json'
    old_hooks = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
    if not isinstance(old_hooks, dict):
        parser.error('Existing hooks.json must be a JSON object; no files changed.')
    old_hooks.update(json.loads((ROOT / 'payload/config/hooks.json').read_text()))
    writes[hooks_path] = (json.dumps(old_hooks, indent=2, ensure_ascii=False) + '\n').encode()
    rules = target / 'GEMINI.md'
    existing = rules.read_text() if rules.exists() else ''
    if (BEGIN in existing) != (END in existing) or existing.count(BEGIN) > 1 or existing.count(END) > 1:
        parser.error('Existing managed policy block is malformed; no files changed.')
    managed = BEGIN + '\n' + (ROOT / 'payload/GEMINI.md').read_text().rstrip() + '\n' + END
    if BEGIN in existing:
        start, stop = existing.index(BEGIN), existing.index(END) + len(END)
        existing = existing[:start] + managed + existing[stop:]
    else:
        existing = existing.rstrip() + ('\n\n' if existing.strip() else '') + managed + '\n'
    writes[rules] = existing.encode()
    for path in writes:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            parser.error(f'Refusing non-regular destination: {path}')
    print(f'Destination: {target}; {len(writes)} managed files')
    print('Existing unrelated hook groups/rules are preserved; matching kit files are backed up/replaced.')
    if args.dry_run:
        print('DRY RUN: no files, links, permissions or backups changed.')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = target / 'control-kit-backups' / stamp
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    receipt = {'created': [], 'replaced': [], 'cli_link_created': False}
    # Back up everything before the first config mutation.
    for path in writes:
        name = str(path.relative_to(target))
        receipt['replaced' if path.exists() else 'created'].append(name)
        if path.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
    (backup / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    for path, content in writes.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if path.suffix == '.sh':
            path.chmod(0o755)
    if not cli.exists():
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.symlink_to(chosen)
        receipt['cli_link_created'] = True
        (backup / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(f'Installed. Backup/receipt: {backup}')
    print('Restart Antigravity. Authenticate AGY/GitHub separately on this machine.')


if __name__ == '__main__':
    main()
