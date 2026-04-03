from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.logseq_adapter import TaskRecord
from app.application.writeback_service import WritebackOutcome
from app.domain.models import ExecutionResult, InstructionBundle


class AuditService:
    def __init__(self, run_logs_dir: Path | str) -> None:
        self.run_logs_dir = Path(run_logs_dir)

    def try_acquire_claim(
        self,
        *,
        record: TaskRecord,
        run_id: str,
        lock_owner: str | None,
        locked_at: str | None,
    ) -> bool:
        target_dir = self.run_logs_dir / record.task.task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        claim_path = target_dir / 'active_claim.json'
        payload = {
            'task_id': record.task.task_id,
            'run_id': run_id,
            'lock_owner': lock_owner,
            'locked_at': locked_at,
        }
        try:
            with claim_path.open('x', encoding='utf-8') as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except FileExistsError:
            return False
        return True

    def release_claim(self, task_id: str) -> None:
        claim_path = self.run_logs_dir / task_id / 'active_claim.json'
        claim_path.unlink(missing_ok=True)

    def start_run(
        self,
        *,
        record: TaskRecord,
        started_at: str,
        run_id: str,
        idempotency_key: str,
    ) -> Path:
        target_dir = self.run_logs_dir / record.task.task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        audit_path = target_dir / f'{run_id}.json'
        payload = {
            'task_id': record.task.task_id,
            'run_id': run_id,
            'idempotency_key': idempotency_key,
            'runtime_status': record.task.runtime_status.value,
            'task_keyword': record.task.task_keyword.value,
            'locked_at': record.task.locked_at,
            'lock_owner': record.task.lock_owner,
            'retry_count': record.task.retry_count,
            'max_retries': record.task.max_retries,
            'started_at': started_at,
            'finished_at': None,
            'writeback_status': 'PENDING',
            'answer_page': None,
            'runtime_artifact': None,
            'duplicate_run_detected': False,
            'task_snapshot': record.task.to_dict(),
        }
        audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return audit_path

    def mark_writeback_failed(
        self,
        *,
        record: TaskRecord,
        run_id: str,
        finished_at: str,
        runtime_artifact: Path | None,
        error_message: str,
        result_status: str,
    ) -> Path:
        return self.update_runtime_record(
            record.task.task_id,
            run_id,
            {
                'finished_at': finished_at,
                'runtime_status': record.task.runtime_status.value,
                'task_keyword': record.task.task_keyword.value,
                'writeback_status': 'FAILED',
                'runtime_artifact': str(runtime_artifact) if runtime_artifact else None,
                'error_message': error_message,
                'result_status': result_status,
            },
        )

    def mark_task_failed(
        self,
        *,
        record: TaskRecord,
        run_id: str,
        finished_at: str,
        error_message: str,
        failed_flow: str,
        writeback_status: str,
        result_status: str = 'FAILED',
        failure_context: dict[str, Any] | None = None,
    ) -> Path:
        return self.update_runtime_record(
            record.task.task_id,
            run_id,
            {
                'finished_at': finished_at,
                'runtime_status': record.task.runtime_status.value,
                'task_keyword': record.task.task_keyword.value,
                'writeback_status': writeback_status,
                'error_message': error_message,
                'failed_flow': failed_flow,
                'result_status': result_status,
                'failure_context': failure_context,
            },
        )

    def write_log(
        self,
        *,
        locked: TaskRecord,
        instruction_bundle: InstructionBundle,
        execution_result: ExecutionResult,
        writeback: WritebackOutcome,
        started_at: str,
        finished_at: str,
        run_id: str,
        idempotency_key: str,
        context_evidence: dict[str, Any] | None = None,
        token_used: int = 0,
        tools_used: list[str] | None = None,
        error_message: str | None = None,
    ) -> Path:
        target_dir = self.run_logs_dir / locked.task.task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        duration_ms = int(
            (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000
        )
        payload = self._load_payload(target_dir / f'{run_id}.json')
        payload.update(
            {
                'task_id': locked.task.task_id,
                'run_id': run_id,
                'idempotency_key': idempotency_key,
                'executor_type': instruction_bundle.executor_type.value,
                'task_type': instruction_bundle.task_type.value,
                'analysis_mode': instruction_bundle.analysis_mode.value,
                'model': instruction_bundle.model,
                'started_at': started_at,
                'finished_at': finished_at,
                'duration_ms': duration_ms,
                'token_used': token_used,
                'tools_used': tools_used or execution_result.audit_log.get('tools_used', []),
                'result_status': execution_result.result_status.value,
                'runtime_status': writeback.record.task.runtime_status.value,
                'task_keyword': writeback.record.task.task_keyword.value,
                'writeback_status': 'COMPLETED',
                'answer_page': str(writeback.answer_page) if writeback.answer_page else None,
                'runtime_artifact': str(writeback.runtime_artifact) if writeback.runtime_artifact else None,
                'appended_link': writeback.appended_link,
                'idempotent_replay': writeback.idempotent_replay,
                'duplicate_run_detected': writeback.idempotent_replay,
                'error_message': error_message,
                'result_status': execution_result.result_status.value,
                'task_snapshot': locked.task.to_dict(),
                'instruction_bundle': instruction_bundle.to_dict(),
                'context_evidence': context_evidence,
                'execution_result': execution_result.to_dict(),
            }
        )
        audit_path = target_dir / f'{run_id}.json'
        audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return audit_path

    def load_latest_runtime_record(self, task_id: str) -> dict[str, Any] | None:
        target_dir = self.run_logs_dir / task_id
        if not target_dir.exists():
            return None
        candidates = sorted(target_dir.glob('*.json'))
        if not candidates:
            return None
        payload = self._load_payload(candidates[-1])
        payload['_path'] = str(candidates[-1])
        return payload

    def update_runtime_record(self, task_id: str, run_id: str, updates: dict[str, Any]) -> Path:
        target_dir = self.run_logs_dir / task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        audit_path = target_dir / f'{run_id}.json'
        payload = self._load_payload(audit_path)
        payload.update(updates)
        audit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return audit_path

    def _load_payload(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding='utf-8'))





