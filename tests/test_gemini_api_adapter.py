import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.adapters.llm_adapter import CodexCliExecutionError, GeminiApiAdapter
from app.domain.enums import AnalysisMode, ExecutorType, RuntimeStatus, TaskKeyword, TaskType
from app.domain.models import ContextBundle, InstructionBundle, Task


class GeminiApiAdapterTests(unittest.TestCase):
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

    def build_instruction(self, *, analysis_mode: AnalysisMode = AnalysisMode.REASONING_ANALYSIS) -> InstructionBundle:
        return InstructionBundle(
            task_type=TaskType.REASONING_ANALYSIS,
            analysis_mode=analysis_mode,
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
                {'type': 'scope', 'impact': 'low', 'description': '題目沒有明確限定範圍。'}
            ],
            'artifact_content': None,
            'artifact_type': 'MARKDOWN',
            'target_file': None,
            'links_to_append': [],
            'writeback_actions': ['write_answer_page'],
            'confidence': 0.8,
            'assumptions': ['test'],
            'audit_log': {'tools_used': ['gemini-api'], 'notes': None},
        }

    def test_requires_api_key(self) -> None:
        with self.assertRaisesRegex(ValueError, 'GEMINI_API_KEY is required'):
            GeminiApiAdapter(api_key=None)

    def test_selects_flash_for_normal_mode(self) -> None:
        adapter = GeminiApiAdapter(api_key='test-key')
        self.assertEqual(adapter._select_model(self.build_instruction(analysis_mode=AnalysisMode.NORMAL)), 'gemini-2.5-flash')

    def test_selects_pro_for_reasoning_mode(self) -> None:
        adapter = GeminiApiAdapter(api_key='test-key')
        self.assertEqual(adapter._select_model(self.build_instruction()), 'gemini-2.5-pro')

    def test_complete_structured_uses_native_json_mode_and_schema(self) -> None:
        adapter = GeminiApiAdapter(api_key='test-key', flash_model='gemini-2.5-flash', pro_model='gemini-2.5-pro')
        payload = self.build_payload()
        response = SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), parsed=payload)

        with patch.object(adapter, '_generate_content', return_value=response) as mock_generate:
            result = adapter.complete_structured(self.build_context(), self.build_instruction())

        kwargs = mock_generate.call_args.kwargs
        self.assertEqual(kwargs['model'], 'gemini-2.5-pro')
        self.assertIn('schema', kwargs)
        self.assertEqual(result['summary'], payload['summary'])
        self.assertEqual(result['audit_log']['adapter_completion']['completion_source'], 'api_json')
        self.assertEqual(result['audit_log']['adapter_metadata']['llm_brand'], 'gemini_api')
        self.assertEqual(result['audit_log']['adapter_metadata']['model'], 'gemini-2.5-pro')

    def test_generate_content_builds_expected_sdk_request(self) -> None:
        adapter = GeminiApiAdapter(api_key='test-key')
        fake_response = object()
        fake_models = SimpleNamespace(generate_content=lambda **kwargs: fake_response)
        fake_client = SimpleNamespace(models=fake_models)

        with patch('google.genai.Client', return_value=fake_client) as mock_client:
            response = adapter._generate_content(prompt='hello', model='gemini-2.5-flash', schema=adapter._build_schema())

        self.assertIs(response, fake_response)
        mock_client.assert_called_once_with(api_key='test-key')

    def test_falls_back_from_pro_to_flash_on_quota_exhausted(self) -> None:
        adapter = GeminiApiAdapter(api_key='test-key', flash_model='gemini-2.5-flash', pro_model='gemini-2.5-pro')
        payload = self.build_payload()
        response = SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), parsed=payload)

        with patch.object(
            adapter,
            '_generate_content_with_timeout',
            side_effect=[Exception('429 RESOURCE_EXHAUSTED quota exceeded for gemini-2.5-pro'), response],
        ) as mock_generate:
            result = adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertEqual(mock_generate.call_args_list[0].kwargs['model'], 'gemini-2.5-pro')
        self.assertEqual(mock_generate.call_args_list[1].kwargs['model'], 'gemini-2.5-flash')
        self.assertEqual(result['audit_log']['adapter_metadata']['model'], 'gemini-2.5-flash')
        self.assertEqual(result['audit_log']['adapter_completion']['fallback_from_model'], 'gemini-2.5-pro')
        self.assertEqual(result['audit_log']['adapter_completion']['fallback_reason'], 'quota_exhausted')

    def test_raises_when_response_is_not_ready_payload(self) -> None:
        adapter = GeminiApiAdapter(api_key='test-key')
        response = SimpleNamespace(text='{"summary":"bad"}', parsed={'summary': 'bad'})

        with patch.object(adapter, '_generate_content', return_value=response):
            with self.assertRaisesRegex(CodexCliExecutionError, 'did not return a ready structured payload') as ctx:
                adapter.complete_structured(self.build_context(), self.build_instruction())

        self.assertEqual(ctx.exception.diagnostic_payload['llm_brand'], 'gemini_api')
        self.assertEqual(ctx.exception.diagnostic_payload['response_mime_type'], 'application/json')
        self.assertEqual(ctx.exception.diagnostic_payload['schema_mode'], 'response_json_schema')

    def test_raises_on_timeout(self) -> None:
        adapter = GeminiApiAdapter(api_key='test-key', command_timeout_seconds=0.01)

        with patch.object(adapter, '_generate_content_with_timeout', side_effect=TimeoutError):
            with self.assertRaisesRegex(CodexCliExecutionError, 'timed out after 0.01 seconds'):
                adapter.complete_structured(self.build_context(), self.build_instruction())


if __name__ == '__main__':
    unittest.main()
