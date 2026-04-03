from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.adapters.logseq_adapter import LogseqAdapter, TaskRecord
from app.application.audit_service import AuditService
from app.application.classifier_service import ClassifierService
from app.application.recovery_service import RecoveryService
from app.application.context_builder import ContextBuilder
from app.application.writeback_service import WritebackFailure, WritebackService
from app.domain.enums import RuntimeStatus, TaskKeyword
from app.domain.models import ContextBundle, ExecutionResult, InstructionBundle

ExecutorFn = Callable[[ContextBundle, InstructionBundle], ExecutionResult]
IdleCallback = Callable[[int, float], None]
HeartbeatCallback = Callable[[int, float], None]
FlowCallback = Callable[['FlowEvent'], None]
OutcomeCallback = Callable[['RunnerOutcome'], None]
FailureCallback = Callable[['TaskFailure'], None]
FLOW_NAMES = ('intake', 'dispatch', 'execute', 'writeback', 'statusback')


@dataclass(slots=True)
class RunnerOutcome:
    task_id: str
    run_id: str
    idempotency_key: str
    audit_log_path: Path
    answer_page: Path | None
    final_keyword: str
    executor_type: str
    result_status: str
    flow_timings: dict[str, int] = field(default_factory=dict)
    total_duration_ms: int = 0


@dataclass(slots=True)
class TaskFailure:
    task_id: str
    run_id: str
    failed_flow: str
    flow_timings: dict[str, int]
    total_duration_ms: int
    error_message: str


@dataclass(slots=True)
class FlowEvent:
    task_id: str
    run_id: str
    flow_name: str
    duration_ms: int


@dataclass(slots=True)
class WorkerOutcome:
    processed_count: int
    outcomes: list[RunnerOutcome]
    interrupted: bool = False
    stop_reason: str | None = None
    idle_cycles: int = 0
    failure: TaskFailure | None = None


@dataclass(slots=True)
class TaskExecutionError(RuntimeError):
    failure: TaskFailure

    def __init__(self, failure: TaskFailure) -> None:
        RuntimeError.__init__(self, failure.error_message)
        self.failure = failure


