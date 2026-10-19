import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
FAKE_CLI = shutil.which('true')


class InstallTest(unittest.TestCase):
    def run_install(self, home, *arguments):
        result = subprocess.run(
            [sys.executable, str(ROOT / 'install.py'), '--home', str(home),
             '--agy', FAKE_CLI, *arguments],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'home'
            self.run_install(home, '--dry-run')
            self.assertFalse(home.exists())

    def test_merge_backup_and_reinstall_preserve_user_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / '.gemini/config'
            config.mkdir(parents=True)
            hooks = config / 'hooks.json'
            original = {'unrelated-user-hook': {'Stop': []}}
            hooks.write_text(json.dumps(original))
            rules = home / '.gemini/GEMINI.md'
            rules.write_text('Existing user rules\n')
            self.run_install(home)
            self.assertEqual(json.loads(hooks.read_text())['unrelated-user-hook'], original['unrelated-user-hook'])
            backups = sorted((home / '.gemini/control-kit-backups').iterdir())
            self.assertEqual(json.loads((backups[0] / 'config/hooks.json').read_text()), original)
            self.assertEqual((backups[0] / 'GEMINI.md').read_text(), 'Existing user rules\n')
            self.assertEqual((home / '.local/bin/agy').resolve(), Path(FAKE_CLI).resolve())
            self.run_install(home)
            text = rules.read_text()
            self.assertTrue(text.startswith('Existing user rules\n'))
            self.assertEqual(text.count('<!-- antigravity-control-kit:begin -->'), 1)
            self.assertFalse((config / 'hooks/state').exists())
            self.assertFalse((config / 'hooks/authorization').exists())


if __name__ == '__main__':
    unittest.main()
