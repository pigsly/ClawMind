from pathlib import Path
import shutil
import unittest
import uuid

from app.adapters.logseq_adapter import LogseqAdapter
from app.domain.enums import ArtifactType, ResultStatus, RuntimeStatus, TaskKeyword
from app.domain.models import ExecutionResult
from app.executors.deterministic_executor import DeterministicExecutor


class DeterministicExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / '_tmp_deterministic' / uuid.uuid4().hex
        (self.root / 'logseq' / 'journals').mkdir(parents=True, exist_ok=True)
        (self.root / 'logseq' / 'pages').mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.journal_path = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        self.journal_path.write_text(
            '\n'.join(
                [
                    '- WAITING 整理結果',
                    '    id:: block-uuid-001',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        self.adapter = LogseqAdapter(self.root / 'logseq')
        self.executor = DeterministicExecutor(self.adapter)
        self.record = self.adapter._parse_record(self.journal_path, 0)
        self.page_name = self.adapter.build_answer_page_name(self.record)

    def test_apply_writeback_writes_answer_without_changing_keyword(self) -> None:
        result = ExecutionResult(
            result_status=ResultStatus.SUCCESS,
            artifact_content='# Answer\n\n內容',
            artifact_type=ArtifactType.MARKDOWN,
            target_file=None,
            confidence=0.88,
            assumptions=['assumption'],
            unresolved_items=[],
        )

        outcome = self.executor.apply_writeback(
            self.record,
            result,
            finished_at='2026-03-15T12:00:00+08:00',
            idempotency_key='block-uuid-001:run-001',
        )

        self.assertTrue(outcome.answer_page is not None and outcome.answer_page.exists())
        self.assertEqual(outcome.answer_page.name, '20260315__blockuui.md')
        self.assertTrue(outcome.appended_link)
        self.assertFalse(outcome.idempotent_replay)
        self.assertEqual(outcome.record.task.runtime_status, RuntimeStatus.SUCCEEDED)
        self.assertEqual(outcome.record.task.task_keyword, TaskKeyword.WAITING)
        content = self.journal_path.read_text(encoding='utf-8')
        self.assertIn(f'[[{self.page_name}]]', content)
        self.assertIn('- WAITING 整理結果', content)
        self.assertNotIn('result::', content)
        self.assertNotIn('last_writeback_idempotency_key::', content)

    def test_apply_writeback_is_idempotent_when_answer_page_and_link_exist(self) -> None:
        result = ExecutionResult(
            result_status=ResultStatus.SUCCESS,
            artifact_content='# Answer\n\n內容',
            artifact_type=ArtifactType.MARKDOWN,
            target_file=None,
            confidence=0.88,
        )
        first = self.executor.apply_writeback(
            self.record,
            result,
            finished_at='2026-03-15T12:00:00+08:00',
            idempotency_key='block-uuid-001:run-001',
        )
        second_record = self.adapter._parse_record(self.journal_path, 0)

        second = self.executor.apply_writeback(
            second_record,
            result,
            finished_at='2026-03-15T12:01:00+08:00',
            idempotency_key='block-uuid-001:run-001',
        )

        self.assertTrue(first.appended_link)
        self.assertTrue(second.idempotent_replay)
        self.assertFalse(second.appended_link)
        line = self.journal_path.read_text(encoding='utf-8').splitlines()[0]
        self.assertEqual(line.count(f'[[{self.page_name}]]'), 1)
        self.assertIn('- WAITING 整理結果', line)

    def test_apply_writeback_records_failure_exception_fields(self) -> None:
        result = ExecutionResult(
            result_status=ResultStatus.FAILED,
            artifact_content=None,
            artifact_type=ArtifactType.NONE,
            target_file=None,
            unresolved_items=['timeout while waiting'],
            confidence=0.10,
        )

        outcome = self.executor.apply_writeback(
            self.record,
            result,
            finished_at='2026-03-15T12:02:00+08:00',
            idempotency_key='block-uuid-001:run-002',
        )

        self.assertIsNone(outcome.answer_page)
        self.assertEqual(outcome.record.task.runtime_status, RuntimeStatus.FAILED)
        self.assertEqual(outcome.record.task.task_keyword, TaskKeyword.WAITING)
        content = self.journal_path.read_text(encoding='utf-8')
        self.assertIn('error_reason:: timeout while waiting', content)
        self.assertIn('failed_at:: 2026-03-15T12:02:00+08:00', content)
        self.assertNotIn('result::', content)


if __name__ == '__main__':
    unittest.main()
