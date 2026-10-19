#!/usr/bin/env python3
"""Offline behavioral checks: disposable transcripts, no real AI or user tasks."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = runpy.run_path(str(Path(__file__).with_name('conversation-attention.py')))


class AttentionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'HOME': str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path = self.home / '.gemini/antigravity/brain/demo/.system_generated/logs/transcript_full.jsonl'
        self.path.parent.mkdir(parents=True)
        agent = self.home / '.gemini/config/agents/conversation-attention/agent.md'
        agent.parent.mkdir(parents=True)
        agent.write_text('fixture')
        self.counter = 0
        self.data = {'conversationId': 'demo', 'toolCall': {'name': 'manage_task', 'args': {'Action': 'cancel'}}}
        self.human('Implement the requested change.')
        self.assertIsNone(MODULE['gate'](self.data))

    def record(self, source, kind, text, **extra):
        self.counter += 1
        item = dict(step_index=self.counter, created_at=str(self.counter), source=source,
                    type=kind, status='DONE', content=text, **extra)
        with self.path.open('a') as stream:
            stream.write(json.dumps(item) + '\n')

    def human(self, text, **extra):
        self.record('USER_EXPLICIT', 'USER_INPUT', '<USER_REQUEST>' + text + '</USER_REQUEST>', **extra)

    def answer(self, text):
        self.record('MODEL', 'PLANNER_RESPONSE', text)

    def pending(self, text='Tại sao lại chạy agent đó?'):
        self.human(text)
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')

    def report(self, **overrides):
        value = dict(intent='QUESTION', response='ANSWERED', user_evidence='Tại sao',
                     answer_evidence='Agent đang kiểm tra ảnh giao diện.', stop_requested=False,
                     stop_evidence='', waiting=False, wait_report='NOT_APPLICABLE', next_step='CONTINUE', reason='Substantive reply.')
        return dict(value, **overrides)

    def assess(self, report):
        with patch('runpy.run_path', return_value={'agent_result': lambda *a, **k: report}):
            return MODULE['assess']('demo')

    def test_delivered_question_blocks_next_action_without_cancelling_any_task(self):
        self.pending()
        self.assertNotIn('cancelled', MODULE['gate'](self.data))

    def test_model_quoted_question_is_not_a_new_human_message(self):
        self.record('MODEL', 'USER_INPUT', 'Stop the agent')
        self.assertIsNone(MODULE['gate'](self.data))

    def test_semantically_requested_read_is_one_exact_inspection_never_cancellation(self):
        self.pending()
        read = dict(self.data, toolCall={'name': 'manage_task', 'args': {'Action': 'status', 'TaskId': 'owned-task'}})
        MODULE['gate'](read)
        self.assess(self.report(response='UNANSWERED', answer_evidence='', next_step='ONE_READ_FOR_ANSWER'))
        self.assertIsNone(MODULE['gate'](read))
        self.assertEqual(MODULE['gate'](read)['decision'], 'deny')
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')

    def test_answered_question_allows_work_and_new_question_invalidates_it(self):
        self.pending()
        self.answer('Agent đang kiểm tra ảnh giao diện.')
        self.assertEqual(self.assess(self.report()), 0)
        self.assertIsNone(MODULE['gate'](self.data))
        self.human('Còn bước nào chưa làm?')
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')

    def test_partial_reply_and_missing_wait_estimate_are_not_silently_completed(self):
        self.pending()
        self.answer('Agent đang kiểm tra ảnh giao diện.')
        self.assertEqual(self.assess(self.report(response='PARTIAL', waiting=True, wait_report='MISSING')), 0)
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')
        self.answer('Chưa ước lượng tin cậy được; đã chờ 20 giây, hạn còn 100 giây, kiểm tra tiếp ở kết quả worker.')
        self.assertEqual(self.assess(self.report(waiting=True, wait_report='ADEQUATE')), 0)
        self.assertIsNone(MODULE['gate'](self.data))

    def test_claimed_but_invisible_answer_cannot_clear_checkpoint(self):
        self.pending()
        self.assertEqual(self.assess(self.report()), 1)
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')
        self.assertIsNone(MODULE['gate'](self.data))

    def test_timeout_is_not_pass_and_does_not_create_retry_or_stop_loop(self):
        self.pending()
        with patch('runpy.run_path', return_value={'agent_result': lambda *a, **k: (_ for _ in ()).throw(TimeoutError())}) as runner:
            self.assertEqual(MODULE['assess']('demo'), 1)
            self.assertEqual(MODULE['assess']('demo'), 1)
            self.assertEqual(runner.call_count, 1)
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')
        self.assertIsNone(MODULE['gate'](self.data))

    def test_identical_context_cannot_launch_second_classifier(self):
        self.pending()
        report = self.report(response='UNANSWERED', answer_evidence='')
        with patch('runpy.run_path', return_value={'agent_result': lambda *a, **k: report}) as runner:
            self.assertEqual(MODULE['assess']('demo'), 0)
            self.assertEqual(MODULE['assess']('demo'), 1)
            self.assertEqual(runner.call_count, 1)

    def test_new_human_arriving_during_assessment_discards_old_result(self):
        self.pending()
        self.answer('Agent đang kiểm tra ảnh giao diện.')
        def arrive(*args, **kwargs):
            self.human('Khoan, trả lời câu mới này trước.')
            return self.report()
        with patch('runpy.run_path', return_value={'agent_result': arrive}):
            self.assertEqual(MODULE['assess']('demo'), 1)
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')
        state = json.loads(MODULE['state_path']('demo').read_text())
        self.assertEqual(len(state['pending_questions']), 1)

    def test_truncated_request_is_not_sent_to_classifier(self):
        self.pending()
        self.human('Incomplete request', truncated_fields=['content'])
        MODULE['gate'](self.data)
        with patch('runpy.run_path') as runner:
            self.assertEqual(MODULE['assess']('demo'), 1)
            runner.assert_not_called()

    def test_helper_exemption_rejects_extra_shell_commands_and_finish_can_answer(self):
        self.pending()
        raw = 'python3 ' + str(Path(__file__).with_name('conversation-attention.py')) + ' assess demo && kill 123'
        data = dict(self.data, toolCall={'name': 'run_command', 'args': {'CommandLine': raw}})
        self.assertEqual(MODULE['gate'](data)['decision'], 'deny')
        self.assertIsNone(MODULE['gate'](dict(self.data, toolCall={'name': 'finish', 'args': {}})))

    def test_deadline_expires_without_restarting_a_classifier(self):
        self.pending()
        path = MODULE['state_path']('demo')
        state = json.loads(path.read_text())
        state['deadline'] = 0
        MODULE['save'](path, state)
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')
        self.assertIsNone(MODULE['gate'](self.data))

    def test_classifier_booleans_are_real_booleans_not_truthy_text(self):
        validate = runpy.run_path(str(Path(__file__).with_name('independent-agent.py')))['validate_report']
        validate(self.report(), MODULE['SCHEMA'])
        with self.assertRaises(ValueError):
            validate(self.report(stop_requested='false'), MODULE['SCHEMA'])

    def test_real_pretool_entrypoint_gives_question_priority(self):
        self.pending()
        process = subprocess.run(
            [sys.executable, str(Path(__file__).with_name('review-gate.py')), 'pre'],
            input=json.dumps(self.data), capture_output=True, text=True, timeout=3)
        result = json.loads(process.stdout)
        self.assertEqual(result['decision'], 'deny')
        self.assertIn('conversation-attention.py', result['reason'])

    def test_short_continuation_does_not_erase_an_unanswered_question(self):
        self.pending()
        self.assess(self.report(response='UNANSWERED', answer_evidence=''))
        self.human('Tiếp đi')
        MODULE['gate'](self.data)
        path = MODULE['state_path']('demo')
        pending = json.loads(path.read_text())['pending_questions']
        self.assertEqual(len(pending), 1)
        self.assess(self.report(intent='CONTINUATION', response='NOT_NEEDED', user_evidence='Tiếp', answer_evidence=''))
        self.assertEqual(MODULE['gate'](self.data)['decision'], 'deny')
        self.answer('Agent đang kiểm tra ảnh giao diện.')
        report = self.report(intent='CONTINUATION', response='NOT_NEEDED', user_evidence='Tiếp',
            question_resolutions=[{'id': pending[0]['id'], 'status': 'ANSWERED', 'evidence': 'Agent đang kiểm tra ảnh giao diện.'}])
        self.assess(report)
        self.assertIsNone(MODULE['gate'](self.data))

    def test_semantic_agent_only_has_terminal_reporting_permission(self):
        for tool, expected in [('write_to_file', 'deny'), ('run_command', 'deny'), ('finish', 'allow')]:
            data = dict(self.data, toolCall={'name': tool, 'args': {}})
            process = subprocess.run(
                [sys.executable, str(Path(__file__).with_name('review-gate.py')), 'pre'],
                input=json.dumps(data), capture_output=True, text=True, timeout=3,
                env=dict(os.environ, AGY_GUARD_ROLE='intent-classifier'))
            self.assertEqual(json.loads(process.stdout)['decision'], expected)


if __name__ == '__main__':
    unittest.main()
