from pathlib import Path
import json
import shutil
import unittest
import uuid

from app.adapters.logseq_adapter import LogseqAdapter
from app.application.recovery_service import RecoveryService


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / '_tmp_recovery' / uuid.uuid4().hex
        (self.root / 'logseq' / 'journals').mkdir(parents=True, exist_ok=True)
        (self.root / 'run_logs').mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.journal_path = self.root / 'logseq' / 'journals' / '2026_03_15.md'
        self.adapter = LogseqAdapter(self.root / 'logseq')
        self.service = RecoveryService(
            self.adapter,
            run_logs_dir=self.root / 'run_logs',
            lock_timeout_minutes=15,
        )

    def write_waiting_task(self, *, retry_count: int = 0, max_retries: int = 2) -> None:
        self.journal_path.write_text(
            '\n'.join(
                [
                    '- WAITING 超時任務',
                    '    id:: block-uuid-001',
                    f'    retry_count:: {retry_count}',
                    f'    max_retries:: {max_retries}',
                    '',
                ]
            ),
            encoding='utf-8',
        )

    def write_runtime_record(self, *, retry_count: int, max_retries: int, locked_at: str) -> None:
        target_dir = self.root / 'run_logs' / 'block-uuid-001'
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            'task_id': 'block-uuid-001',
            'run_id': 'run-001',
            'locked_at': locked_at,
            'retry_count': retry_count,
            'max_retries': max_retries,
            'runtime_status': 'RUNNING',
            'writeback_status': 'PENDING',
        }
        (target_dir / 'run-001.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def test_recover_if_timed_out_returns_to_todo_using_runtime_record(self) -> None:
        self.write_waiting_task(retry_count=0, max_retries=2)
        self.write_runtime_record(retry_count=0, max_retries=2, locked_at='2026-03-15T12:00:00+08:00')
        record = self.adapter._parse_record(self.journal_path, 0)

        outcome = self.service.recover_if_timed_out(record, now_iso='2026-03-15T12:20:01+08:00')

        self.assertTrue(outcome.timed_out)
        self.assertTrue(outcome.retried)
        self.assertFalse(outcome.exhausted)
        self.assertEqual(outcome.record.task.retry_count, 1)
        content = self.journal_path.read_text(encoding='utf-8')
        self.assertIn('- TODO 超時任務', content)
        runtime_payload = json.loads((self.root / 'run_logs' / 'block-uuid-001' / 'run-001.json').read_text(encoding='utf-8'))
        self.assertEqual(runtime_payload['runtime_status'], 'TIMEOUT')
        self.assertEqual(runtime_payload['retry_count'], 1)
        self.assertTrue(runtime_payload['timeout_recovered'])

    def test_recover_if_timed_out_marks_failed_with_allowed_error_fields(self) -> None:
        self.write_waiting_task(retry_count=1, max_retries=2)
        self.write_runtime_record(retry_count=1, max_retries=2, locked_at='2026-03-15T12:00:00+08:00')
        record = self.adapter._parse_record(self.journal_path, 0)

        outcome = self.service.recover_if_timed_out(record, now_iso='2026-03-15T12:30:00+08:00')

        self.assertTrue(outcome.exhausted)
        content = self.journal_path.read_text(encoding='utf-8')
        self.assertIn('error_reason:: lock_timeout', content)
        self.assertIn('failed_at:: 2026-03-15T12:30:00+08:00', content)
        runtime_payload = json.loads((self.root / 'run_logs' / 'block-uuid-001' / 'run-001.json').read_text(encoding='utf-8'))
        self.assertEqual(runtime_payload['runtime_status'], 'FAILED')
        self.assertEqual(runtime_payload['retry_count'], 2)

    def test_recover_if_timed_out_ignores_non_expired_lock(self) -> None:
        self.write_waiting_task()
        self.write_runtime_record(retry_count=0, max_retries=2, locked_at='2026-03-15T12:10:00+08:00')
        record = self.adapter._parse_record(self.journal_path, 0)

        outcome = self.service.recover_if_timed_out(record, now_iso='2026-03-15T12:20:00+08:00')

        self.assertFalse(outcome.timed_out)
        self.assertFalse(outcome.retried)
        self.assertFalse(outcome.exhausted)


if __name__ == '__main__':
    unittest.main()
