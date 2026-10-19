#!/usr/bin/env python3
"""Standing permission and CI efficiency: disposable local repos, no remote calls."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


class WorkflowPolicyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.env = dict(os.environ, HOME=str(self.home))
        self.repo = self.home / 'repo with spaces'
        self.repo.mkdir()
        self.git('init', '-q')
        (self.repo / 'code.ts').write_text('export const value = 1\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('branch', '-M', 'feature/smoke')
        self.git('remote', 'add', 'origin', str(self.home / 'remote.git'))
        self.git('update-ref', 'refs/remotes/origin/trunk', self.base)
        self.git('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/trunk')
        self.hooks = self.home / '.gemini/config/hooks'
        self.hooks.mkdir(parents=True)
        (self.hooks / 'workflow-policy.json').write_text((HERE / 'workflow-policy.json').read_text())
        self.conversation = 'workflow-fixture'

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.repo), *args], env=self.env,
            stderr=subprocess.DEVNULL, text=True, timeout=3).strip()

    def pre(self, command='', name='run_command', args=None):
        payload = {'conversationId': self.conversation, 'workspacePaths': [str(self.repo)],
                   'toolCall': {'name': name, 'args': args or {'CommandLine': command, 'Cwd': str(self.repo)}}}
        result = subprocess.run([sys.executable, str(HERE / 'review-gate.py'), 'pre'],
            input=json.dumps(payload), text=True, capture_output=True, timeout=5, env=self.env)
        return json.loads(result.stdout)

    def test_normal_branch_git_always_allowed_without_review_receipt_or_approval(self):
        for command in ('git add .', 'git commit -m change',
                        'git push --no-follow-tags -u origin HEAD:refs/heads/feature/smoke'):
            for _ in range(2):
                result = self.pre(command)
                self.assertEqual(result['decision'], 'allow', result)
                self.assertNotIn('authorize-git.py', result.get('reason', ''))
        self.assertFalse((self.hooks / 'authorization').exists())
        self.assertFalse((self.hooks / 'readiness').exists())

    def test_protected_and_remote_default_branch_still_denied(self):
        for branch in ('main', 'trunk'):
            self.git('checkout', '-B', branch)
            for command in ('git add .', 'git commit -m change', f'git push origin HEAD:refs/heads/{branch}'):
                self.assertEqual(self.pre(command)['decision'], 'deny', (branch, command))

    def test_force_other_branch_shell_wrappers_and_no_verify_not_granted(self):
        for command in ('git push --force origin feature/smoke', 'git push origin HEAD:refs/heads/main',
                        'git commit --no-verify -m change', 'git add --force .',
                        'python mutate.py && git add .', 'sh -c "git add ."',
                        'git --git-dir=.git add .'):
            self.assertEqual(self.pre(command)['decision'], 'deny', command)

    def test_test_review_remains_required_across_an_allowed_commit(self):
        self.assertEqual(self.pre('pytest')['decision'], 'deny')
        self.assertEqual(self.pre('git commit -m checkpoint')['decision'], 'allow')
        self.assertEqual(self.pre('pytest')['decision'], 'deny')
        self.assertEqual(self.pre('gh pr merge 22 --repo owner/repo --merge')['decision'], 'deny')

    def test_git_question_is_not_shown_again_on_a_normal_branch(self):
        question = [{'is_multi_select': False, 'question':
            f'Repo: {self.repo}\nBranch: feature/smoke\nActions: commit, push\nReason: review pending\nUnverified: CI pending\nCho phép commit và push?',
            'options': ['Cho phép commit và push', 'Chưa cho phép']}]
        result = self.pre(name='ask_question', args={'questions': question})
        self.assertEqual(result['decision'], 'deny')
        self.assertIn('already permits', result['reason'])

    def test_all_foreground_ci_query_forms_share_one_head_budget_but_logs_stay_readable(self):
        for command in ('gh run view 101 --repo owner/repo',
                        'gh api /repos/owner/repo/actions/runs/101/jobs',
                        'gh pr checks 22 --repo owner/repo'):
            self.assertEqual(self.pre(command)['decision'], 'allow', command)
        self.assertEqual(self.pre('gh api /repos/owner/repo/actions/runs/101 --jq .status')['decision'], 'deny')
        self.assertEqual(self.pre('gh run view 101 --repo owner/repo --log-failed')['decision'], 'allow')
        self.assertEqual(self.pre('gh api /repos/owner/repo/actions/jobs/202/logs')['decision'], 'allow')
        self.assertEqual(self.pre('gh run watch 101 --repo owner/repo')['decision'], 'deny')
        self.assertEqual(self.pre("printf '%s' 'gh run view 101'")['decision'], 'allow')

    def test_final_review_covers_committed_changes_even_after_push_tracking_catches_up(self):
        (self.repo / 'code.ts').write_text('export const value = 2\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'feature')
        self.git('update-ref', 'refs/remotes/origin/feature/smoke', 'HEAD')
        with patch.dict(os.environ, self.env):
            module = runpy.run_path(str(HERE / 'independent-agent.py'))
            key = module['prepare_final_review'](self.conversation, str(self.repo), 'origin/trunk')
            state = json.loads((self.hooks / 'state' / (key + '.json')).read_text())
            context = json.loads(module['snapshot_context']([str(self.repo)], review_base=state['review_base']))
        preparation = subprocess.run([sys.executable, str(HERE / 'review-gate.py'), 'dispatch'],
            input=json.dumps({'conversationId': self.conversation}), text=True, capture_output=True,
            timeout=5, env=self.env)
        self.assertEqual(preparation.returncode, 0, preparation.stderr)
        self.assertEqual(json.loads(preparation.stdout), {})
        queued = json.loads((self.hooks / 'state' / (key + '.json')).read_text())
        self.assertEqual(queued['status'], 'queued')
        self.assertIn('READ-ONLY', queued['prompt'])
        self.assertEqual(queued['review_head'], self.git('rev-parse', 'HEAD'))
        queued.update(status='blocked', attempts=2)
        (self.hooks / 'state' / (key + '.json')).write_text(json.dumps(queued))
        with patch.dict(os.environ, self.env):
            with self.assertRaises(ValueError):
                module['prepare_final_review'](self.conversation, str(self.repo), 'origin/trunk')
        self.assertEqual(state['kind'], 'comprehensive')
        self.assertEqual(state['review_base'], self.base)
        self.assertIn('export const value = 2', context[0]['outgoing_diff'])
        self.assertTrue(context[0]['production_changed'])
        self.assertFalse(context[0]['outgoing_unverified'])


if __name__ == '__main__':
    unittest.main()