class RunnerService:
    def __init__(
        self,
        *,
        logseq_adapter: LogseqAdapter,
        classifier_service: ClassifierService,
        context_builder: ContextBuilder,
        writeback_service: WritebackService,
        audit_service: AuditService,
        recovery_service: RecoveryService | None = None,
        lock_owner: str = 'runner-service',
        timezone_name: str = 'Asia/Taipei',
    ) -> None:
        self.logseq_adapter = logseq_adapter
        self.classifier_service = classifier_service
        self.context_builder = context_builder
        self.writeback_service = writeback_service
        self.audit_service = audit_service
        self.recovery_service = recovery_service
        self.lock_owner = lock_owner
        self.timezone = self._resolve_timezone(timezone_name)

    def run_once(self, executor: ExecutorFn) -> RunnerOutcome | None:
        outcome, failure = self._run_once_internal(executor, capture_failure=False)
        if failure is not None:
            raise RuntimeError(failure.error_message)
        return outcome

    def run_worker(
        self,
        executor: ExecutorFn,
        *,
        max_tasks: int | None = None,
    ) -> WorkerOutcome:
        outcomes: list[RunnerOutcome] = []
        while max_tasks is None or len(outcomes) < max_tasks:
            outcome, failure = self._run_once_internal(executor, capture_failure=True)
            if failure is not None:
                return WorkerOutcome(
                    processed_count=len(outcomes),
                    outcomes=outcomes,
                    interrupted=False,
                    stop_reason='task_failed',
                    idle_cycles=0,
                    failure=failure,
                )
            if outcome is None:
                break
            outcomes.append(outcome)
        return WorkerOutcome(
            processed_count=len(outcomes),
            outcomes=outcomes,
            interrupted=False,
            stop_reason='max_tasks_reached' if max_tasks is not None and len(outcomes) >= max_tasks else 'queue_drained',
            idle_cycles=0,
        )

    def run_running_worker(
        self,
        executor: ExecutorFn,
        *,
        poll_interval_seconds: float = 10.0,
        heartbeat_interval_seconds: float | None = 60.0,
        max_tasks: int | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        idle_callback: IdleCallback | None = None,
        heartbeat_callback: HeartbeatCallback | None = None,
        flow_callback: FlowCallback | None = None,
        outcome_callback: OutcomeCallback | None = None,
        failure_callback: FailureCallback | None = None,
    ) -> WorkerOutcome:
        outcomes: list[RunnerOutcome] = []
        idle_cycles = 0
        consecutive_idle_cycles = 0
        sleep = sleep_fn or self._sleep

        try:
            while max_tasks is None or len(outcomes) < max_tasks:
                outcome, failure = self._run_once_internal(executor, capture_failure=True, flow_callback=flow_callback)
                if failure is not None:
                    if failure_callback is not None:
                        failure_callback(failure)
                    return WorkerOutcome(
                        processed_count=len(outcomes),
                        outcomes=outcomes,
                        interrupted=False,
                        stop_reason='task_failed',
                        idle_cycles=idle_cycles,
                        failure=failure,
                    )
                if outcome is None:
                    idle_cycles += 1
                    consecutive_idle_cycles += 1
                    if idle_callback is not None:
                        idle_callback(idle_cycles, poll_interval_seconds)
                    if (
                        heartbeat_callback is not None
                        and heartbeat_interval_seconds is not None
                        and consecutive_idle_cycles * poll_interval_seconds >= heartbeat_interval_seconds
                    ):
                        heartbeat_callback(idle_cycles, consecutive_idle_cycles * poll_interval_seconds)
                        consecutive_idle_cycles = 0
                    sleep(poll_interval_seconds)
                    continue
                outcomes.append(outcome)
                if outcome_callback is not None:
                    outcome_callback(outcome)
                consecutive_idle_cycles = 0
        except KeyboardInterrupt:
            return WorkerOutcome(
                processed_count=len(outcomes),
                outcomes=outcomes,
                interrupted=True,
                stop_reason='keyboard_interrupt',
                idle_cycles=idle_cycles,
            )

        return WorkerOutcome(
            processed_count=len(outcomes),
            outcomes=outcomes,
            interrupted=False,
            stop_reason='max_tasks_reached' if max_tasks is not None and len(outcomes) >= max_tasks else 'stopped',
            idle_cycles=idle_cycles,
        )

    def _run_once_internal(
        self,
        executor: ExecutorFn,
        *,
        capture_failure: bool,
        flow_callback: FlowCallback | None = None,
    ) -> tuple[RunnerOutcome | None, TaskFailure | None]:
        started_at = self._now()
        total_started = perf_counter()
        dispatch_record, flow_timings = self._next_dispatch_record(started_at, flow_callback=flow_callback)
        if dispatch_record is None:
            return None, None

        try:
            outcome = self._execute_record(
                dispatch_record,
                executor=executor,
                started_at=started_at,
                total_started=total_started,
                flow_timings=flow_timings,
                capture_failure=capture_failure,
                flow_callback=flow_callback,
            )
            return outcome, None
        except TaskExecutionError as exc:
            return None, exc.failure
        finally:
            self.audit_service.release_claim(dispatch_record.task.task_id)

    def _next_dispatch_record(
        self,
        started_at: str,
        *,
        flow_callback: FlowCallback | None = None,
    ) -> tuple[TaskRecord | None, dict[str, int]]:
        flow_timings = self._blank_flow_timings()

        dispatch_started = perf_counter()
        waiting_tasks = self.logseq_adapter.scan_waiting_tasks()
        for waiting_task in waiting_tasks:
            prepared = self._prepare_waiting_task(waiting_task, started_at)
            if prepared is not None:
                flow_timings['dispatch'] = self._elapsed_ms(dispatch_started)
                self._emit_flow_event(prepared, 'dispatch', flow_timings['dispatch'], flow_callback)
                return prepared, flow_timings

        intake_started = perf_counter()
        doing_tasks = self.logseq_adapter.scan_doing_tasks()
        for doing_task in doing_tasks:
            record = self.logseq_adapter.normalize_task_id(doing_task)
            run_id = self._build_run_id(record, started_at)
            idempotency_key = self._build_idempotency_key(record)
            locked = self.logseq_adapter.lock_task(
                record,
                lock_owner=self.lock_owner,
                locked_at=started_at,
                run_id=run_id,
                idempotency_key=idempotency_key,
            )
            if self.audit_service.try_acquire_claim(
                record=locked,
                run_id=run_id,
                lock_owner=locked.task.lock_owner,
                locked_at=locked.task.locked_at,
            ):
                flow_timings['intake'] = self._elapsed_ms(intake_started)
                self._emit_flow_event(locked, 'intake', flow_timings['intake'], flow_callback)
                return locked, flow_timings

        return None, flow_timings

    def _prepare_waiting_task(self, record: TaskRecord, started_at: str) -> TaskRecord | None:
        current = record
        if not current.task.task_id:
            current = self.logseq_adapter.normalize_task_id(current)

        current = self.logseq_adapter._parse_record(current.journal_path, current.line_index)
        if current.task.task_keyword.value != 'WAITING':
            raise ValueError('Task is no longer in WAITING state.')

        if self.recovery_service is not None:
            recovery = self.recovery_service.recover_if_timed_out(current, now_iso=started_at)
            if recovery.timed_out:
                self.audit_service.release_claim(current.task.task_id)
                return None
            current = recovery.record

        replay_runtime = self._load_failed_writeback_runtime(current.task.task_id)
        if replay_runtime is not None:
            current.task.run_id = str(replay_runtime['run_id'])
            current.task.idempotency_key = str(replay_runtime['idempotency_key'])
            current.task.locked_at = str(replay_runtime.get('locked_at') or started_at)
            current.task.lock_owner = str(replay_runtime.get('lock_owner') or self.lock_owner)
        else:
            current.task.run_id = self._build_run_id(current, started_at)
            current.task.idempotency_key = self._build_idempotency_key(current)
            current.task.locked_at = current.task.locked_at or started_at
            current.task.lock_owner = current.task.lock_owner or self.lock_owner
        current.task.runtime_status = RuntimeStatus.RUNNING
        current.task.updated_at = started_at
        if not self.audit_service.try_acquire_claim(
            record=current,
            run_id=current.task.run_id,
            lock_owner=current.task.lock_owner,
            locked_at=current.task.locked_at,
        ):
            return None
        return current

    def _execute_record(
        self,
        record: TaskRecord,
        *,
        executor: ExecutorFn,
        started_at: str,
        total_started: float,
        flow_timings: dict[str, int],
        capture_failure: bool,
        flow_callback: FlowCallback | None = None,
    ) -> RunnerOutcome:
        replay_runtime = self._load_failed_writeback_runtime(record.task.task_id)
        instruction_bundle = self.classifier_service.classify(record.task)
        context_evidence: dict[str, object] | None = None

        if replay_runtime is None:
            self.audit_service.start_run(
                record=record,
                started_at=started_at,
                run_id=record.task.run_id,
                idempotency_key=record.task.idempotency_key,
            )
            self._emit_flow_event(record, 'execute_start', 0, flow_callback)
            execute_started = perf_counter()
            try:
                context_bundle, context_evidence = self.context_builder.build_with_audit(record.task, instruction_bundle=instruction_bundle)
                execution_result = executor(context_bundle, instruction_bundle)
            except Exception as exc:
                flow_timings['execute'] = self._elapsed_ms(execute_started)
                self._emit_flow_event(record, 'execute', flow_timings['execute'], flow_callback)
                self._finalize_failure(
                    record,
                    failed_flow='execute',
                    error_message=str(exc),
                    finished_at=self._now(),
                    writeback_status='SKIPPED',
                    failure_context=self._extract_failure_context(exc),
                )
                if not capture_failure:
                    raise
                raise TaskExecutionError(
                    self._build_failure(record, flow_timings, total_started, 'execute', str(exc))
                ) from exc
            flow_timings['execute'] = self._elapsed_ms(execute_started)
            self._emit_flow_event(record, 'execute', flow_timings['execute'], flow_callback)
            replay_started_at = started_at

            writeback_started = perf_counter()
            try:
                finished_at = self._now()
                writeback = self.writeback_service.apply(
                    record,
                    execution_result,
                    finished_at=finished_at,
                    idempotency_key=record.task.idempotency_key,
                )
            except WritebackFailure as exc:
                flow_timings['writeback'] = self._elapsed_ms(writeback_started)
                self._emit_flow_event(record, 'writeback', flow_timings['writeback'], flow_callback)
                self.audit_service.mark_writeback_failed(
                    record=record,
                    run_id=record.task.run_id,
                    finished_at=self._now(),
                    runtime_artifact=exc.runtime_artifact,
                    error_message=str(exc),
                    result_status=exc.result_status,
                )
                if not capture_failure:
                    raise
                raise TaskExecutionError(
                    self._build_failure(record, flow_timings, total_started, 'writeback', str(exc))
                ) from exc
            flow_timings['writeback'] = self._elapsed_ms(writeback_started)
            self._emit_flow_event(record, 'writeback', flow_timings['writeback'], flow_callback)
        else:
            replay_started_at = str(replay_runtime.get('started_at') or started_at)
            writeback_started = perf_counter()
            try:
                execution_result, writeback = self.writeback_service.replay(
                    record,
                    finished_at=self._now(),
                    run_id=record.task.run_id,
                    idempotency_key=record.task.idempotency_key,
                )
            except Exception as exc:
                flow_timings['writeback'] = self._elapsed_ms(writeback_started)
                self._emit_flow_event(record, 'writeback', flow_timings['writeback'], flow_callback)
                if not capture_failure:
                    raise
                raise TaskExecutionError(
                    self._build_failure(record, flow_timings, total_started, 'writeback', str(exc))
                ) from exc
            flow_timings['execute'] = 0
            flow_timings['writeback'] = self._elapsed_ms(writeback_started)
            self._emit_flow_event(record, 'writeback', flow_timings['writeback'], flow_callback)
            finished_at = self._now()

        statusback_started = perf_counter()
        try:
            statusback_record = self._apply_statusback(writeback.record, execution_result, finished_at=finished_at)
            audit_log_path = self.audit_service.write_log(
                locked=record,
                instruction_bundle=instruction_bundle,
                execution_result=execution_result,
                writeback=writeback,
                started_at=replay_started_at,
                finished_at=finished_at,
                run_id=record.task.run_id,
                idempotency_key=record.task.idempotency_key,
                context_evidence=context_evidence if replay_runtime is None else None,
            )
        except Exception as exc:
            flow_timings['statusback'] = self._elapsed_ms(statusback_started)
            self._emit_flow_event(record, 'statusback', flow_timings['statusback'], flow_callback)
            self._finalize_failure(
                writeback.record,
                failed_flow='statusback',
                error_message=str(exc),
                finished_at=self._now(),
                writeback_status='COMPLETED',
                result_status=execution_result.result_status.value,
            )
            if not capture_failure:
                raise
            raise TaskExecutionError(
                self._build_failure(record, flow_timings, total_started, 'statusback', str(exc))
            ) from exc
        flow_timings['statusback'] = self._elapsed_ms(statusback_started)
        self._emit_flow_event(record, 'statusback', flow_timings['statusback'], flow_callback)

        return RunnerOutcome(
            task_id=statusback_record.task.task_id,
            run_id=statusback_record.task.run_id,
            idempotency_key=statusback_record.task.idempotency_key,
            audit_log_path=audit_log_path,
            answer_page=writeback.answer_page,
            final_keyword=statusback_record.task.task_keyword.value,
            executor_type=instruction_bundle.executor_type.value,
            result_status=execution_result.result_status.value,
            flow_timings=dict(flow_timings),
            total_duration_ms=self._elapsed_ms(total_started),
        )

    def _apply_statusback(
        self,
        record: TaskRecord,
        execution_result: ExecutionResult,
        *,
        finished_at: str,
    ) -> TaskRecord:
        updated = self.logseq_adapter.update_task_keyword(record, TaskKeyword.TODO)
        updated.task.run_id = record.task.run_id
        updated.task.idempotency_key = record.task.idempotency_key
        updated.task.locked_at = record.task.locked_at
        updated.task.lock_owner = record.task.lock_owner
        updated.task.runtime_status = (
            RuntimeStatus.FAILED if execution_result.result_status.value == 'FAILED' else RuntimeStatus.SUCCEEDED
        )
        updated.task.updated_at = finished_at
        return updated

    def _finalize_failure(
        self,
        record: TaskRecord,
        *,
        failed_flow: str,
        error_message: str,
        finished_at: str,
        writeback_status: str,
        result_status: str = 'FAILED',
        failure_context: dict[str, object] | None = None,
    ) -> TaskRecord:
        updated = record
        try:
            updated = self.logseq_adapter.update_block_properties(
                updated,
                {
                    'error_reason': error_message,
                    'failed_at': finished_at,
                },
            )
        except Exception:
            updated = record
        try:
            updated = self.logseq_adapter.update_task_keyword(updated, TaskKeyword.TODO)
        except Exception:
            pass
        updated.task.run_id = record.task.run_id
        updated.task.idempotency_key = record.task.idempotency_key
        updated.task.locked_at = record.task.locked_at
        updated.task.lock_owner = record.task.lock_owner
        updated.task.runtime_status = RuntimeStatus.FAILED
        updated.task.updated_at = finished_at
        self.audit_service.mark_task_failed(
            record=updated,
            run_id=record.task.run_id,
            finished_at=finished_at,
            error_message=error_message,
            failed_flow=failed_flow,
            writeback_status=writeback_status,
            result_status=result_status,
            failure_context=failure_context,
        )
        return updated

    def _extract_failure_context(self, exc: Exception) -> dict[str, object] | None:
        diagnostic_payload = getattr(exc, 'diagnostic_payload', None)
        if isinstance(diagnostic_payload, dict):
            return diagnostic_payload
        return None

    def _load_failed_writeback_runtime(self, task_id: str) -> dict[str, str] | None:
        runtime_record = self.audit_service.load_latest_runtime_record(task_id)
        if runtime_record is None:
            return None
        if runtime_record.get('writeback_status') != 'FAILED':
            return None
        if runtime_record.get('result_status') != 'SUCCESS':
            return None
        if not runtime_record.get('runtime_artifact'):
            return None
        if not runtime_record.get('run_id') or not runtime_record.get('idempotency_key'):
            return None
        return runtime_record

    def _build_run_id(self, record: TaskRecord, started_at: str) -> str:
        timestamp = datetime.fromisoformat(started_at).strftime('%Y%m%d%H%M%S')
        return f'{record.task.task_id}-{timestamp}'

    def _build_idempotency_key(self, record: TaskRecord) -> str:
        canonical_target = self.logseq_adapter.build_answer_page_relative_path(record)
        canonical_intent = (
            f'{record.task.task_id}|write_answer_page+append_journal_link|{canonical_target}'
        )
        digest = hashlib.sha256(canonical_intent.encode('utf-8')).hexdigest()
        return f'wb:{digest[:16]}'

    def _blank_flow_timings(self) -> dict[str, int]:
        return {name: 0 for name in FLOW_NAMES}

    def _emit_flow_event(
        self,
        record: TaskRecord,
        flow_name: str,
        duration_ms: int,
        callback: FlowCallback | None,
    ) -> None:
        if callback is None:
            return
        callback(
            FlowEvent(
                task_id=record.task.task_id,
                run_id=record.task.run_id,
                flow_name=flow_name,
                duration_ms=duration_ms,
            )
        )

    def _build_failure(
        self,
        record: TaskRecord,
        flow_timings: dict[str, int],
        total_started: float,
        failed_flow: str,
        error_message: str,
    ) -> TaskFailure:
        return TaskFailure(
            task_id=record.task.task_id,
            run_id=record.task.run_id,
            failed_flow=failed_flow,
            flow_timings=dict(flow_timings),
            total_duration_ms=self._elapsed_ms(total_started),
            error_message=error_message,
        )

    def _elapsed_ms(self, started_at: float) -> int:
        return int((perf_counter() - started_at) * 1000)

    def _now(self) -> str:
        return datetime.now(self.timezone).isoformat(timespec='seconds')

    def _resolve_timezone(self, timezone_name: str):
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            # Windows Python environments may not bundle IANA tzdata.
            if timezone_name == 'Asia/Taipei':
                return timezone(timedelta(hours=8), name='Asia/Taipei')
            return timezone.utc

    def _sleep(self, seconds: float) -> None:
        from time import sleep

        sleep(seconds)


