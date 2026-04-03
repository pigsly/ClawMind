import json
import unittest

from app.domain.contracts import WritebackContract
from app.domain.enums import (
    AnalysisMode,
    AnswerType,
    ArtifactType,
    ExecutorType,
    ResultStatus,
    RuntimeStatus,
    TaskKeyword,
    TaskType,
)
from app.domain.models import ContextBundle, ExecutionResult, InstructionBundle, Task, UncertaintyItem
from app.policies.context_options import ContextOptions


class DomainModelRoundTripTests(unittest.TestCase):
    def build_task(self) -> Task:
        return Task(
            task_id='20260315_01',
            run_id='run-001',
            idempotency_key='20260315_01:run-001',
            task_keyword=TaskKeyword.DOING,
            runtime_status=RuntimeStatus.PENDING,
            priority=10,
            retry_count=0,
            max_retries=2,
            locked_at=None,
            lock_owner=None,
            created_at='2026-03-15T10:00:00+08:00',
            updated_at='2026-03-15T10:00:00+08:00',
            block_uuid='block-uuid-001',
            page_id='2026_03_15',
            raw_block_text='DOING 分析 [[文章1]] 並整理結論',
            properties={'execution_mode': 'codex', 'load_memory': 'false'},
            page_links=['文章1'],
        )

    def test_task_json_round_trip(self) -> None:
        original = self.build_task()

        serialized = json.dumps(original.to_dict(), ensure_ascii=False)
        restored = Task.from_dict(json.loads(serialized))

        self.assertEqual(restored, original)

    def test_instruction_bundle_round_trip(self) -> None:
        original = InstructionBundle(
            task_type=TaskType.REASONING_ANALYSIS,
            analysis_mode=AnalysisMode.REASONING_ANALYSIS,
            executor_type=ExecutorType.CODEX,
            model='gpt-5.4',
            template_id='analysis_v1',
            instruction_patch='Focus on source-backed analysis.',
            expected_output_type='markdown',
            validation_rules=['must_include_confidence', 'must_include_assumptions'],
        )

        restored = InstructionBundle.from_dict(json.loads(json.dumps(original.to_dict())))

        self.assertEqual(restored, original)

    def test_context_bundle_round_trip(self) -> None:
        original = ContextBundle(
            task=self.build_task(),
            pages={'文章1': '這是一段頁面內容'},
            memory={'memory/retro': '先前結論'},
            adr={'ADR-001': '選擇單一 writeback actor'},
            skill_context={'skill': 'analysis'},
            context_options=ContextOptions(load_memory=True, execution_mode='mixed'),
        )

        restored = ContextBundle.from_dict(json.loads(json.dumps(original.to_dict())))

        self.assertEqual(restored, original)

    def test_execution_result_round_trip(self) -> None:
        original = ExecutionResult(
            result_status=ResultStatus.PARTIAL,
            artifact_content='# Answer\n\n初步分析',
            artifact_type=ArtifactType.MARKDOWN,
            target_file='logseq/pages/answer/20260315__blockuui.md',
            links_to_append=['[[20260315__blockuui]]'],
            writeback_actions=['write_answer_page', 'append_journal_link'],
            unresolved_items=[],
            answer_type=AnswerType.BEST_EFFORT,
            summary='初步結論',
            answer_paragraphs=['第一段說明'],
            uncertainty=[UncertaintyItem(type='missing_source', impact='medium', description='缺少原始文件。')],
            confidence=0.72,
            assumptions=['linked page 內容為最新版本'],
            audit_log={'executor_type': 'CODEX'},
            writeback_contract=WritebackContract(
                task_id='20260315_01',
                run_id='run-001',
                idempotency_key='20260315_01:run-001',
                result_status='PARTIAL',
                target_file='logseq/pages/answer/20260315__blockuui.md',
                links_to_append=['[[20260315__blockuui]]'],
                writeback_actions=['write_answer_page'],
                writeback_status='PENDING',
            ),
        )

        restored = ExecutionResult.from_dict(json.loads(json.dumps(original.to_dict())))

        self.assertEqual(restored, original)


if __name__ == '__main__':
    unittest.main()
