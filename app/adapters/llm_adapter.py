from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.domain.models import ContextBundle, InstructionBundle


class LlmAdapter(ABC):
    @abstractmethod
    def complete_structured(
        self,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
    ) -> dict[str, Any]:
        raise NotImplementedError


class CodexCliExecutionError(RuntimeError):
    def __init__(self, message: str, *, diagnostic_payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostic_payload = diagnostic_payload


@dataclass(slots=True)
class CodexCommandOutcome:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    output_payload: dict[str, Any] | None
    completion_info: dict[str, Any] | None
    cleanup_info: dict[str, Any]


class CodexCliAdapter(LlmAdapter):
    def __init__(
        self,
        codex_cli_path: str = 'codex',
        *,
        working_dir: Path | str = '.',
        model: str | None = None,
        sandbox_mode: str = 'workspace-write',
        extra_args: list[str] | None = None,
        temp_root: Path | str | None = None,
        command_timeout_seconds: float | None = None,
    ) -> None:
        self.codex_cli_path = codex_cli_path
        self.working_dir = Path(working_dir)
        self.model = model
        self.sandbox_mode = sandbox_mode
        self.extra_args = extra_args or []
        self.temp_root = Path(temp_root) if temp_root is not None else self.working_dir / '.codex_tmp'
        self.command_timeout_seconds = command_timeout_seconds

    def complete_structured(
        self,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
    ) -> dict[str, Any]:
        no_data_missing_pattern = r'^(?:(?!\[Data missing\]).)*$'
        schema = {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'result_status': {'type': 'string'},
                'answer_type': {
                    'type': 'string',
                    'enum': ['DIRECT_ANSWER', 'BEST_EFFORT', 'HYPOTHESIS'],
                },
                'summary': {'type': 'string', 'minLength': 1, 'pattern': no_data_missing_pattern},
                'answer_paragraphs': {
                    'type': 'array',
                    'items': {'type': 'string', 'pattern': no_data_missing_pattern},
                },
                'uncertainty': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'additionalProperties': False,
                        'properties': {
                            'type': {'type': 'string', 'minLength': 1, 'pattern': no_data_missing_pattern},
                            'impact': {'type': 'string', 'minLength': 1, 'pattern': no_data_missing_pattern},
                            'description': {'type': 'string', 'minLength': 1, 'pattern': no_data_missing_pattern},
                        },
                        'required': ['type', 'impact', 'description'],
                    },
                },
                'artifact_content': {'type': ['string', 'null']},
                'artifact_type': {'type': 'string'},
                'target_file': {'type': ['string', 'null']},
                'links_to_append': {'type': 'array', 'items': {'type': 'string'}},
                'writeback_actions': {'type': 'array', 'items': {'type': 'string'}},
                'confidence': {'type': 'number', 'minimum': 0, 'maximum': 1},
                'assumptions': {
                    'type': 'array',
                    'items': {'type': 'string', 'pattern': no_data_missing_pattern},
                },
                'audit_log': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'tools_used': {'type': 'array', 'items': {'type': 'string'}},
                        'notes': {'type': ['string', 'null']},
                    },
                    'required': ['tools_used', 'notes'],
                },
            },
            'required': [
                'result_status',
                'answer_type',
                'summary',
                'answer_paragraphs',
                'uncertainty',
                'artifact_content',
                'artifact_type',
                'target_file',
                'links_to_append',
                'writeback_actions',
                'confidence',
                'assumptions',
                'audit_log',
            ],
        }
        prompt = self._build_prompt(context_bundle, instruction_bundle)
        temp_dir = self._create_temp_dir()
        cleanup_temp_dir = True
        try:
            schema_path = temp_dir / 'codex_output_schema.json'
            output_path = temp_dir / 'codex_last_message.json'
            prompt_path = temp_dir / 'codex_prompt.json'
            schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding='utf-8')
            prompt_path.write_text(prompt, encoding='utf-8')
            command = self._build_command(schema_path, output_path, model=instruction_bundle.model)
            completed = self._run_command(command, prompt, output_path)
            if completed.output_payload is not None:
                return self._finalize_output_payload(
                    completed.output_payload,
                    completion_info=completed.completion_info,
                    cleanup_info=completed.cleanup_info,
                )
            cleanup_temp_dir = False
            diagnostic_payload = self._build_diagnostic_payload(
                context_bundle=context_bundle,
                instruction_bundle=instruction_bundle,
                prompt=prompt,
                command=command,
                temp_dir=temp_dir,
                stdout=completed.stdout,
                stderr=completed.stderr,
                timed_out=completed.timed_out,
                completion_info=completed.completion_info,
                cleanup_info=completed.cleanup_info,
            )
            self._write_diagnostic_file(temp_dir, diagnostic_payload)
            if completed.timed_out:
                timeout_seconds = self.command_timeout_seconds
                raise CodexCliExecutionError(
                    f'Codex CLI execution timed out after {timeout_seconds} seconds',
                    diagnostic_payload=diagnostic_payload,
                )
            if completed.returncode != 0:
                raise CodexCliExecutionError(
                    'Codex CLI execution failed: '
                    f'{completed.stderr.strip() or completed.stdout.strip() or completed.returncode}',
                    diagnostic_payload=diagnostic_payload,
                )
            raise CodexCliExecutionError(
                'Codex CLI did not write a ready output payload before exiting.',
                diagnostic_payload=diagnostic_payload,
            )
        except CodexCliExecutionError:
            cleanup_temp_dir = False
            raise
        finally:
            if cleanup_temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _create_temp_dir(self) -> Path:
        self.temp_root.mkdir(parents=True, exist_ok=True)
        temp_dir = self.temp_root / uuid.uuid4().hex
        temp_dir.mkdir(parents=True, exist_ok=False)
        return temp_dir

    def _build_command(self, schema_path: Path, output_path: Path, *, model: str | None = None) -> list[str]:
        command = [
            self.codex_cli_path,
            'exec',
            '--skip-git-repo-check',
            '--sandbox',
            self.sandbox_mode,
            '--output-schema',
            str(schema_path),
            '--output-last-message',
            str(output_path),
        ]
        selected_model = model or self.model
        if selected_model:
            command.extend(['--model', selected_model])
        command.extend(self.extra_args)
        command.append('-')
        return command

    def _run_command(
        self,
        command: list[str],
        prompt: str,
        output_path: Path,
    ) -> CodexCommandOutcome:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.working_dir),
            env=os.environ.copy(),
        )
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_thread = self._start_stream_reader(process.stdout, stdout_buffer)
        stderr_thread = self._start_stream_reader(process.stderr, stderr_buffer)
        timed_out = False
        completion_info: dict[str, Any] | None = None
        output_payload: dict[str, Any] | None = None
        try:
            self._write_prompt(process, prompt)
            started = time.monotonic()
            while True:
                output_payload = self._try_load_output_payload(output_path)
                if output_payload is not None:
                    completion_info = {
                        'completion_status': 'completed_by_output',
                        'completion_source': 'output_file',
                        'completed_at': self._now_iso(),
                    }
                    break
                if process.poll() is not None:
                    break
                if (
                    self.command_timeout_seconds is not None
                    and (time.monotonic() - started) >= self.command_timeout_seconds
                ):
                    timed_out = True
                    break
                time.sleep(0.1)
            cleanup_info = self._cleanup_process(
                process,
                completion_established=completion_info is not None,
                timed_out=timed_out,
            )
            stdout, stderr = self._collect_streams(
                stdout_thread,
                stderr_thread,
                stdout_buffer,
                stderr_buffer,
            )
            if output_payload is None:
                output_payload = self._try_load_output_payload(output_path)
                if output_payload is not None and completion_info is None:
                    completion_info = {
                        'completion_status': 'completed_by_output',
                        'completion_source': 'output_file',
                        'completed_at': self._now_iso(),
                    }
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
        return CodexCommandOutcome(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_payload=output_payload,
            completion_info=completion_info,
            cleanup_info=cleanup_info,
        )

    def _build_diagnostic_payload(
        self,
        *,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
        prompt: str,
        command: list[str],
        temp_dir: Path,
        stdout: str,
        stderr: str,
        timed_out: bool,
        completion_info: dict[str, Any] | None = None,
        cleanup_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            'adapter': 'CodexCliAdapter',
            'timed_out': timed_out,
            'working_dir': str(self.working_dir),
            'temp_dir': str(temp_dir),
            'command': command,
            'model': instruction_bundle.model or self.model,
            'analysis_mode': instruction_bundle.analysis_mode.value,
            'executor_type': instruction_bundle.executor_type.value,
            'prompt_chars': len(prompt),
            'prompt_preview': prompt[:500],
            'context_stats': {
                'pages_count': len(context_bundle.pages),
                'memory_count': len(context_bundle.memory),
                'adr_count': len(context_bundle.adr),
                'skill_context_count': len(context_bundle.skill_context),
            },
            'stdout_excerpt': stdout[-1000:],
            'stderr_excerpt': stderr[-1000:],
            'completion_info': completion_info,
            'cleanup_info': cleanup_info,
            'task_id': context_bundle.task.task_id,
            'run_id': context_bundle.task.run_id,
        }

    def _write_diagnostic_file(self, temp_dir: Path, diagnostic_payload: dict[str, Any]) -> None:
        diagnostic_path = temp_dir / 'codex_diagnostic.json'
        diagnostic_path.write_text(json.dumps(diagnostic_payload, ensure_ascii=False, indent=2), encoding='utf-8')

    def _decode_output(self, value: bytes | str | None) -> str:
        if value is None:
            return ''
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='replace')
        return value

    def _start_stream_reader(self, stream: Any, buffer: bytearray) -> threading.Thread:
        def consume() -> None:
            if stream is None:
                return
            chunk = stream.read()
            if chunk:
                buffer.extend(chunk)

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        return thread

    def _write_prompt(self, process: subprocess.Popen[bytes], prompt: str) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(prompt.encode('utf-8'))
            process.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            process.stdin.close()

    def _collect_streams(
        self,
        stdout_thread: threading.Thread,
        stderr_thread: threading.Thread,
        stdout_buffer: bytearray,
        stderr_buffer: bytearray,
    ) -> tuple[str, str]:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        return self._decode_output(bytes(stdout_buffer)), self._decode_output(bytes(stderr_buffer))

    def _cleanup_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        completion_established: bool,
        timed_out: bool,
    ) -> dict[str, Any]:
        cleanup_info: dict[str, Any] = {
            'cleanup_grace_seconds': 5,
            'terminated': False,
            'killed': False,
            'returncode': process.poll(),
        }
        if process.poll() is None:
            cleanup_info['terminated'] = True
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_info['killed'] = True
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        cleanup_info['returncode'] = process.returncode
        if completion_established:
            if timed_out:
                cleanup_info['cleanup_status'] = (
                    'cleanup_forced' if cleanup_info['killed'] else 'timed_out_after_completion'
                )
            elif cleanup_info['terminated']:
                cleanup_info['cleanup_status'] = (
                    'cleanup_forced_after_completion'
                    if cleanup_info['killed']
                    else 'terminated_after_completion'
                )
            else:
                cleanup_info['cleanup_status'] = 'process_exited_after_completion'
        else:
            if timed_out:
                cleanup_info['cleanup_status'] = 'timed_out_before_completion'
            elif cleanup_info['terminated']:
                cleanup_info['cleanup_status'] = 'terminated_before_completion'
            else:
                cleanup_info['cleanup_status'] = 'process_exited_before_completion'
        return cleanup_info

    def _try_load_output_payload(self, output_path: Path) -> dict[str, Any] | None:
        if not output_path.exists():
            return None
        raw_text = output_path.read_text(encoding='utf-8').strip()
        if not raw_text:
            return None
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        if not self._is_ready_payload(payload):
            return None
        return payload

    def _is_ready_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        required_keys = {
            'result_status',
            'answer_type',
            'summary',
            'answer_paragraphs',
            'uncertainty',
            'artifact_content',
            'artifact_type',
            'target_file',
            'links_to_append',
            'writeback_actions',
            'confidence',
            'assumptions',
            'audit_log',
        }
        if not required_keys.issubset(payload.keys()):
            return False
        if payload.get('answer_type') not in {'DIRECT_ANSWER', 'BEST_EFFORT', 'HYPOTHESIS'}:
            return False
        try:
            confidence = float(payload.get('confidence'))
        except (TypeError, ValueError):
            return False
        if confidence < 0 or confidence > 1:
            return False
        if confidence < 0.5 and payload.get('answer_type') != 'HYPOTHESIS':
            return False
        if self._contains_forbidden_tag(payload.get('summary')):
            return False
        answer_paragraphs = payload.get('answer_paragraphs')
        if not isinstance(answer_paragraphs, list) or any(self._contains_forbidden_tag(item) for item in answer_paragraphs):
            return False
        assumptions = payload.get('assumptions')
        if not isinstance(assumptions, list) or any(self._contains_forbidden_tag(item) for item in assumptions):
            return False
        uncertainty = payload.get('uncertainty')
        if not isinstance(uncertainty, list):
            return False
        for item in uncertainty:
            if not isinstance(item, dict):
                return False
            if not {'type', 'impact', 'description'}.issubset(item.keys()):
                return False
            if any(self._contains_forbidden_tag(item.get(key)) for key in ('type', 'impact', 'description')):
                return False
        audit_log = payload.get('audit_log')
        if not isinstance(audit_log, dict):
            return False
        return {'tools_used', 'notes'}.issubset(audit_log.keys())

    def _contains_forbidden_tag(self, value: Any) -> bool:
        return '[Data missing]' in str(value or '')

    def _finalize_output_payload(
        self,
        payload: dict[str, Any],
        *,
        completion_info: dict[str, Any] | None,
        cleanup_info: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(payload)
        audit_log = dict(result.get('audit_log') or {})
        if completion_info is not None:
            audit_log['adapter_completion'] = completion_info
        audit_log['adapter_cleanup'] = cleanup_info
        result['audit_log'] = audit_log
        return result

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _build_prompt(
        self,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
    ) -> str:
        payload = {
            'task': context_bundle.task.to_dict(),
            'pages': context_bundle.pages,
            'memory': context_bundle.memory,
            'adr': context_bundle.adr,
            'skill_context': context_bundle.skill_context,
            'context_options': context_bundle.context_options.to_dict(),
            'instruction_bundle': instruction_bundle.to_dict(),
        }
        return (
            'You are the CodexRunner for a Logseq Q&A system. '
            'Return only a JSON object matching the provided schema. '
            'You must always provide a usable answer and never refuse due to missing information. '
            'Separate answer, uncertainty, and assumptions clearly. '
            'Do not write markdown files directly. '
            'Do not output [Data missing]. '
            'Do not mix uncertainty into answer_paragraphs. '
            'Provide answer_type as DIRECT_ANSWER, BEST_EFFORT, or HYPOTHESIS. '
            'Put the one-line conclusion into summary and the explanation into answer_paragraphs. '
            'Use the uncertainty field for structured gaps only. '
            'If confidence is below 0.5, answer_type must be HYPOTHESIS. '
            'Set artifact_content to null when summary and answer_paragraphs are present. '
            'Set audit_log.notes to null if there is nothing extra to report.\n\n'
            f'{json.dumps(payload, ensure_ascii=False, indent=2)}'
        )
