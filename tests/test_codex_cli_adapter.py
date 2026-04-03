import io
import json
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.llm_adapter import CodexCliAdapter, CodexCliExecutionError
from app.domain.enums import AnalysisMode, ExecutorType, RuntimeStatus, TaskKeyword, TaskType
from app.domain.models import ContextBundle, InstructionBundle, Task


class CodexCliAdapterTests(unittest.TestCase):
    def build_context(self) -> ContextBundle:
        task = Task(
            task_id='task-20260315-abc123',
            run_id='run-001',
            idempotency_key='task-20260315-abc123:run-001',
            task_keyword=TaskKeyword.WAITING,
            runtime_status=RuntimeStatus.RUNNING,
            priority=0,
            retry_count=0,
            max_retries=2,
            locked_at='2026-03-15T12:00:00+08:00',
            lock_owner='runner-1',
            created_at='2026-03-15T10:00:00+08:00',
            updated_at='2026-03-15T12:00:00+08:00',
            block_uuid='block-uuid-001',
            page_id='2026_03_15',
            raw_block_text='- WAITING 分析 [[文章1]]',
            properties={'execution_mode': 'codex'},
            page_links=['文章1'],
        )
        return ContextBundle(task=task, pages={'文章1': '內容'})

    def build_instruction(self) -> InstructionBundle:
        return InstructionBundle(
            task_type=TaskType.REASONING_ANALYSIS,
            analysis_mode=AnalysisMode.REASONING_ANALYSIS,
            executor_type=ExecutorType.CODEX,
            model='gpt-5.4',
            expected_output_type='markdown',
        )

    def build_payload(self) -> dict[str, object]:
        return {
            'result_status': 'SUCCESS',
            'answer_type': 'DIRECT_ANSWER',
            'summary': '一句結論',
            'answer_paragraphs': ['第一段', '第二段'],
            'uncertainty': [
                {
                    'type': 'scope',
                    'impact': 'low',
                    'description': '題目沒有明確限定範圍。',
                }
            ],
            'artifact_content': None,
            'artifact_type': 'MARKDOWN',
            'target_file': None,
            'links_to_append': [],
            'writeback_actions': ['write_answer_page'],
            'confidence': 0.8,
            'assumptions': ['test'],
            'audit_log': {'tools_used': ['codex'], 'notes': None},
        }
    def test_complete_structured_builds_command_and_reads_output(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex', working_dir='D:/PY_REPO/ClawMind')
        expected_payload = self.build_payload()

        class FakeProcess:
            def __init__(self, command):
                self.command = command
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = 0
                output_path = Path(self.command[self.command.index('--output-last-message') + 1])
                output_path.write_text(json.dumps(expected_payload, ensure_ascii=False), encoding='utf-8')

            def poll(self):
                return self.returncode

            def terminate(self):
                raise AssertionError('terminate should not be called when process already exited')

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                raise AssertionError('kill should not be called when process already exited')

        with patch('app.adapters.llm_adapter.subprocess.Popen', side_effect=lambda *args, **kwargs: FakeProcess(args[0])) as mock_popen:
            result = adapter.complete_structured(self.build_context(), self.build_instruction())

        command = mock_popen.call_args.args[0]
        self.assertIn('exec', command)
        self.assertIn('--output-schema', command)
        self.assertIn('--output-last-message', command)
        self.assertIn('--model', command)
        self.assertIn('gpt-5.4', command)
        self.assertEqual(command[-1], '-')
        self.assertEqual(result['summary'], expected_payload['summary'])
        self.assertEqual(result['audit_log']['tools_used'], ['codex'])
        self.assertEqual(result['audit_log']['adapter_completion']['completion_status'], 'completed_by_output')
        self.assertEqual(result['audit_log']['adapter_cleanup']['cleanup_status'], 'process_exited_after_completion')

    def test_complete_structured_returns_success_when_output_ready_before_exit(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex', working_dir='D:/PY_REPO/ClawMind')
        expected_payload = self.build_payload()
        state = {'terminated': False, 'killed': False}

        class FakeProcess:
            def __init__(self, command):
                self.command = command
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = None
                output_path = Path(self.command[self.command.index('--output-last-message') + 1])
                output_path.write_text(json.dumps(expected_payload, ensure_ascii=False), encoding='utf-8')

            def poll(self):
                return self.returncode

            def terminate(self):
                state['terminated'] = True
                self.returncode = 0

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                state['killed'] = True
                self.returncode = -9

        with patch('app.adapters.llm_adapter.subprocess.Popen', side_effect=lambda *args, **kwargs: FakeProcess(args[0])):
            result = adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertTrue(state['terminated'])
        self.assertFalse(state['killed'])
        self.assertEqual(result['audit_log']['adapter_cleanup']['cleanup_status'], 'terminated_after_completion')

    def test_ready_payload_requires_answer_type_uncertainty_and_no_data_missing(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex')
        payload = self.build_payload()
        self.assertTrue(adapter._is_ready_payload(payload))

        missing_answer_type = dict(payload)
        del missing_answer_type['answer_type']
        self.assertFalse(adapter._is_ready_payload(missing_answer_type))

        machine_tag_payload = dict(payload)
        machine_tag_payload['summary'] = '[Data missing] 不可接受'
        self.assertFalse(adapter._is_ready_payload(machine_tag_payload))

        low_confidence = dict(payload)
        low_confidence['confidence'] = 0.4
        low_confidence['answer_type'] = 'BEST_EFFORT'
        self.assertFalse(adapter._is_ready_payload(low_confidence))

    def test_complete_structured_raises_when_codex_fails(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex')

        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO(b'boom')
                self.returncode = 1

            def poll(self):
                return self.returncode

            def terminate(self):
                raise AssertionError('terminate should not be called for exited process')

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                raise AssertionError('kill should not be called for exited process')

        with patch('app.adapters.llm_adapter.subprocess.Popen', return_value=FakeProcess()):
            with self.assertRaisesRegex(CodexCliExecutionError, 'Codex CLI execution failed') as ctx:
                adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertEqual(ctx.exception.diagnostic_payload['model'], 'gpt-5.4')
        self.assertIn('stderr_excerpt', ctx.exception.diagnostic_payload)
        self.assertEqual(ctx.exception.diagnostic_payload['cleanup_info']['cleanup_status'], 'process_exited_before_completion')

    def test_complete_structured_raises_when_codex_times_out_without_output(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex', command_timeout_seconds=0.05)
        state = {'terminated': False, 'killed': False}

        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                state['terminated'] = True
                self.returncode = 1

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 1
                return self.returncode

            def kill(self):
                state['killed'] = True
                self.returncode = -9

        with patch('app.adapters.llm_adapter.subprocess.Popen', return_value=FakeProcess()):
            with self.assertRaisesRegex(CodexCliExecutionError, 'timed out after 0.05 seconds') as ctx:
                adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertTrue(state['terminated'])
        self.assertFalse(state['killed'])
        self.assertTrue(ctx.exception.diagnostic_payload['timed_out'])
        self.assertEqual(ctx.exception.diagnostic_payload['cleanup_info']['cleanup_status'], 'timed_out_before_completion')
        self.assertGreater(ctx.exception.diagnostic_payload['prompt_chars'], 0)

    def test_complete_structured_does_not_salvage_partial_json(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex', command_timeout_seconds=0.05)

        class FakeProcess:
            def __init__(self, command):
                self.command = command
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = None
                output_path = Path(self.command[self.command.index('--output-last-message') + 1])
                output_path.write_text('{"result_status":', encoding='utf-8')

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 1

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = 1
                return self.returncode

            def kill(self):
                self.returncode = -9

        with patch('app.adapters.llm_adapter.subprocess.Popen', side_effect=lambda *args, **kwargs: FakeProcess(args[0])):
            with self.assertRaisesRegex(CodexCliExecutionError, 'timed out after 0.05 seconds'):
                adapter.complete_structured(self.build_context(), self.build_instruction())

    def test_complete_structured_salvages_async_output_before_process_exit(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex', working_dir='D:/PY_REPO/ClawMind', command_timeout_seconds=0.5)
        expected_payload = self.build_payload()
        state = {'terminated': False, 'killed': False}

        class FakeProcess:
            def __init__(self, command):
                self.command = command
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b'partial stdout')
                self.stderr = io.BytesIO(b'partial stderr')
                self.returncode = None
                self._writer = threading.Thread(target=self._write_output, daemon=True)
                self._writer.start()

            def _write_output(self) -> None:
                time.sleep(0.1)
                output_path = Path(self.command[self.command.index('--output-last-message') + 1])
                output_path.write_text(json.dumps(expected_payload, ensure_ascii=False), encoding='utf-8')

            def poll(self):
                return self.returncode

            def terminate(self):
                state['terminated'] = True
                self.returncode = 0

            def wait(self, timeout=None):
                self._writer.join(timeout=1)
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                state['killed'] = True
                self.returncode = -9

        with patch('app.adapters.llm_adapter.subprocess.Popen', side_effect=lambda *args, **kwargs: FakeProcess(args[0])):
            result = adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertTrue(state['terminated'])
        self.assertFalse(state['killed'])
        self.assertEqual(result['summary'], expected_payload['summary'])
        self.assertEqual(result['audit_log']['adapter_completion']['completion_source'], 'output_file')
        self.assertEqual(result['audit_log']['adapter_cleanup']['cleanup_status'], 'terminated_after_completion')

    def test_complete_structured_waits_for_parseable_json_before_salvage(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex', working_dir='D:/PY_REPO/ClawMind', command_timeout_seconds=0.5)
        expected_payload = self.build_payload()
        state = {'terminated': False, 'killed': False}

        class FakeProcess:
            def __init__(self, command):
                self.command = command
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO(b'tokens used\n321\n')
                self.returncode = None
                self._writer = threading.Thread(target=self._write_output, daemon=True)
                self._writer.start()

            def _write_output(self) -> None:
                output_path = Path(self.command[self.command.index('--output-last-message') + 1])
                output_path.write_text('{"result_status":', encoding='utf-8')
                time.sleep(0.1)
                output_path.write_text(json.dumps(expected_payload, ensure_ascii=False), encoding='utf-8')

            def poll(self):
                return self.returncode

            def terminate(self):
                state['terminated'] = True
                self.returncode = 0

            def wait(self, timeout=None):
                self._writer.join(timeout=1)
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                state['killed'] = True
                self.returncode = -9

        with patch('app.adapters.llm_adapter.subprocess.Popen', side_effect=lambda *args, **kwargs: FakeProcess(args[0])):
            result = adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertTrue(state['terminated'])
        self.assertFalse(state['killed'])
        self.assertEqual(result['answer_paragraphs'], expected_payload['answer_paragraphs'])
        self.assertEqual(result['audit_log']['adapter_cleanup']['cleanup_status'], 'terminated_after_completion')

    def test_complete_structured_records_forced_cleanup_after_completion(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex', working_dir='D:/PY_REPO/ClawMind')
        expected_payload = self.build_payload()
        state = {'terminated': False, 'killed': False}

        class FakeProcess:
            def __init__(self, command):
                self.command = command
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO(b'tokens used\n123\n')
                self.returncode = None
                output_path = Path(self.command[self.command.index('--output-last-message') + 1])
                output_path.write_text(json.dumps(expected_payload, ensure_ascii=False), encoding='utf-8')

            def poll(self):
                return self.returncode

            def terminate(self):
                state['terminated'] = True

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd='codex', timeout=timeout or 5)

            def kill(self):
                state['killed'] = True
                self.returncode = -9

        with patch('app.adapters.llm_adapter.subprocess.Popen', side_effect=lambda *args, **kwargs: FakeProcess(args[0])):
            result = adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertTrue(state['terminated'])
        self.assertTrue(state['killed'])
        self.assertEqual(result['audit_log']['adapter_cleanup']['cleanup_status'], 'cleanup_forced_after_completion')
        self.assertEqual(result['audit_log']['adapter_cleanup']['returncode'], -9)

    def test_complete_structured_terminates_child_on_keyboard_interrupt(self) -> None:
        adapter = CodexCliAdapter(codex_cli_path='codex')
        state = {'terminated': False, 'killed': False, 'waited': False}

        class FakeProcess:
            def __init__(self):
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()
                self.returncode = 130

            def poll(self):
                return self.returncode

            def terminate(self):
                state['terminated'] = True

            def wait(self, timeout=None):
                state['waited'] = True
                return self.returncode

            def kill(self):
                state['killed'] = True

        with patch('app.adapters.llm_adapter.subprocess.Popen', return_value=FakeProcess()):
            with patch.object(adapter, '_write_prompt', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertTrue(state['terminated'])
        self.assertTrue(state['waited'])
        self.assertFalse(state['killed'])


if __name__ == '__main__':
    unittest.main()







