from pathlib import Path
import shutil
import unittest
import uuid
from datetime import date

from app.adapters.logseq_adapter import LogseqAdapter
from app.domain.enums import RuntimeStatus, TaskKeyword


class LogseqAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_tmp = Path(__file__).resolve().parent / '_tmp'
        self.root = self.workspace_tmp / uuid.uuid4().hex
        (self.root / 'journals').mkdir(parents=True, exist_ok=True)
        (self.root / 'pages').mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.journal_path = self.root / 'journals' / '2026_03_15.md'
        self.journal_path.write_text(
            '\n'.join(
                [
                    '- DOING 分析 [[文章1]] 並整理結論',
                    '    id:: block-uuid-001',
                    '    priority:: 5',
                    '    max_retries:: 4',
                    '    - Question: 請比較重點',
                    '- WAITING 已入佇列任務',
                    '    id:: block-uuid-003',
                    '- TODO 第二個任務',
                    '    id:: block-uuid-002',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        self.adapter = LogseqAdapter(self.root)

    def test_scan_doing_tasks_parses_properties_and_links(self) -> None:
        records = self.adapter.scan_doing_tasks()

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.task.task_keyword, TaskKeyword.DOING)
        self.assertEqual(record.task.block_uuid, 'block-uuid-001')
        self.assertEqual(record.task.task_id, 'block-uuid-001')
        self.assertEqual(record.task.priority, 5)
        self.assertEqual(record.task.max_retries, 4)
        self.assertEqual(record.task.page_links, ['文章1'])

    def test_scan_waiting_tasks_returns_existing_waiting_queue_items(self) -> None:
        records = self.adapter.scan_waiting_tasks()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].task.task_keyword, TaskKeyword.WAITING)
        self.assertEqual(records[0].task.task_id, 'block-uuid-003')

    def test_scan_waiting_task_accepts_two_space_property_indent(self) -> None:
        self.journal_path.write_text(
            '\n'.join(
                [
                    '- WAITING prisma version 7 有甚麼新功能?',
                    '  id:: 31d8a993-b679-445f-9b79-7fb9cb7d305e',
                    '  execution_mode:: codex',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        records = self.adapter.scan_waiting_tasks()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].task.task_id, '31d8a993-b679-445f-9b79-7fb9cb7d305e')

    def test_scan_tasks_respects_recent_journal_window(self) -> None:
        old_journal = self.root / 'journals' / '2026_03_10.md'
        old_journal.write_text('- DOING 舊任務\n    id:: old-task-001\n', encoding='utf-8')
        recent_adapter = LogseqAdapter(
            self.root,
            journal_scan_days=3,
            reference_date=date(2026, 3, 15),
        )

        doing_records = recent_adapter.scan_doing_tasks()

        self.assertEqual([record.task.task_id for record in doing_records], ['block-uuid-001'])

    def test_normalize_task_id_assigns_uuid_v4_and_reloads_record(self) -> None:
        self.journal_path.write_text(
            '\n'.join(
                [
                    '- DOING 缺少 id 的任務',
                    '  priority:: 1',
                    '',
                ]
            ),
            encoding='utf-8',
        )

        record = self.adapter.scan_doing_tasks()[0]
        normalized = self.adapter.normalize_task_id(record)

        self.assertTrue(normalized.task.task_id)
        parsed_uuid = uuid.UUID(normalized.task.task_id)
        self.assertEqual(str(parsed_uuid), normalized.task.task_id)
        self.assertEqual(normalized.task.block_uuid, normalized.task.task_id)
        content = self.journal_path.read_text(encoding='utf-8')
        self.assertIn(f'  id:: {normalized.task.task_id}', content)
        self.assertIn('  priority:: 1', content)

    def test_lock_task_switches_keyword_without_persisting_runtime_fields(self) -> None:
        record = self.adapter.scan_doing_tasks()[0]

        locked = self.adapter.lock_task(
            record,
            lock_owner='runner-1',
            locked_at='2026-03-15T12:00:00+08:00',
            run_id='run-001',
            idempotency_key='block-uuid-001:run-001',
        )

        self.assertEqual(locked.task.task_keyword, TaskKeyword.WAITING)
        self.assertEqual(locked.task.runtime_status, RuntimeStatus.RUNNING)
        self.assertEqual(locked.task.lock_owner, 'runner-1')
        self.assertEqual(locked.task.run_id, 'run-001')
        content = self.journal_path.read_text(encoding='utf-8')
        self.assertIn('- WAITING 分析 [[文章1]] 並整理結論', content)
        self.assertNotIn('task_runner_status::', content)
        self.assertNotIn('run_id::', content)
        self.assertNotIn('locked_at::', content)
        self.assertNotIn('lock_owner::', content)

    def test_write_answer_page_creates_expected_file(self) -> None:
        record = self.adapter.scan_doing_tasks()[0]
        target = self.adapter.write_answer_page(record, '# Answer\n\n內容')

        self.assertEqual(target, self.root / 'pages' / 'answer' / '20260315__blockuui.md')
        self.assertEqual(target.read_text(encoding='utf-8'), '# Answer\n\n內容')

    def test_append_journal_link_is_idempotent(self) -> None:
        record = self.adapter.scan_doing_tasks()[0]
        page_name = self.adapter.build_answer_page_name(record)

        first = self.adapter.append_journal_link(record, page_name)
        second = self.adapter.append_journal_link(record, page_name)

        self.assertTrue(first)
        self.assertFalse(second)
        first_line = self.journal_path.read_text(encoding='utf-8').splitlines()[0]
        self.assertEqual(first_line.count(f'[[{page_name}]]'), 1)

    def test_generated_answer_page_name_is_stable(self) -> None:
        record = self.adapter.scan_doing_tasks()[0]

        self.assertEqual(self.adapter.build_answer_page_name(record), '20260315__blockuui')
        self.assertEqual(self.adapter.build_answer_page_relative_path(record), 'answer/20260315__blockuui.md')

    def test_answer_page_name_uses_journal_date_segment(self) -> None:
        record = self.adapter.scan_doing_tasks()[0]

        self.assertEqual(self.adapter.build_answer_page_name(record), '20260315__blockuui')

    def test_update_block_properties_merges_new_values(self) -> None:
        record = self.adapter.scan_doing_tasks()[0]

        updated = self.adapter.update_block_properties(
            record,
            {'error_reason': 'timeout', 'failed_at': '2026-03-15T12:00:00+08:00'},
        )

        self.assertEqual(updated.task.properties['error_reason'], 'timeout')
        self.assertEqual(updated.task.properties['failed_at'], '2026-03-15T12:00:00+08:00')
        content = self.journal_path.read_text(encoding='utf-8')
        self.assertIn('  error_reason:: timeout', content)
        self.assertIn('  failed_at:: 2026-03-15T12:00:00+08:00', content)


if __name__ == '__main__':
    unittest.main()
