from __future__ import annotations

import concurrent.futures
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


class StructuredCliAdapter(LlmAdapter):
    def __init__(
        self,
        *,
        working_dir: Path | str = '.',
        model: str | None = None,
        command_timeout_seconds: float | None = None,
        llm_brand: str,
    ) -> None:
        self.working_dir = Path(working_dir)
        self.model = model
        self.command_timeout_seconds = command_timeout_seconds
        self.llm_brand = llm_brand

    def _build_schema(self) -> dict[str, Any]:
        no_data_missing_pattern = r'^(?:(?!\[Data missing\]).)*$'
        return {
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
        model_name: str | None,
    ) -> dict[str, Any]:
        result = dict(payload)
        audit_log = dict(result.get('audit_log') or {})
        if completion_info is not None:
            audit_log['adapter_completion'] = completion_info
        audit_log['adapter_cleanup'] = cleanup_info
        audit_log['adapter_metadata'] = {
            'adapter': self.__class__.__name__,
            'llm_brand': self.llm_brand,
            'model': model_name,
        }
        result['audit_log'] = audit_log
        return result

    def _build_diagnostic_payload(
        self,
        *,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
        prompt: str,
        command: list[str] | None,
        stdout: str,
        stderr: str,
        timed_out: bool,
        completion_info: dict[str, Any] | None = None,
        cleanup_info: dict[str, Any] | None = None,
        temp_dir: Path | None = None,
        model_name: str | None = None,
        response_mime_type: str | None = None,
        schema_mode: str | None = None,
    ) -> dict[str, Any]:
        return {
            'adapter': self.__class__.__name__,
            'llm_brand': self.llm_brand,
            'timed_out': timed_out,
            'working_dir': str(self.working_dir),
            'temp_dir': str(temp_dir) if temp_dir is not None else None,
            'command': command,
            'model': model_name,
            'response_mime_type': response_mime_type,
            'schema_mode': schema_mode,
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
            'required_schema': self._build_schema(),
        }
        return (
            'You are the structured reasoning adapter for a Logseq Q&A system. '
            'Return only one JSON object that matches the required schema exactly. '
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
            'Set audit_log.notes to null if there is nothing extra to report. '
            'Answer the user task only. '
            'Do not inspect workspace files, repository files, or local project documentation unless the provided page links or explicit task context require it. '
            'If no linked pages or explicit file context are provided, do not use file-reading tools and do not infer that the question is about the current repository. '
            'Do not answer about ClawMind, Logseq, this repository, or the local workspace unless the user task explicitly asks about them.\n\n'
            f'{json.dumps(payload, ensure_ascii=False, indent=2)}'
        )


