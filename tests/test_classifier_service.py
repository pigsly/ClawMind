import unittest
from unittest.mock import patch

import app.application.classifier_service as classifier_module
from app.application.classifier_service import ClassifierService, PhraseMatcher, _SubstringKeywordProcessor
from app.domain.enums import AnalysisMode, ExecutorType, RuntimeStatus, TaskKeyword, TaskType
from app.domain.models import Task


class ClassifierServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ClassifierService()

    def build_task(self, *, raw_block_text: str, properties: dict[str, str] | None = None, page_links: list[str] | None = None) -> Task:
        return Task(
            task_id='task-20260315-abc123',
            run_id='',
            idempotency_key='',
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
            raw_block_text=raw_block_text,
            properties=properties or {},
            page_links=page_links or [],
        )

    def test_task_type_property_has_highest_priority(self) -> None:
        task = self.build_task(
            raw_block_text='- DOING 請幫我分析',
            properties={'task_type': 'LINK_APPEND', 'execution_mode': 'codex'},
        )

        instruction = self.service.classify(task)

        self.assertEqual(instruction.task_type, TaskType.LINK_APPEND)
        self.assertEqual(instruction.analysis_mode, AnalysisMode.NORMAL)
        self.assertEqual(instruction.executor_type, ExecutorType.CODEX)
        self.assertEqual(instruction.model, 'gpt-5.4-mini')

    def test_execution_mode_deterministic_forces_deterministic_executor(self) -> None:
        task = self.build_task(
            raw_block_text='- DOING 幫我分析這篇文章',
            properties={'execution_mode': 'deterministic'},
        )

        instruction = self.service.classify(task)

        self.assertEqual(instruction.task_type, TaskType.MARKDOWN_APPEND)
        self.assertEqual(instruction.analysis_mode, AnalysisMode.NORMAL)
        self.assertEqual(instruction.executor_type, ExecutorType.DETERMINISTIC)
        self.assertIsNone(instruction.model)

    def test_keyword_rule_routes_metadata_update_to_deterministic(self) -> None:
        task = self.build_task(raw_block_text='- DOING 更新這筆 block properties 與 metadata')

        instruction = self.service.classify(task)

        self.assertEqual(instruction.task_type, TaskType.METADATA_UPDATE)
        self.assertEqual(instruction.analysis_mode, AnalysisMode.NORMAL)
        self.assertEqual(instruction.executor_type, ExecutorType.DETERMINISTIC)

    def test_analysis_mode_uses_cross_page_when_two_or_more_links(self) -> None:
        task = self.build_task(
            raw_block_text='- DOING 綜整 [[A]] [[B]]',
            page_links=['A', 'B'],
        )

        instruction = self.service.classify(task)

        self.assertEqual(instruction.analysis_mode, AnalysisMode.CROSS_PAGE_SYNTHESIS)
        self.assertEqual(instruction.executor_type, ExecutorType.CODEX)
        self.assertEqual(instruction.model, 'gpt-5.4')

    def test_unmatched_task_defaults_to_normal_mode(self) -> None:
        task = self.build_task(raw_block_text='- DOING 協助看看這個問題')

        instruction = self.service.classify(task)

        self.assertEqual(instruction.task_type, TaskType.MARKDOWN_APPEND)
        self.assertEqual(instruction.analysis_mode, AnalysisMode.NORMAL)
        self.assertEqual(instruction.executor_type, ExecutorType.CODEX)
        self.assertEqual(instruction.model, 'gpt-5.4-mini')

    def test_general_question_uses_codex_fast_model_instead_of_deterministic_fallback(self) -> None:
        task = self.build_task(raw_block_text='- DOING 美伊共同控制荷姆茲海峽，對於美元指數、金價有何影響?')

        instruction = self.service.classify(task)

        self.assertEqual(instruction.task_type, TaskType.MARKDOWN_APPEND)
        self.assertEqual(instruction.analysis_mode, AnalysisMode.NORMAL)
        self.assertEqual(instruction.executor_type, ExecutorType.CODEX)
        self.assertEqual(instruction.model, 'gpt-5.4-mini')

    def test_reasoning_cues_raise_analysis_mode(self) -> None:
        task = self.build_task(raw_block_text='- DOING 為什麼這個方案比較好，請分析差異與取捨')

        instruction = self.service.classify(task)

        self.assertEqual(instruction.analysis_mode, AnalysisMode.REASONING_ANALYSIS)
        self.assertEqual(instruction.model, 'gpt-5.4')

    def test_uncertainty_weighted_cues_raise_reasoning_analysis(self) -> None:
        task = self.build_task(raw_block_text='- DOING 我想比較兩種方案，請給建議')

        instruction = self.service.classify(task)

        self.assertEqual(instruction.analysis_mode, AnalysisMode.REASONING_ANALYSIS)
        self.assertEqual(instruction.model, 'gpt-5.4')

    def test_english_uncertainty_cues_raise_reasoning_analysis(self) -> None:
        task = self.build_task(
            raw_block_text='- DOING compare A and B, and tell me what do you recommend for this workflow'
        )

        instruction = self.service.classify(task)

        self.assertEqual(instruction.analysis_mode, AnalysisMode.REASONING_ANALYSIS)
        self.assertEqual(instruction.model, 'gpt-5.4')

    def test_simplified_chinese_uncertainty_cues_raise_reasoning_analysis(self) -> None:
        task = self.build_task(raw_block_text='- DOING 我想比较两种方案，请给建议并检查差异')

        instruction = self.service.classify(task)

        self.assertEqual(instruction.analysis_mode, AnalysisMode.REASONING_ANALYSIS)
        self.assertEqual(instruction.model, 'gpt-5.4')

    def test_english_single_words_do_not_count_as_uncertainty_phrases(self) -> None:
        task = self.build_task(raw_block_text='- DOING compare recommend optimize tradeoff')

        instruction = self.service.classify(task)

        self.assertEqual(instruction.analysis_mode, AnalysisMode.NORMAL)
        self.assertEqual(instruction.model, 'gpt-5.4-mini')

    def test_phrase_matcher_falls_back_when_flashtext_unavailable(self) -> None:
        with patch.object(classifier_module, 'FlashTextKeywordProcessor', None):
            matcher = PhraseMatcher(('what do you recommend',))

        self.assertEqual(matcher.backend_name, 'substring_fallback')
        self.assertTrue(isinstance(matcher._processor, _SubstringKeywordProcessor))
        self.assertTrue(matcher.contains_any('What do you recommend for this task?'))

    def test_phrase_matcher_prefers_flashtext_when_available(self) -> None:
        class FakeKeywordProcessor:
            def __init__(self, case_sensitive: bool = False) -> None:
                self.entries: list[tuple[str, str]] = []

            def add_keyword(self, keyword: str, clean_name: str | None = None) -> None:
                self.entries.append((keyword, clean_name or keyword))

            def extract_keywords(self, text: str) -> list[str]:
                lowered = text.lower()
                return [clean_name for keyword, clean_name in self.entries if keyword in lowered]

        with patch.object(classifier_module, 'FlashTextKeywordProcessor', FakeKeywordProcessor):
            matcher = PhraseMatcher(('what do you recommend',))

        self.assertEqual(matcher.backend_name, 'flashtext')
        self.assertTrue(matcher.contains_any('what do you recommend for this task?'))


if __name__ == '__main__':
    unittest.main()
