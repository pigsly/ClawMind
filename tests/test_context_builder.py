from pathlib import Path
import json
import shutil
import unittest
import uuid

from app.application.context_builder import ContextBuilder
from app.domain.enums import AnalysisMode, ExecutorType, RuntimeStatus, TaskKeyword, TaskType
from app.domain.models import InstructionBundle, Task
from app.policies.context_options import ContextOptions


class ContextBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / '_tmp_context' / uuid.uuid4().hex
        (self.root / 'logseq' / 'journals').mkdir(parents=True, exist_ok=True)
        (self.root / 'logseq' / 'pages' / 'answer').mkdir(parents=True, exist_ok=True)
        (self.root / 'logseq' / 'pages' / 'memory').mkdir(parents=True, exist_ok=True)
        (self.root / 'logseq' / 'pages' / 'ADR').mkdir(parents=True, exist_ok=True)
        (self.root / 'run_logs' / 'task-20260315-abc123').mkdir(parents=True, exist_ok=True)
        (self.root / 'runtime_artifacts' / 'task-20260315-abc123' / 'run-001').mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        (self.root / 'logseq' / 'journals' / '2026_03_15.md').write_text('journal page content', encoding='utf-8')
        (self.root / 'logseq' / 'pages' / 'answer' / '文章1.md').write_text('answer page content', encoding='utf-8')
        (self.root / 'logseq' / 'pages' / 'answer' / '文章2.md').write_text('answer page content 2', encoding='utf-8')
        (self.root / 'logseq' / 'pages' / 'memory' / 'retro.md').write_text('memory content', encoding='utf-8')
        (self.root / 'logseq' / 'pages' / 'ADR' / 'ADR-001.md').write_text('adr content', encoding='utf-8')
        (self.root / 'run_logs' / 'task-20260315-abc123' / 'run-001.json').write_text(
            json.dumps({'status': 'ok'}, ensure_ascii=False),
            encoding='utf-8',
        )
        (self.root / 'runtime_artifacts' / 'task-20260315-abc123' / 'run-001' / 'notes.txt').write_text(
            'artifact content',
            encoding='utf-8',
        )
        self.builder = ContextBuilder(
            self.root / 'logseq',
            run_logs_dir=self.root / 'run_logs',
            runtime_artifacts_dir=self.root / 'runtime_artifacts',
        )

    def build_task(self, **property_overrides: str) -> Task:
        properties = {
            'load_memory': 'false',
            'load_adr': 'false',
            'load_linked_pages': 'true',
            'debugging_mode': 'false',
            'execution_mode': 'codex',
        }
        properties.update(property_overrides)
        return Task(
            task_id='task-20260315-abc123',
            run_id='run-001',
            idempotency_key='task-20260315-abc123:run-001',
            task_keyword=TaskKeyword.DOING,
            runtime_status=RuntimeStatus.PENDING,
            priority=0,
            retry_count=0,
            max_retries=2,
            locked_at=None,
            lock_owner=None,
            created_at='2026-03-15T10:00:00+08:00',
            updated_at='2026-03-15T10:00:00+08:00',
            block_uuid='block-uuid-001',
            page_id='2026_03_15',
            raw_block_text='- DOING 分析 [[文章1]] 並整理結論',
            properties=properties,
            page_links=['文章1', '文章2'],
        )

    def build_instruction(self, analysis_mode: AnalysisMode) -> InstructionBundle:
        return InstructionBundle(
            task_type=TaskType.MARKDOWN_APPEND,
            analysis_mode=analysis_mode,
            executor_type=ExecutorType.CODEX,
            model='gpt-5.4-mini' if analysis_mode == AnalysisMode.NORMAL else 'gpt-5.4',
        )

    def test_build_loads_only_one_linked_page_for_normal_mode(self) -> None:
        bundle = self.builder.build(self.build_task(), instruction_bundle=self.build_instruction(AnalysisMode.NORMAL))

        self.assertEqual(bundle.pages, {'文章1': 'answer page content'})
        self.assertEqual(bundle.memory, {})
        self.assertEqual(bundle.adr, {})
        self.assertEqual(bundle.skill_context, {})

    def test_build_with_audit_records_found_and_loaded_linked_pages(self) -> None:
        bundle, audit = self.builder.build_with_audit(
            self.build_task(),
            instruction_bundle=self.build_instruction(AnalysisMode.NORMAL),
        )

        self.assertEqual(bundle.pages, {'文章1': 'answer page content'})
        linked_page_context = audit['linked_page_context']
        self.assertEqual(linked_page_context['requested_page_links'], ['文章1', '文章2'])
        self.assertEqual(linked_page_context['selected_page_links'], ['文章1'])
        self.assertEqual(
            linked_page_context['resolution'],
            [
                {
                    'page_name': '文章1',
                    'selected_for_context': True,
                    'found_page': True,
                    'loaded_to_context': True,
                    'resolved_path': str(self.root / 'logseq' / 'pages' / 'answer' / '文章1.md'),
                },
                {
                    'page_name': '文章2',
                    'selected_for_context': False,
                    'found_page': True,
                    'loaded_to_context': False,
                    'resolved_path': str(self.root / 'logseq' / 'pages' / 'answer' / '文章2.md'),
                },
            ],
        )

    def test_build_loads_current_page_and_adr_for_reasoning_mode(self) -> None:
        bundle = self.builder.build(
            self.build_task(),
            instruction_bundle=self.build_instruction(AnalysisMode.REASONING_ANALYSIS),
        )

        self.assertIn('2026_03_15', bundle.pages)
        self.assertIn('文章1', bundle.pages)
        self.assertIn('文章2', bundle.pages)
        self.assertEqual(bundle.adr, {'ADR-001': 'adr content'})

    def test_build_loads_all_linked_pages_for_cross_page_mode(self) -> None:
        bundle = self.builder.build(
            self.build_task(),
            instruction_bundle=self.build_instruction(AnalysisMode.CROSS_PAGE_SYNTHESIS),
        )

        self.assertIn('文章1', bundle.pages)
        self.assertIn('文章2', bundle.pages)
        self.assertIn('2026_03_15', bundle.pages)

    def test_build_ignores_block_debugging_mode_override(self) -> None:
        bundle = self.builder.build(self.build_task(debugging_mode='true'))

        self.assertEqual(bundle.skill_context, {})
        self.assertFalse(bundle.context_options.debugging_mode)

    def test_build_includes_debug_context_only_when_runtime_debug_enabled(self) -> None:
        bundle = self.builder.build(
            self.build_task(),
            instruction_bundle=self.build_instruction(AnalysisMode.NORMAL),
            runtime_options=ContextOptions(debugging_mode=True),
        )

        self.assertIn('run_logs/run-001.json', bundle.skill_context)
        self.assertIn('runtime_artifacts/task-20260315-abc123/run-001/notes.txt', bundle.skill_context)
        self.assertTrue(bundle.context_options.debugging_mode)


if __name__ == '__main__':
    unittest.main()