class CodexCliAdapter(StructuredCliAdapter):
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
        super().__init__(
            working_dir=working_dir,
            model=model,
            command_timeout_seconds=command_timeout_seconds,
            llm_brand='codex_cli',
        )
        self.codex_cli_path = codex_cli_path
        self.sandbox_mode = sandbox_mode
        self.extra_args = extra_args or []
        self.temp_root = Path(temp_root) if temp_root is not None else self.working_dir / '.codex_tmp'

    def complete_structured(
        self,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(context_bundle, instruction_bundle)
        temp_dir = self._create_temp_dir()
        cleanup_temp_dir = True
        selected_model = instruction_bundle.model or self.model
        try:
            schema_path = temp_dir / 'codex_output_schema.json'
            output_path = temp_dir / 'codex_last_message.json'
            prompt_path = temp_dir / 'codex_prompt.json'
            schema_path.write_text(json.dumps(self._build_schema(), ensure_ascii=False, indent=2), encoding='utf-8')
            prompt_path.write_text(prompt, encoding='utf-8')
            command = self._build_command(schema_path, output_path, model=selected_model)
            completed = self._run_command(command, prompt, output_path)
            if completed.output_payload is not None:
                return self._finalize_output_payload(
                    completed.output_payload,
                    completion_info=completed.completion_info,
                    cleanup_info=completed.cleanup_info,
                    model_name=selected_model,
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
                model_name=selected_model,
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
        if model:
            command.extend(['--model', model])
        command.extend(self.extra_args)
        command.append('-')
        return command

    def _run_command(
        self,
        command: list[str] | None,
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



class GeminiApiAdapter(StructuredCliAdapter):
    def __init__(
        self,
        api_key: str | None,
        *,
        flash_model: str = 'gemini-2.5-flash',
        pro_model: str = 'gemini-2.5-pro',
        working_dir: Path | str = '.',
        command_timeout_seconds: float | None = None,
        llm_brand: str = 'gemini_api',
        response_mime_type: str = 'application/json',
        temperature: float = 0.1,
    ) -> None:
        super().__init__(
            working_dir=working_dir,
            model=None,
            command_timeout_seconds=command_timeout_seconds,
            llm_brand=llm_brand,
        )
        if api_key is None or not str(api_key).strip():
            raise ValueError('GEMINI_API_KEY is required when LLM_BRAND=gemini_api')
        self.api_key = str(api_key).strip()
        self.flash_model = flash_model
        self.pro_model = pro_model
        self.response_mime_type = response_mime_type
        self.temperature = temperature

    def complete_structured(
        self,
        context_bundle: ContextBundle,
        instruction_bundle: InstructionBundle,
    ) -> dict[str, Any]:
        prompt = self._build_prompt(context_bundle, instruction_bundle)
        selected_model = self._select_model(instruction_bundle)
        schema = self._build_schema()
        fallback_from_model: str | None = None
        try:
            response = self._generate_content_with_timeout(prompt=prompt, model=selected_model, schema=schema)
        except TimeoutError:
            diagnostic_payload = self._build_diagnostic_payload(
                context_bundle=context_bundle,
                instruction_bundle=instruction_bundle,
                prompt=prompt,
                command=None,
                stdout='',
                stderr='',
                timed_out=True,
                completion_info=None,
                cleanup_info={'cleanup_status': 'timed_out_before_completion'},
                model_name=selected_model,
                response_mime_type=self.response_mime_type,
                schema_mode='response_json_schema',
            )
            raise CodexCliExecutionError(
                f'Gemini API execution timed out after {self.command_timeout_seconds} seconds',
                diagnostic_payload=diagnostic_payload,
            )
        except Exception as exc:
            if self._should_fallback_to_flash(exc, selected_model):
                fallback_from_model = selected_model
                selected_model = self.flash_model
                response = self._generate_content_with_timeout(prompt=prompt, model=selected_model, schema=schema)
            else:
                raise

        raw_text = getattr(response, 'text', '') or ''
        payload = self._extract_response_payload(response, raw_text)
        completion_info = None
        if payload is not None:
            completion_info = {
                'completion_status': 'completed_by_output',
                'completion_source': 'api_json',
                'completed_at': self._now_iso(),
            }
            if fallback_from_model is not None:
                completion_info['fallback_from_model'] = fallback_from_model
                completion_info['fallback_reason'] = 'quota_exhausted'
            return self._finalize_output_payload(
                payload,
                completion_info=completion_info,
                cleanup_info={'cleanup_status': 'api_call_completed'},
                model_name=selected_model,
            )

        diagnostic_payload = self._build_diagnostic_payload(
            context_bundle=context_bundle,
            instruction_bundle=instruction_bundle,
            prompt=prompt,
            command=None,
            stdout=raw_text,
            stderr='',
            timed_out=False,
            completion_info=completion_info,
            cleanup_info={'cleanup_status': 'api_call_completed'},
            model_name=selected_model,
            response_mime_type=self.response_mime_type,
            schema_mode='response_json_schema',
        )
        if fallback_from_model is not None:
            diagnostic_payload['fallback_from_model'] = fallback_from_model
            diagnostic_payload['fallback_reason'] = 'quota_exhausted'
        raise CodexCliExecutionError(
            'Gemini API did not return a ready structured payload.',
            diagnostic_payload=diagnostic_payload,
        )

    def _select_model(self, instruction_bundle: InstructionBundle) -> str:
        if instruction_bundle.analysis_mode.value == 'NORMAL':
            return self.flash_model
        return self.pro_model

    def _should_fallback_to_flash(self, exc: Exception, selected_model: str) -> bool:
        if selected_model != self.pro_model or self.flash_model == self.pro_model:
            return False
        message = str(exc).lower()
        return '429' in message and ('resource_exhausted' in message or 'quota exceeded' in message)

    def _generate_content_with_timeout(self, *, prompt: str, model: str, schema: dict[str, Any]) -> Any:
        if self.command_timeout_seconds is None:
            return self._generate_content(prompt=prompt, model=model, schema=schema)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self._generate_content, prompt=prompt, model=model, schema=schema)
            return future.result(timeout=self.command_timeout_seconds)

    def _generate_content(self, *, prompt: str, model: str, schema: dict[str, Any]) -> Any:
        from google import genai

        client = genai.Client(api_key=self.api_key)
        return client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                'response_mime_type': self.response_mime_type,
                'response_json_schema': schema,
                'temperature': self.temperature,
            },
        )

    def _extract_response_payload(self, response: Any, raw_text: str) -> dict[str, Any] | None:
        parsed = getattr(response, 'parsed', None)
        payload = self._extract_ready_payload_from_value(parsed)
        if payload is not None:
            return payload
        return self._extract_ready_payload_from_value(self._safe_json_loads(raw_text))

    def _extract_ready_payload_from_value(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict) and self._is_ready_payload(value):
            return value
        return None

    def _safe_json_loads(self, text: Any) -> Any:
        if not isinstance(text, str) or not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
