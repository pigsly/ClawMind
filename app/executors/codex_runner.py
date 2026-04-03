from __future__ import annotations

from typing import Any

from app.adapters.llm_adapter import LlmAdapter
from app.domain.enums import AnswerType, ArtifactType, ResultStatus
from app.domain.models import ContextBundle, ExecutionResult, InstructionBundle, UncertaintyItem


class CodexRunner:
    def __init__(self, llm_adapter: LlmAdapter) -> None:
        self.llm_adapter = llm_adapter

    def run(
        self,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
    ) -> ExecutionResult:
        raw_result = self.llm_adapter.complete_structured(context_bundle, instruction_bundle)
        return self._normalize_result(raw_result)

    def _normalize_result(self, raw_result: dict[str, Any]) -> ExecutionResult:
        result_status = self._normalize_result_status(raw_result.get('result_status'))
        artifact_type = self._normalize_artifact_type(raw_result.get('artifact_type'))
        assumptions = [
            self._sanitize_text(item)
            for item in raw_result.get('assumptions', [])
            if self._sanitize_text(item)
        ]
        audit_log = self._sanitize_audit_log(dict(raw_result.get('audit_log', {})))
        confidence = self._normalize_confidence(raw_result.get('confidence', 0.0))
        paragraphs = self._normalize_paragraphs(raw_result.get('answer_paragraphs', []))
        summary = self._normalize_summary(raw_result.get('summary'), paragraphs)
        uncertainty = self._normalize_uncertainty(raw_result.get('uncertainty', []))
        answer_type = self._normalize_answer_type(raw_result.get('answer_type'), confidence)

        artifact_content = self._build_markdown_content(
            answer_type=answer_type,
            summary=summary,
            paragraphs=paragraphs,
            assumptions=assumptions,
            uncertainty=uncertainty,
            confidence=confidence,
        )

        return ExecutionResult(
            result_status=result_status,
            artifact_content=artifact_content,
            artifact_type=artifact_type,
            target_file=raw_result.get('target_file'),
            links_to_append=[str(item) for item in raw_result.get('links_to_append', [])],
            writeback_actions=[str(item) for item in raw_result.get('writeback_actions', [])],
            unresolved_items=[],
            answer_type=answer_type,
            summary=summary,
            answer_paragraphs=paragraphs,
            uncertainty=uncertainty,
            confidence=confidence,
            assumptions=assumptions,
            audit_log=audit_log,
        )

    def _build_markdown_content(
        self,
        *,
        answer_type: AnswerType,
        summary: str,
        paragraphs: list[str],
        assumptions: list[str],
        uncertainty: list[UncertaintyItem],
        confidence: float,
    ) -> str:
        conclusion = summary
        if answer_type == AnswerType.HYPOTHESIS:
            conclusion = f'（假設）{summary}'

        lines = [
            '# Answer',
            '',
            'Conclusion:',
            conclusion,
            '',
            'Explanation:',
        ]
        if paragraphs:
            lines.extend(paragraphs)
        else:
            lines.append('根據目前資訊，這是最合理的初步判斷。')
        lines.extend(['', 'Assumptions:'])
        lines.extend([f'- {item}' for item in assumptions] or ['- 無'])
        lines.extend(['', 'Uncertainty:'])
        if uncertainty:
            lines.extend([f'- [{item.impact}/{item.type}] {item.description}' for item in uncertainty])
        else:
            lines.append('- 無')
        lines.extend(['', f'Confidence: {confidence:.2f}'])
        return '\n'.join(lines).strip()

    def _normalize_result_status(self, value: Any) -> ResultStatus:
        normalized = str(value or ResultStatus.OPEN_QUESTION.value).strip().upper()
        aliases = {
            'SUCCESS': ResultStatus.SUCCESS,
            'SUCCEEDED': ResultStatus.SUCCESS,
            'FAILED': ResultStatus.FAILED,
            'FAILURE': ResultStatus.FAILED,
            'PARTIAL': ResultStatus.PARTIAL,
            'OPEN_QUESTION': ResultStatus.OPEN_QUESTION,
            'OPEN QUESTION': ResultStatus.OPEN_QUESTION,
        }
        return aliases.get(normalized, ResultStatus.OPEN_QUESTION)

    def _normalize_artifact_type(self, value: Any) -> ArtifactType:
        normalized = str(value or ArtifactType.MARKDOWN.value).strip().upper()
        aliases = {
            'MARKDOWN': ArtifactType.MARKDOWN,
            'MD': ArtifactType.MARKDOWN,
            'JSON': ArtifactType.JSON,
            'PATCH': ArtifactType.PATCH,
            'TEXT': ArtifactType.TEXT,
            'NONE': ArtifactType.NONE,
        }
        return aliases.get(normalized, ArtifactType.TEXT)

    def _normalize_answer_type(self, value: Any, confidence: float) -> AnswerType:
        normalized = str(value or '').strip().upper()
        aliases = {
            'DIRECT_ANSWER': AnswerType.DIRECT_ANSWER,
            'BEST_EFFORT': AnswerType.BEST_EFFORT,
            'HYPOTHESIS': AnswerType.HYPOTHESIS,
        }
        answer_type = aliases.get(normalized)
        if confidence < 0.5:
            return AnswerType.HYPOTHESIS
        if answer_type is not None:
            return answer_type
        if confidence >= 0.8:
            return AnswerType.DIRECT_ANSWER
        return AnswerType.BEST_EFFORT

    def _normalize_confidence(self, value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = 0.0
        return max(0.0, min(1.0, confidence))

    def _normalize_summary(self, value: Any, paragraphs: list[str]) -> str:
        summary = self._sanitize_text(value)
        if summary:
            return summary
        if paragraphs:
            return paragraphs[0]
        return '根據目前可得資訊，這是最合理的初步結論。'

    def _normalize_paragraphs(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [self._sanitize_text(item) for item in value if self._sanitize_text(item)]

    def _normalize_uncertainty(self, value: Any) -> list[UncertaintyItem]:
        if not isinstance(value, list):
            return []
        items: list[UncertaintyItem] = []
        for raw_item in value:
            if isinstance(raw_item, dict):
                item = UncertaintyItem(
                    type=self._sanitize_text(raw_item.get('type')) or 'unspecified',
                    impact=self._sanitize_text(raw_item.get('impact')) or 'medium',
                    description=self._sanitize_text(raw_item.get('description')) or '需要進一步驗證。',
                )
                items.append(item)
            else:
                description = self._sanitize_text(raw_item)
                if description:
                    items.append(
                        UncertaintyItem(
                            type='unspecified',
                            impact='medium',
                            description=description,
                        )
                    )
        return items

    def _sanitize_audit_log(self, audit_log: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, value in audit_log.items():
            if isinstance(value, str):
                cleaned = self._sanitize_text(value)
                sanitized[str(key)] = cleaned
            elif isinstance(value, list):
                sanitized[str(key)] = [self._sanitize_text(item) for item in value if self._sanitize_text(item)]
            else:
                sanitized[str(key)] = value
        return sanitized

    def _sanitize_text(self, value: Any) -> str:
        text = str(value or '').replace('[Data missing]', '').strip()
        return ' '.join(text.split())

