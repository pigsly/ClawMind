import unittest

from app.adapters.llm_adapter import LlmAdapter
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
from app.domain.models import ContextBundle, InstructionBundle, Task
from app.executors.codex_runner import CodexRunner


class FakeLlmAdapter(LlmAdapter):
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_structured(self, context_bundle, instruction_bundle):
        self.calls.append((context_bundle, instruction_bundle))
        return self.payload


class CodexRunnerTests(unittest.TestCase):
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

    def test_run_formats_structured_answer_into_markdown(self) -> None:
        adapter = FakeLlmAdapter(
            {
                'result_status': 'PARTIAL',
                'answer_type': 'BEST_EFFORT',
                'summary': '市場常見鮮食品系通常可概括為 4 到 5 種。',
                'answer_paragraphs': [
                    '若你是用市場常見品系來問，通常可概括成 4 到 5 種主流鮮食品系。',
                    '若你是用栽培過的品種來問，數量就不只這些。',
                ],
                'uncertainty': [
                    {
                        'type': 'scope',
                        'impact': 'medium',
                        'description': '問題沒有說明是市場分類還是完整栽培品種。',
                    }
                ],
                'artifact_content': None,
                'artifact_type': 'MARKDOWN',
                'target_file': None,
                'writeback_actions': ['write_answer_page'],
                'confidence': 0.72,
                'assumptions': ['問題指的是市場常見分類。'],
                'audit_log': {'tools_used': ['codex'], 'notes': None},
            }
        )
        runner = CodexRunner(adapter)

        result = runner.run(self.build_context(), self.build_instruction())

        self.assertEqual(result.result_status, ResultStatus.PARTIAL)
        self.assertEqual(result.answer_type, AnswerType.BEST_EFFORT)
        self.assertEqual(result.artifact_type, ArtifactType.MARKDOWN)
        self.assertIn('Conclusion:', result.artifact_content)
        self.assertIn('Explanation:', result.artifact_content)
        self.assertIn('Assumptions:', result.artifact_content)
        self.assertIn('Uncertainty:', result.artifact_content)
        self.assertIn('Confidence: 0.72', result.artifact_content)
        self.assertIn('[medium/scope]', result.artifact_content)
        self.assertEqual(result.summary, '市場常見鮮食品系通常可概括為 4 到 5 種。')
        self.assertEqual(result.answer_paragraphs[0], '若你是用市場常見品系來問，通常可概括成 4 到 5 種主流鮮食品系。')
        self.assertEqual(result.assumptions, ['問題指的是市場常見分類。'])
        self.assertEqual(result.unresolved_items, [])
        self.assertEqual(len(adapter.calls), 1)

    def test_run_backfills_summary_and_empty_structured_fields_without_machine_tags(self) -> None:
        adapter = FakeLlmAdapter(
            {
                'result_status': 'OPEN_QUESTION',
                'answer_type': 'BEST_EFFORT',
                'summary': None,
                'answer_paragraphs': [],
                'uncertainty': [],
                'artifact_content': None,
                'artifact_type': 'TEXT',
                'target_file': None,
                'confidence': 1.5,
                'audit_log': {'tools_used': [], 'notes': None},
            }
        )
        runner = CodexRunner(adapter)

        result = runner.run(self.build_context(), self.build_instruction())

        self.assertEqual(result.result_status, ResultStatus.OPEN_QUESTION)
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.summary, '根據目前可得資訊，這是最合理的初步結論。')
        self.assertEqual(result.uncertainty, [])
        self.assertEqual(result.assumptions, [])
        self.assertEqual(result.unresolved_items, [])
        self.assertNotIn('[Data missing]', result.artifact_content)

    def test_run_forces_low_confidence_answer_to_hypothesis(self) -> None:
        adapter = FakeLlmAdapter(
            {
                'result_status': 'SUCCESS',
                'answer_type': 'DIRECT_ANSWER',
                'summary': '這比較像是假設性的市場訊號。',
                'answer_paragraphs': ['目前看起來更像談判訊號，不像正式制度安排。'],
                'uncertainty': [
                    {
                        'type': 'missing_source',
                        'impact': 'high',
                        'description': '缺少原始發言全文。',
                    }
                ],
                'artifact_content': None,
                'artifact_type': 'MARKDOWN',
                'target_file': None,
                'confidence': 0.3,
                'assumptions': [],
                'audit_log': {'tools_used': ['codex'], 'notes': None},
            }
        )
        runner = CodexRunner(adapter)

        result = runner.run(self.build_context(), self.build_instruction())

        self.assertEqual(result.answer_type, AnswerType.HYPOTHESIS)
        assert result.artifact_content is not None
        self.assertIn('（假設）', result.artifact_content)

    def test_run_sanitizes_machine_tags_from_answer_and_uncertainty(self) -> None:
        adapter = FakeLlmAdapter(
            {
                'result_status': 'SUCCESS',
                'answer_type': 'BEST_EFFORT',
                'summary': '[Data missing] 目前最合理的解讀是政治試探。',
                'answer_paragraphs': ['[Data missing] 這更像是外交與市場安撫訊號。'],
                'uncertainty': [
                    {
                        'type': 'missing_source',
                        'impact': 'medium',
                        'description': '[Data missing] 缺少原始新聞來源。',
                    }
                ],
                'artifact_content': None,
                'artifact_type': 'MARKDOWN',
                'target_file': None,
                'confidence': 0.65,
                'assumptions': ['[Data missing] 假設這是近期公開表態。'],
                'audit_log': {'tools_used': ['codex'], 'notes': '[Data missing] note'},
            }
        )
        runner = CodexRunner(adapter)

        result = runner.run(self.build_context(), self.build_instruction())

        self.assertEqual(result.summary, '目前最合理的解讀是政治試探。')
        self.assertEqual(result.answer_paragraphs, ['這更像是外交與市場安撫訊號。'])
        self.assertEqual(result.uncertainty[0].description, '缺少原始新聞來源。')
        self.assertEqual(result.assumptions, ['假設這是近期公開表態。'])
        self.assertEqual(result.audit_log['notes'], 'note')
        self.assertNotIn('[Data missing]', result.artifact_content)


if __name__ == '__main__':
    unittest.main()

