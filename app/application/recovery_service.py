from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.adapters.logseq_adapter import LogseqAdapter, TaskRecord
from app.domain.enums import RuntimeStatus, TaskKeyword


@dataclass(slots=True)
class RecoveryOutcome:
    record: TaskRecord
    timed_out: bool
    retried: bool
    exhausted: bool


class RecoveryService:
    def __init__(
        self,
        logseq_adapter: LogseqAdapter,
        *,
        run_logs_dir: Path | str | None = None,
        lock_timeout_minutes: int = 15,
    ) -> None:
        self.logseq_adapter = logseq_adapter
        self.run_logs_dir = Path(run_logs_dir) if run_logs_dir is not None else None
        self.lock_timeout = timedelta(minutes=lock_timeout_minutes)

    def recover_if_timed_out(self, record: TaskRecord, *, now_iso: str) -> RecoveryOutcome:
        runtime_state = self._load_latest_runtime_state(record.task.task_id)
        locked_at_value = record.task.locked_at or runtime_state.get('locked_at')
        if not locked_at_value:
            return RecoveryOutcome(record=record, timed_out=False, retried=False, exhausted=False)

        locked_at = datetime.fromisoformat(locked_at_value)
        now = datetime.fromisoformat(now_iso)
        if now - locked_at <= self.lock_timeout:
            return RecoveryOutcome(record=record, timed_out=False, retried=False, exhausted=False)

        retry_count_base = int(runtime_state.get('retry_count', record.task.retry_count))
        max_retries = int(runtime_state.get('max_retries', record.task.max_retries))
        retry_count = retry_count_base + 1
        exhausted = retry_count >= max_retries
        updated = record
        if exhausted:
            updates = {
                'error_reason': 'lock_timeout',
                'failed_at': now_iso,
            }
            updated = self.logseq_adapter.update_block_properties(record, updates)
        updated = self.logseq_adapter.update_task_keyword(updated, TaskKeyword.TODO)
        updated.task.retry_count = retry_count
        updated.task.updated_at = now_iso
        updated.task.runtime_status = RuntimeStatus.FAILED if exhausted else RuntimeStatus.TIMEOUT
        self._persist_recovery_state(
            task_id=record.task.task_id,
            run_id=str(runtime_state.get('run_id') or record.task.run_id or self._build_recovery_run_id(record.task.task_id, now)),
            now_iso=now_iso,
            retry_count=retry_count,
            max_retries=max_retries,
            exhausted=exhausted,
        )
        return RecoveryOutcome(
            record=updated,
            timed_out=True,
            retried=not exhausted,
            exhausted=exhausted,
        )

    def _load_latest_runtime_state(self, task_id: str) -> dict[str, object]:
        if self.run_logs_dir is None:
            return {}
        target_dir = self.run_logs_dir / task_id
        if not target_dir.exists():
            return {}
        candidates = sorted(target_dir.glob('*.json'))
        if not candidates:
            return {}
        import json

        payload = json.loads(candidates[-1].read_text(encoding='utf-8'))
        payload['_path'] = str(candidates[-1])
        return payload

    def _persist_recovery_state(
        self,
        *,
        task_id: str,
        run_id: str,
        now_iso: str,
        retry_count: int,
        max_retries: int,
        exhausted: bool,
    ) -> None:
        if self.run_logs_dir is None:
            return
        target_dir = self.run_logs_dir / task_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f'{run_id}.json'
        import json

        payload: dict[str, object] = {}
        if target_path.exists():
            payload = json.loads(target_path.read_text(encoding='utf-8'))
        payload.update(
            {
                'task_id': task_id,
                'run_id': run_id,
                'retry_count': retry_count,
                'max_retries': max_retries,
                'runtime_status': RuntimeStatus.FAILED.value if exhausted else RuntimeStatus.TIMEOUT.value,
                'recovered_at': now_iso,
                'timeout_recovered': True,
                'writeback_status': payload.get('writeback_status', 'PENDING'),
            }
        )
        target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _build_recovery_run_id(self, task_id: str, now: datetime) -> str:
        return f'{task_id}-recovery-{now.strftime("%Y%m%d%H%M%S")}'
