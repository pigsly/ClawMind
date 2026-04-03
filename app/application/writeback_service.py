from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.adapters.logseq_adapter import LogseqAdapter, TaskRecord
from app.domain.models import ExecutionResult
from app.executors.deterministic_executor import (
    DeterministicExecutionOutcome,
    DeterministicExecutor,
)
from app.repositories.artifact_repository import ArtifactRepository


@dataclass(slots=True)
class WritebackOutcome:
    record: TaskRecord
    answer_page: Path | None
    runtime_artifact: Path | None
    appended_link: bool
    idempotent_replay: bool


class WritebackFailure(RuntimeError):
    def __init__(self, message: str, *, runtime_artifact: Path | None, result_status: str) -> None:
        super().__init__(message)
        self.runtime_artifact = runtime_artifact
        self.result_status = result_status


class WritebackService:
    def __init__(
        self,
        logseq_adapter: LogseqAdapter,
        *,
        runtime_artifacts_dir: Path | str,
    ) -> None:
        self.executor = DeterministicExecutor(logseq_adapter)
        self.artifact_repository = ArtifactRepository(runtime_artifacts_dir)

    def apply(
        self,
        record: TaskRecord,
        result: ExecutionResult,
        *,
        finished_at: str,
        idempotency_key: str,
    ) -> WritebackOutcome:
        runtime_artifact = self.artifact_repository.persist(
            task_id=record.task.task_id,
            run_id=record.task.run_id,
            result=result,
        )
        try:
            outcome: DeterministicExecutionOutcome = self.executor.apply_writeback(
                record,
                result,
                finished_at=finished_at,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise WritebackFailure(
                str(exc),
                runtime_artifact=runtime_artifact,
                result_status=result.result_status.value,
            ) from exc
        return WritebackOutcome(
            record=outcome.record,
            answer_page=outcome.answer_page,
            runtime_artifact=runtime_artifact,
            appended_link=outcome.appended_link,
            idempotent_replay=outcome.idempotent_replay,
        )

    def replay(
        self,
        record: TaskRecord,
        *,
        finished_at: str,
        run_id: str,
        idempotency_key: str,
    ) -> tuple[ExecutionResult, WritebackOutcome]:
        result = self.artifact_repository.load_result(task_id=record.task.task_id, run_id=run_id)
        outcome = self.apply(
            record,
            result,
            finished_at=finished_at,
            idempotency_key=idempotency_key,
        )
        return result, outcome

