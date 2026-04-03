from pathlib import Path
import json
import shutil
import unittest
import uuid

from app.adapters.logseq_adapter import LogseqAdapter
from app.application.audit_service import AuditService
from app.application.classifier_service import ClassifierService
from app.application.recovery_service import RecoveryService
from app.application.context_builder import ContextBuilder
from app.application.runner_service import RunnerOutcome, RunnerService, TaskFailure
from app.application.writeback_service import WritebackService
from app.domain.enums import AnalysisMode, AnswerType, ArtifactType, ExecutorType, ResultStatus, TaskKeyword, TaskType
from app.domain.models import ExecutionResult, UncertaintyItem


class RunnerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / '_tmp_runner' / uuid.uuid4().hex
        (self.root / 'logseq' / 'journals').mkdir(parents=True, exist_ok=True)
        (self.root / 'logseq' / 'pages').mkdir(parents=True, exist_ok=True)
        (self.root / 'logseq' / 'pages' / '文章1.md').write_text('文章1內容', encoding='utf-8')
        (self.root / 'run_logs').mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        (self.root / 'logseq' / 'journals' / '2026_03_15.md').write_text(
            '\n'.join(
                [
                    '- DOING 分析 [[文章1]] 並整理結論',
                    '    priority:: 5',
                    '    load_memory:: false',
                    '    load_adr:: false',
                    '    load_linked_pages:: true',
                    '    debugging_mode:: false',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        adapter = LogseqAdapter(self.root / 'logseq')
        self.runner = RunnerService(
            logseq_adapter=adapter,
            classifier_service=ClassifierService(),
            context_builder=ContextBuilder(
                self.root / 'logseq',
                run_logs_dir=self.root / 'run_logs',
                runtime_artifacts_dir=self.root / 'runtime_artifacts',
            ),
            writeback_service=WritebackService(
                adapter,
                runtime_artifacts_dir=self.root / 'runtime_artifacts',
            ),
            audit_service=AuditService(self.root / 'run_logs'),
            lock_owner='runner-test',
        )

    def test_run_once_processes_doing_task_end_to_end_without_runtime_block_properties(self) -> None:
        captured = {}

        def fake_executor(context_bundle, instruction_bundle):
            captured['task_id'] = context_bundle.task.task_id
            captured['executor_type'] = instruction_bundle.executor_type
            captured['task_type'] = instruction_bundle.task_type
            captured['analysis_mode'] = instruction_bundle.analysis_mode
            captured['model'] = instruction_bundle.model
            return ExecutionResult(
                result_status=ResultStatus.SUCCESS,
                artifact_content='# Answer\n\n這是整合測試輸出。',
                artifact_type=ArtifactType.MARKDOWN,
                target_file=None,
                links_to_append=[],
                writeback_actions=['write_answer_page', 'append_journal_link'],
                unresolved_items=[],
                answer_type=AnswerType.BEST_EFFORT,
                summary='這是可直接使用的初步結論。',
                answer_paragraphs=['這是整合測試輸出。'],
                uncertainty=[
                    UncertaintyItem(type='scope', impact='low', description='題目沒有額外限制條件。')
                ],
                confidence=0.91,
                assumptions=['使用 markdown adapter MVP'],
                audit_log={'tools_used': []},
            )

        outcome = self.runner.run_once(fake_executor)

        self.assertIsNotNone(outcome)
        assert outcome is not None
        parsed_uuid = uuid.UUID(captured['task_id'])
        self.assertEqual(str(parsed_uuid), captured['task_id'])
        self.assertEqual(captured['executor_type'], ExecutorType.CODEX)
        self.assertEqual(captured['task_type'], TaskType.MARKDOWN_APPEND)
        self.assertEqual(captured['analysis_mode'], AnalysisMode.NORMAL)
        self.assertEqual(captured['model'], 'gpt-5.4-mini')
        self.assertEqual(outcome.final_keyword, TaskKeyword.TODO.value)
        self.assertEqual(outcome.result_status, ResultStatus.SUCCESS.value)
        self.assertTrue(outcome.answer_page is not None and outcome.answer_page.exists())
        self.assertEqual(outcome.answer_page.name, '20260315__' + outcome.answer_page.stem.split('__')[1] + '.md')
        self.assertTrue(outcome.idempotency_key.startswith('wb:'))
        runtime_artifact = self.root / 'runtime_artifacts' / captured['task_id'] / outcome.run_id / 'artifact.md'
        manifest_path = self.root / 'runtime_artifacts' / captured['task_id'] / outcome.run_id / 'execution_result.json'
        self.assertTrue(runtime_artifact.exists())
        self.assertTrue(manifest_path.exists())
        journal_content = (self.root / 'logseq' / 'journals' / '2026_03_15.md').read_text(encoding='utf-8')
        self.assertIn(f'id:: {captured["task_id"]}', journal_content)
        self.assertIn('- TODO 分析 [[文章1]] 並整理結論', journal_content)
        self.assertIn(f'[[{outcome.answer_page.stem}]]', journal_content)
        self.assertNotIn('run_id::', journal_content)
        self.assertNotIn('task_runner_status::', journal_content)
        self.assertNotIn('result::', journal_content)

        audit = json.loads(outcome.audit_log_path.read_text(encoding='utf-8'))
        self.assertEqual(audit['task_id'], captured['task_id'])
        self.assertEqual(audit['executor_type'], ExecutorType.CODEX.value)
        self.assertEqual(audit['analysis_mode'], AnalysisMode.NORMAL.value)
        self.assertEqual(audit['model'], 'gpt-5.4-mini')
        self.assertEqual(audit['writeback_status'], 'COMPLETED')
        self.assertEqual(audit['result_status'], ResultStatus.SUCCESS.value)
        claim_path = self.root / 'run_logs' / captured['task_id'] / 'active_claim.json'
        self.assertFalse(claim_path.exists())
        runtime_records = sorted((self.root / 'run_logs' / captured['task_id']).glob('*.json'))
        self.assertEqual(len(runtime_records), 1)
        audit_payload = json.loads(runtime_records[0].read_text(encoding='utf-8'))
        self.assertEqual(audit_payload['lock_owner'], 'runner-test')
        self.assertIsNotNone(audit_payload['locked_at'])
        self.assertEqual(audit_payload['run_id'], outcome.run_id)
        self.assertEqual(audit_payload['context_evidence']['linked_page_context']['requested_page_links'], ['文章1'])
        self.assertEqual(audit_payload['context_evidence']['linked_page_context']['selected_page_links'], ['文章1'])
        self.assertEqual(audit_payload['context_evidence']['linked_page_context']['resolution'][0]['page_name'], '文章1')
        self.assertTrue(audit_payload['context_evidence']['linked_page_context']['resolution'][0]['found_page'])
        self.assertTrue(audit_payload['context_evidence']['linked_page_context']['resolution'][0]['loaded_to_context'])
        self.assertEqual(audit_payload['execution_result']['answer_type'], 'BEST_EFFORT')
        self.assertEqual(audit_payload['execution_result']['summary'], '這是可直接使用的初步結論。')
        self.assertEqual(audit_payload['execution_result']['uncertainty'][0]['type'], 'scope')
        self.assertEqual(audit_payload['execution_result']['confidence'], 0.91)
        self.assertEqual(audit_payload['execution_result']['assumptions'], ['使用 markdown adapter MVP'])

    def test_run_once_repairs_waiting_task_without_id_before_dispatch(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 舊版遺留任務',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        captured = {}

        def fake_executor(context_bundle, instruction_bundle):
            captured['task_id'] = context_bundle.task.task_id
            return ExecutionResult(
                result_status=ResultStatus.SUCCESS,
                artifact_content='# Answer\n\nrepair waiting',
                artifact_type=ArtifactType.MARKDOWN,
                target_file=None,
                links_to_append=[],
                writeback_actions=['write_answer_page', 'append_journal_link'],
                unresolved_items=[],
                confidence=0.8,
                assumptions=[],
                audit_log={'tools_used': []},
            )

        outcome = self.runner.run_once(fake_executor)

        self.assertIsNotNone(outcome)
        assert outcome is not None
        parsed_uuid = uuid.UUID(captured['task_id'])
        self.assertEqual(str(parsed_uuid), captured['task_id'])
        content = journal.read_text(encoding='utf-8')
        self.assertIn(f'id:: {captured["task_id"]}', content)
        self.assertIn('- TODO 舊版遺留任務', content)
        self.assertIn(f'[[{outcome.answer_page.stem}]]', content)

    def test_failed_writeback_reuses_existing_artifact_without_rerunning_executor(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 已入佇列任務',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        original_write_answer_page = self.runner.logseq_adapter.write_answer_page
        state = {'executor_calls': 0, 'fail_once': True}

        def flaky_write_answer_page(record, content: str):
            if state['fail_once']:
                state['fail_once'] = False
                raise RuntimeError('simulated writeback failure')
            return original_write_answer_page(record, content)

        self.runner.logseq_adapter.write_answer_page = flaky_write_answer_page  # type: ignore[method-assign]

        def fake_executor(context_bundle, instruction_bundle):
            state['executor_calls'] += 1
            return ExecutionResult(
                result_status=ResultStatus.SUCCESS,
                artifact_content='# Answer\n\nrecover me',
                artifact_type=ArtifactType.MARKDOWN,
                target_file=None,
                links_to_append=[],
                writeback_actions=['write_answer_page', 'append_journal_link'],
                unresolved_items=[],
                confidence=0.85,
                assumptions=[],
                audit_log={'tools_used': ['codex']},
            )

        with self.assertRaisesRegex(RuntimeError, 'simulated writeback failure'):
            self.runner.run_once(fake_executor)

        runtime_logs = sorted((self.root / 'run_logs' / 'waiting-task-001').glob('*.json'))
        self.assertEqual(len(runtime_logs), 1)
        failed_payload = json.loads(runtime_logs[0].read_text(encoding='utf-8'))
        self.assertEqual(failed_payload['writeback_status'], 'FAILED')
        self.assertEqual(failed_payload['result_status'], ResultStatus.SUCCESS.value)
        self.assertTrue(failed_payload['runtime_artifact'].endswith('artifact.md'))
        self.assertEqual(state['executor_calls'], 1)
        self.assertIn('- WAITING 已入佇列任務', journal.read_text(encoding='utf-8'))

        outcome = self.runner.run_once(fake_executor)

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(state['executor_calls'], 1)
        self.assertEqual(outcome.run_id, failed_payload['run_id'])
        self.assertEqual(outcome.idempotency_key, failed_payload['idempotency_key'])
        self.assertTrue(outcome.answer_page is not None and outcome.answer_page.exists())
        final_content = journal.read_text(encoding='utf-8')
        self.assertIn(f'- TODO 已入佇列任務 [[{outcome.answer_page.stem}]]', final_content)
        recovered_payload = json.loads(runtime_logs[0].read_text(encoding='utf-8'))
        self.assertEqual(recovered_payload['writeback_status'], 'COMPLETED')
        self.assertTrue(recovered_payload['idempotent_replay'] is False)

    def test_idempotency_key_is_stable_across_reruns_for_same_task(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 已入佇列任務',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        def fake_executor(context_bundle, instruction_bundle):
            return ExecutionResult(
                result_status=ResultStatus.SUCCESS,
                artifact_content='# Answer\n\nwaiting first',
                artifact_type=ArtifactType.MARKDOWN,
                target_file=None,
                links_to_append=[],
                writeback_actions=['write_answer_page', 'append_journal_link'],
                unresolved_items=[],
                confidence=0.88,
                assumptions=[],
                audit_log={'tools_used': []},
            )

        first = self.runner.run_once(fake_executor)
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 已入佇列任務',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        second = self.runner.run_once(fake_executor)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(first.answer_page.name, second.answer_page.name)

    def test_duplicate_run_records_audit_evidence_without_duplicate_writeback(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 已入佇列任務',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        def fake_executor(context_bundle, instruction_bundle):
            return ExecutionResult(
                result_status=ResultStatus.SUCCESS,
                artifact_content='# Answer\n\nwaiting first',
                artifact_type=ArtifactType.MARKDOWN,
                target_file=None,
                links_to_append=[],
                writeback_actions=['write_answer_page', 'append_journal_link'],
                unresolved_items=[],
                confidence=0.88,
                assumptions=[],
                audit_log={'tools_used': []},
            )

        first = self.runner.run_once(fake_executor)
        self.assertIsNotNone(first)
        assert first is not None

        journal.write_text(
            '\n'.join(
                [
                    f'- WAITING 已入佇列任務 [[{first.answer_page.stem}]]',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        second = self.runner.run_once(fake_executor)

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(first.idempotency_key, second.idempotency_key)
        audit_payload = json.loads(second.audit_log_path.read_text(encoding='utf-8'))
        self.assertTrue(audit_payload['duplicate_run_detected'])
        self.assertTrue(audit_payload['idempotent_replay'])
        first_line = journal.read_text(encoding='utf-8').splitlines()[0]
        self.assertEqual(first_line.count(f'[[{second.answer_page.stem}]]'), 1)

    def test_run_once_skips_waiting_task_when_active_claim_exists(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 已入佇列任務',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        claim_dir = self.root / 'run_logs' / 'waiting-task-001'
        claim_dir.mkdir(parents=True, exist_ok=True)
        (claim_dir / 'active_claim.json').write_text('{}', encoding='utf-8')

        outcome = self.runner.run_once(lambda context, instruction: None)

        self.assertIsNone(outcome)

    def test_run_once_dispatches_existing_waiting_task_before_new_doing_task(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 已入佇列任務',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '- DOING 後進來的新任務',
                    '    id:: doing-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        captured = {}

        def fake_executor(context_bundle, instruction_bundle):
            captured['task_id'] = context_bundle.task.task_id
            captured['keyword'] = context_bundle.task.task_keyword.value
            return ExecutionResult(
                result_status=ResultStatus.SUCCESS,
                artifact_content='# Answer\n\nwaiting first',
                artifact_type=ArtifactType.MARKDOWN,
                target_file=None,
                links_to_append=[],
                writeback_actions=['write_answer_page', 'append_journal_link'],
                unresolved_items=[],
                confidence=0.88,
                assumptions=[],
                audit_log={'tools_used': []},
            )

        outcome = self.runner.run_once(fake_executor)

        self.assertIsNotNone(outcome)
        assert outcome is not None
        self.assertEqual(captured['task_id'], 'waiting-task-001')
        self.assertEqual(captured['keyword'], TaskKeyword.WAITING.value)
        content = journal.read_text(encoding='utf-8')
        self.assertIn(f'- TODO 已入佇列任務 [[{outcome.answer_page.stem}]]', content)
        self.assertIn('- DOING 後進來的新任務', content)

    def test_run_worker_processes_waiting_then_doing_tasks_serially(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 第一個任務',
                    '    id:: waiting-task-001',
                    '    execution_mode:: codex',
                    '- DOING 第二個任務',
                    '    id:: doing-task-001',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        seen_task_ids: list[str] = []

        def fake_executor(context_bundle, instruction_bundle):
            seen_task_ids.append(context_bundle.task.task_id)
            return ExecutionResult(
                result_status=ResultStatus.SUCCESS,
                artifact_content='# Answer\n\nworker',
                artifact_type=ArtifactType.MARKDOWN,
                target_file=None,
                links_to_append=[],
                writeback_actions=['write_answer_page', 'append_journal_link'],
                unresolved_items=[],
                confidence=0.80,
                assumptions=[],
                audit_log={'tools_used': []},
            )

        outcome = self.runner.run_worker(fake_executor)

        self.assertEqual(outcome.processed_count, 2)
        self.assertEqual(len(outcome.outcomes), 2)
        self.assertFalse(outcome.interrupted)
        self.assertEqual(outcome.stop_reason, 'queue_drained')
        self.assertEqual(seen_task_ids, ['waiting-task-001', 'doing-task-001'])
        content = journal.read_text(encoding='utf-8')
        self.assertEqual(content.count('- TODO '), 2)
        self.assertEqual(content.count('- DOING '), 0)
        self.assertEqual(content.count('- WAITING '), 0)

    def test_run_once_recovers_timed_out_waiting_claim_before_dispatch(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 超時中的任務',
                    '  id:: waiting-task-timeout-001',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        claim_dir = self.root / 'run_logs' / 'waiting-task-timeout-001'
        claim_dir.mkdir(parents=True, exist_ok=True)
        (claim_dir / 'active_claim.json').write_text('{}', encoding='utf-8')
        (claim_dir / 'run-001.json').write_text(
            json.dumps(
                {
                    'task_id': 'waiting-task-timeout-001',
                    'run_id': 'run-001',
                    'locked_at': '2026-03-15T12:00:00+08:00',
                    'retry_count': 0,
                    'max_retries': 2,
                    'runtime_status': 'RUNNING',
                    'writeback_status': 'PENDING',
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding='utf-8',
        )
        runner = RunnerService(
            logseq_adapter=self.runner.logseq_adapter,
            classifier_service=ClassifierService(),
            context_builder=ContextBuilder(
                self.root / 'logseq',
                run_logs_dir=self.root / 'run_logs',
                runtime_artifacts_dir=self.root / 'runtime_artifacts',
            ),
            writeback_service=WritebackService(
                self.runner.logseq_adapter,
                runtime_artifacts_dir=self.root / 'runtime_artifacts',
            ),
            audit_service=AuditService(self.root / 'run_logs'),
            recovery_service=RecoveryService(self.runner.logseq_adapter, run_logs_dir=self.root / 'run_logs'),
            lock_owner='runner-test',
        )

        outcome = runner.run_once(lambda context, instruction: None)

        self.assertIsNone(outcome)
        self.assertFalse((claim_dir / 'active_claim.json').exists())
        content = journal.read_text(encoding='utf-8')
        self.assertIn('- TODO 超時中的任務', content)

    def test_run_once_marks_execute_failure_and_returns_task_to_todo(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 會失敗的任務',
                    '    id:: waiting-task-execute-failure',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        with self.assertRaisesRegex(RuntimeError, 'execute boom'):
            self.runner.run_once(lambda context, instruction: (_ for _ in ()).throw(RuntimeError('execute boom')))

        content = journal.read_text(encoding='utf-8')
        self.assertIn('- TODO 會失敗的任務', content)
        self.assertIn('error_reason:: execute boom', content)
        self.assertIn('failed_at::', content)
        runtime_logs = sorted((self.root / 'run_logs' / 'waiting-task-execute-failure').glob('*.json'))
        self.assertEqual(len(runtime_logs), 1)
        payload = json.loads(runtime_logs[0].read_text(encoding='utf-8'))
        self.assertEqual(payload['runtime_status'], 'FAILED')
        self.assertEqual(payload['task_keyword'], 'TODO')
        self.assertEqual(payload['writeback_status'], 'SKIPPED')
        self.assertEqual(payload['failed_flow'], 'execute')
        self.assertEqual(payload['result_status'], 'FAILED')
        self.assertIsNone(payload['failure_context'])

    def test_run_once_persists_failure_context_from_executor_exception(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 會帶診斷資訊失敗的任務',
                    '    id:: waiting-task-execute-diagnostic',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        class DiagnosticError(RuntimeError):
            def __init__(self) -> None:
                super().__init__('Codex CLI execution timed out after 300 seconds')
                self.diagnostic_payload = {
                    'adapter': 'CodexCliAdapter',
                    'timed_out': True,
                    'model': 'gpt-5.4-mini',
                    'prompt_chars': 1234,
                    'stderr_excerpt': 'timeout',
                }

        with self.assertRaisesRegex(RuntimeError, 'timed out after 300 seconds'):
            self.runner.run_once(lambda context, instruction: (_ for _ in ()).throw(DiagnosticError()))

        runtime_logs = sorted((self.root / 'run_logs' / 'waiting-task-execute-diagnostic').glob('*.json'))
        self.assertEqual(len(runtime_logs), 1)
        payload = json.loads(runtime_logs[0].read_text(encoding='utf-8'))
        self.assertEqual(payload['failed_flow'], 'execute')
        self.assertEqual(payload['failure_context']['adapter'], 'CodexCliAdapter')
        self.assertTrue(payload['failure_context']['timed_out'])
        self.assertEqual(payload['failure_context']['model'], 'gpt-5.4-mini')

    def test_run_once_marks_statusback_failure_with_runtime_evidence(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING statusback 會失敗的任務',
                    '    id:: waiting-task-statusback-failure',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        original_write_log = self.runner.audit_service.write_log

        def broken_write_log(**kwargs):
            raise RuntimeError('statusback boom')

        self.runner.audit_service.write_log = broken_write_log  # type: ignore[method-assign]

        try:
            with self.assertRaisesRegex(RuntimeError, 'statusback boom'):
                self.runner.run_once(
                    lambda context, instruction: ExecutionResult(
                        result_status=ResultStatus.SUCCESS,
                        artifact_content='# Answer\n\nstatusback fail',
                        artifact_type=ArtifactType.MARKDOWN,
                        target_file=None,
                        links_to_append=[],
                        writeback_actions=['write_answer_page', 'append_journal_link'],
                        unresolved_items=[],
                        confidence=0.9,
                        assumptions=[],
                        audit_log={'tools_used': []},
                    )
                )
        finally:
            self.runner.audit_service.write_log = original_write_log  # type: ignore[method-assign]

        content = journal.read_text(encoding='utf-8')
        self.assertIn('- TODO statusback 會失敗的任務', content)
        self.assertIn('error_reason:: statusback boom', content)
        runtime_logs = sorted((self.root / 'run_logs' / 'waiting-task-statusback-failure').glob('*.json'))
        self.assertEqual(len(runtime_logs), 1)
        payload = json.loads(runtime_logs[0].read_text(encoding='utf-8'))
        self.assertEqual(payload['runtime_status'], 'FAILED')
        self.assertEqual(payload['task_keyword'], 'TODO')
        self.assertEqual(payload['writeback_status'], 'COMPLETED')
        self.assertEqual(payload['failed_flow'], 'statusback')
        self.assertEqual(payload['result_status'], 'SUCCESS')

    def test_run_running_worker_handles_execute_timeout_as_task_failure(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text(
            '\n'.join(
                [
                    '- WAITING 會 timeout 的任務',
                    '    id:: waiting-task-timeout-execute',
                    '    execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        outcome = self.runner.run_running_worker(
            lambda context, instruction: (_ for _ in ()).throw(RuntimeError('Codex CLI execution timed out after 300 seconds')),
            poll_interval_seconds=0.01,
            max_tasks=1,
        )

        self.assertEqual(outcome.processed_count, 0)
        self.assertEqual(outcome.stop_reason, 'task_failed')
        self.assertIsNotNone(outcome.failure)
        assert outcome.failure is not None
        self.assertEqual(outcome.failure.failed_flow, 'execute')
        self.assertIn('timed out after 300 seconds', outcome.failure.error_message)

    def test_run_running_worker_emits_callbacks_per_task_and_failure(self) -> None:
        outcomes = [
            RunnerOutcome(
                task_id='task-1',
                run_id='run-1',
                idempotency_key='wb:1',
                audit_log_path=self.root / 'run_logs' / '1.json',
                answer_page=None,
                final_keyword='TODO',
                executor_type='CODEX',
                result_status='SUCCESS',
                flow_timings={'intake': 1, 'dispatch': 2, 'execute_start': 0, 'execute': 3, 'writeback': 4, 'statusback': 5},
                total_duration_ms=15,
            )
        ]
        failure = TaskFailure(
            task_id='task-2',
            run_id='run-2',
            failed_flow='execute',
            flow_timings={'intake': 1, 'dispatch': 0, 'execute': 9, 'writeback': 0, 'statusback': 0},
            total_duration_ms=10,
            error_message='boom',
        )
        call_count = {'value': 0}
        seen_flows = []
        seen_outcomes = []
        seen_failures = []

        def fake_run_once_internal(executor, *, capture_failure, flow_callback=None):
            call_count['value'] += 1
            if call_count['value'] == 1:
                if flow_callback is not None:
                    for flow_name, duration_ms in outcomes[0].flow_timings.items():
                        flow_callback(type('FlowEventStub', (), {
                            'task_id': outcomes[0].task_id,
                            'run_id': outcomes[0].run_id,
                            'flow_name': flow_name,
                            'duration_ms': duration_ms,
                        })())
                return outcomes[0], None
            return None, failure

        self.runner._run_once_internal = fake_run_once_internal  # type: ignore[method-assign]

        result = self.runner.run_running_worker(
            lambda context, instruction: None,
            poll_interval_seconds=0.01,
            flow_callback=lambda item: seen_flows.append((item.task_id, item.flow_name, item.duration_ms)),
            outcome_callback=lambda item: seen_outcomes.append(item.task_id),
            failure_callback=lambda item: seen_failures.append(item.task_id),
        )

        self.assertEqual(result.processed_count, 1)
        self.assertEqual(result.stop_reason, 'task_failed')
        self.assertEqual(seen_flows, [('task-1', 'intake', 1), ('task-1', 'dispatch', 2), ('task-1', 'execute_start', 0), ('task-1', 'execute', 3), ('task-1', 'writeback', 4), ('task-1', 'statusback', 5)])
        self.assertEqual(seen_outcomes, ['task-1'])
        self.assertEqual(seen_failures, ['task-2'])

    def test_run_running_worker_emits_independent_heartbeat(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text('- TODO 已完成\n    id:: block-uuid-001\n', encoding='utf-8')
        sleep_calls: list[float] = []
        idle_events: list[tuple[int, float]] = []
        heartbeat_events: list[tuple[int, float]] = []

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            if len(sleep_calls) == 3:
                raise KeyboardInterrupt

        outcome = self.runner.run_running_worker(
            lambda context, instruction: None,
            poll_interval_seconds=0.2,
            heartbeat_interval_seconds=0.5,
            sleep_fn=fake_sleep,
            idle_callback=lambda idle_cycles, interval: idle_events.append((idle_cycles, interval)),
            heartbeat_callback=lambda idle_cycles, idle_seconds: heartbeat_events.append((idle_cycles, idle_seconds)),
        )

        self.assertEqual(outcome.processed_count, 0)
        self.assertTrue(outcome.interrupted)
        self.assertEqual(outcome.stop_reason, 'keyboard_interrupt')
        self.assertEqual(outcome.idle_cycles, 3)
        self.assertEqual(sleep_calls, [0.2, 0.2, 0.2])
        self.assertEqual(idle_events, [(1, 0.2), (2, 0.2), (3, 0.2)])
        self.assertEqual(len(heartbeat_events), 1)
        self.assertEqual(heartbeat_events[0][0], 3)
        self.assertAlmostEqual(heartbeat_events[0][1], 0.6, places=6)

    def test_run_running_worker_waits_until_keyboard_interrupt(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text('- TODO 已完成\n    id:: block-uuid-001\n', encoding='utf-8')
        sleep_calls: list[float] = []
        idle_events: list[tuple[int, float]] = []

        def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            raise KeyboardInterrupt

        outcome = self.runner.run_running_worker(
            lambda context, instruction: None,
            poll_interval_seconds=0.25,
            sleep_fn=fake_sleep,
            idle_callback=lambda idle_cycles, interval: idle_events.append((idle_cycles, interval)),
        )

        self.assertEqual(outcome.processed_count, 0)
        self.assertTrue(outcome.interrupted)
        self.assertEqual(outcome.stop_reason, 'keyboard_interrupt')
        self.assertEqual(outcome.idle_cycles, 1)
        self.assertEqual(sleep_calls, [0.25])
        self.assertEqual(idle_events, [(1, 0.25)])

    def test_run_running_worker_stops_after_max_tasks(self) -> None:
        task_outcome = RunnerOutcome(
            task_id='task-1',
            run_id='run-1',
            idempotency_key='task-1:run-1',
            audit_log_path=self.root / 'run_logs' / 'dummy.json',
            answer_page=None,
            final_keyword='TODO',
            executor_type='CODEX',
            result_status='SUCCESS',
        )
        call_count = {'value': 0}

        def fake_run_once_internal(executor, *, capture_failure, flow_callback=None):
            call_count['value'] += 1
            return task_outcome, None

        self.runner._run_once_internal = fake_run_once_internal  # type: ignore[method-assign]

        outcome = self.runner.run_running_worker(lambda context, instruction: None, max_tasks=1, poll_interval_seconds=0.01)

        self.assertEqual(outcome.processed_count, 1)
        self.assertFalse(outcome.interrupted)
        self.assertEqual(outcome.stop_reason, 'max_tasks_reached')
        self.assertEqual(call_count['value'], 1)

    def test_run_once_returns_none_when_no_queueable_task(self) -> None:
        journal = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        journal.write_text('- TODO 已完成\n    id:: block-uuid-001\n', encoding='utf-8')

        outcome = self.runner.run_once(lambda context, instruction: None)

        self.assertIsNone(outcome)


if __name__ == '__main__':
    unittest.main()




