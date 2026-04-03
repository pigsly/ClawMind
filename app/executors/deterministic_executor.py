from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.adapters.logseq_adapter import LogseqAdapter, TaskRecord
from app.domain.enums import ResultStatus, RuntimeStatus
from app.domain.models import ExecutionResult


@dataclass(slots=True)
class DeterministicExecutionOutcome:
    record: TaskRecord
    answer_page: Path | None
    appended_link: bool
    idempotent_replay: bool


class DeterministicExecutor:
    def __init__(self, logseq_adapter: LogseqAdapter) -> None:
        self.logseq_adapter = logseq_adapter

    def apply_writeback(
        self,
        record: TaskRecord,
        result: ExecutionResult,
        *,
        finished_at: str,
        idempotency_key: str,
    ) -> DeterministicExecutionOutcome:
        target = self.logseq_adapter.build_answer_page_path(record)
        answer_page: Path | None = target if target.exists() else None
        appended_link = False
        idempotent_replay = False

        if result.artifact_content:
            if answer_page is None:
                answer_page = self.logseq_adapter.write_answer_page(
                    record,
                    result.artifact_content,
                )
                appended_link = self.logseq_adapter.append_journal_link(record, target.stem)
            else:
                appended_link = self.logseq_adapter.append_journal_link(record, target.stem)
                idempotent_replay = not appended_link

        updated = record
        if result.result_status == ResultStatus.FAILED:
            updates = {
                'error_reason': ' | '.join(result.unresolved_items) if result.unresolved_items else 'executor_failed',
                'failed_at': finished_at,
            }
            updated = self.logseq_adapter.update_block_properties(record, updates)

        updated.task.runtime_status = (
            RuntimeStatus.FAILED if result.result_status == ResultStatus.FAILED else RuntimeStatus.SUCCEEDED
        )
        updated.task.updated_at = finished_at
        updated.task.idempotency_key = idempotency_key
        return DeterministicExecutionOutcome(
            record=updated,
            answer_page=answer_page,
            appended_link=appended_link,
            idempotent_replay=idempotent_replay,
        )

