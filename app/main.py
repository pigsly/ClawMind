from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from hashlib import sha1
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Callable
import re

from app.adapters.llm_adapter import CodexCliAdapter, GeminiApiAdapter, LlmAdapter
from app.adapters.logseq_adapter import LogseqAdapter
from app.application.audit_service import AuditService
from app.application.classifier_service import ClassifierService
from app.application.context_builder import ContextBuilder
from app.application.recovery_service import RecoveryService
from app.application.runner_service import FlowEvent, RunnerOutcome, RunnerService, TaskFailure, WorkerOutcome
from app.application.writeback_service import WritebackService
from app.config import AppConfig
from app.domain.enums import ArtifactType, ExecutorType, ResultStatus
from app.domain.models import ContextBundle, ExecutionResult, InstructionBundle
from app.executors.codex_runner import CodexRunner

PACKAGE_NAME = 'clawmind'


@dataclass(slots=True)
class InstallInfo:
    package_name: str
    package_version: str
    executable_path: str
    python_path: str
    install_method: str
    install_hint: str


def _build_short_task_id(task_id: str) -> str:
    compact = ''.join(char for char in task_id.lower() if char.isalnum())
    if compact:
        return compact[:8]
    return sha1(task_id.encode('utf-8')).hexdigest()[:8]


def _print_flow_event(event: FlowEvent) -> None:
    short_id = _build_short_task_id(event.task_id)
    print(
        'task_flow '
        f'short_id={short_id} '
        f'task_id={event.task_id} '
        f'run_id={event.run_id} '
        f'flow_name={event.flow_name} '
        f'duration_ms={event.duration_ms}'
    )


def _print_task_summary(outcome: RunnerOutcome) -> None:
    short_id = _build_short_task_id(outcome.task_id)
    print(
        'task_flow_summary '
        f'short_id={short_id} '
        f'task_id={outcome.task_id} '
        f'run_id={outcome.run_id} '
        f'result_status={outcome.result_status} '
        f'total_duration_ms={outcome.total_duration_ms}'
    )


def _print_task_failure(failure: TaskFailure) -> None:
    short_id = _build_short_task_id(failure.task_id)
    print(
        'task_flow_summary '
        f'short_id={short_id} '
        f'task_id={failure.task_id} '
        f'run_id={failure.run_id} '
        'result_status=FAILED '
        f'failed_flow={failure.failed_flow} '
        f'total_duration_ms={failure.total_duration_ms}'
    )
    print(
        'task_flow_failure '
        f'short_id={short_id} '
        f'failed_flow={failure.failed_flow} '
        f'error_message={failure.error_message}'
    )


def _read_local_project_version(root_dir: Path) -> str:
    pyproject_path = root_dir / 'pyproject.toml'
    if not pyproject_path.exists():
        return '[Data missing]'
    data = tomllib.loads(pyproject_path.read_text(encoding='utf-8'))
    return str(data.get('project', {}).get('version', '[Data missing]'))


def get_cli_version() -> str:
    try:
        return package_version(PACKAGE_NAME)
    except PackageNotFoundError:
        return _read_local_project_version(Path(__file__).resolve().parent.parent)


def detect_install_method(
    *,
    executable_path: str,
    python_path: str,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    environment = env or dict(os.environ)
    if environment.get('PIPX_HOME') or environment.get('PIPX_BIN_DIR'):
        return 'pipx', 'env:PIPX_HOME/PIPX_BIN_DIR'

    haystacks = [executable_path.lower(), python_path.lower()]
    if any('pipx' in value for value in haystacks):
        return 'pipx', 'path_contains:pipx'

    uv_markers = (
        '/uv/tools/',
        '\\uv\\tools\\',
        '/.local/share/uv/',
        '\\.local\\share\\uv\\',
    )
    if any(marker in value for value in haystacks for marker in uv_markers):
        return 'uv', 'path_contains:uv_tools'

    return 'pip', 'fallback:python_env'


def build_install_info(
    *,
    executable_path: str | None = None,
    python_path: str | None = None,
    env: dict[str, str] | None = None,
) -> InstallInfo:
    resolved_executable = executable_path or sys.argv[0] or PACKAGE_NAME
    resolved_python = python_path or sys.executable
    install_method, install_hint = detect_install_method(
        executable_path=resolved_executable,
        python_path=resolved_python,
        env=env,
    )
    return InstallInfo(
        package_name=PACKAGE_NAME,
        package_version=get_cli_version(),
        executable_path=resolved_executable,
        python_path=resolved_python,
        install_method=install_method,
        install_hint=install_hint,
    )


def print_version() -> int:
    print(f'{PACKAGE_NAME} {get_cli_version()}')
    return 0


def print_install_info() -> int:
    info = build_install_info()
    print(f'package_name={info.package_name}')
    print(f'package_version={info.package_version}')
    print(f'executable_path={info.executable_path}')
    print(f'python_path={info.python_path}')
    print(f'install_method={info.install_method}')
    print(f'install_hint={info.install_hint}')
    return 0


def _resolve_upgrade_command(method: str) -> list[str]:
    if method == 'pipx':
        return ['pipx', 'upgrade', PACKAGE_NAME]
    if method == 'uv':
        return ['uv', 'tool', 'upgrade', PACKAGE_NAME, '--reinstall']
    return [sys.executable, '-m', 'pip', 'install', '-U', PACKAGE_NAME]


def _parse_tasklist_pids(stdout: str) -> list[int]:
    pids: list[int] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith('"'):
            continue
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) < 2:
            continue
        try:
            pids.append(int(parts[1]))
        except ValueError:
            continue
    return pids


def _list_running_clawmind_pids(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[int]:
    if os.name != 'nt':
        return []
    completed = runner(
        ['tasklist', '/FI', f'IMAGENAME eq {PACKAGE_NAME}.exe', '/FO', 'CSV', '/NH'],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return _parse_tasklist_pids(completed.stdout)


def _stop_running_clawmind_processes(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    current_pid: int | None = None,
) -> list[int]:
    stopped_pids: list[int] = []
    for pid in _list_running_clawmind_pids(runner=runner):
        if current_pid is not None and pid == current_pid:
            continue
        completed = runner(
            ['taskkill', '/PID', str(pid), '/F', '/T'],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            stopped_pids.append(pid)
    return stopped_pids


def _is_windows_entrypoint_self_upgrade(executable_path: str | None = None) -> bool:
    if os.name != 'nt':
        return False
    candidate = Path(executable_path or sys.argv[0] or '')
    return candidate.name.lower() == f'{PACKAGE_NAME}.exe'


def _quote_powershell_literal(value: str) -> str:
    return value.replace("'", "''")


def _build_deferred_upgrade_helper_command(command: list[str], *, current_pid: int) -> list[str]:
    quoted_command = ', '.join(f"'{_quote_powershell_literal(part)}'" for part in command)
    helper_script = (
        f"$command = @({quoted_command}); "
        f"$waitPid = {current_pid}; "
        "while (Get-Process -Id $waitPid -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 200 }; "
        "if ($command.Length -gt 1) { & $command[0] @($command[1..($command.Length - 1)]) } else { & $command[0] }; "
        "exit $LASTEXITCODE"
    )
    return ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', helper_script]


def _launch_deferred_upgrade(
    command: list[str],
    *,
    current_pid: int,
    launcher: Callable[..., object] = subprocess.Popen,
) -> object:
    creationflags = 0
    creationflags |= getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    creationflags |= getattr(subprocess, 'DETACHED_PROCESS', 0)
    helper_command = _build_deferred_upgrade_helper_command(command, current_pid=current_pid)
    return launcher(helper_command, creationflags=creationflags, close_fds=True)


def _emit_completed_output(completed: subprocess.CompletedProcess[str]) -> None:
    stdout = getattr(completed, 'stdout', None)
    stderr = getattr(completed, 'stderr', None)
    if stdout:
        print(stdout, end='' if stdout.endswith(('\n', '\r')) else '\n')
    if stderr:
        print(stderr, end='' if stderr.endswith(('\n', '\r')) else '\n')


def _is_uv_entrypoint_copy_false_failure(method: str, completed: subprocess.CompletedProcess[str]) -> bool:
    if method != 'uv' or completed.returncode == 0:
        return False
    stdout = completed.stdout or ''
    stderr = completed.stderr or ''
    combined = f'{stdout}\n{stderr}'
    return (
        bool(re.search(rf'Updated\s+{PACKAGE_NAME}\s+v[^\s]+\s+->\s+v[^\s]+', combined))
        and 'Failed to install entrypoint' in combined
        and 'failed to copy file' in combined
        and 'os error 32' in combined
    )


def run_upgrade(
    *,
    method: str,
    stop_running: bool = True,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    launcher: Callable[..., object] = subprocess.Popen,
    current_pid: int | None = None,
    executable_path: str | None = None,
) -> int:
    resolved_method = method
    if method == 'auto':
        resolved_method = build_install_info().install_method

    command = _resolve_upgrade_command(resolved_method)
    if resolved_method in {'pipx', 'uv'} and which(command[0]) is None:
        print(f'upgrade_method={resolved_method}')
        print(f'upgrade_command={" ".join(command)}')
        print(f'error={command[0]} not found in PATH')
        return 1

    active_pid = os.getpid() if current_pid is None else current_pid
    print(f'upgrade_method={resolved_method}')
    print(f'upgrade_command={" ".join(command)}')
    print(f'upgrade_stop_running={str(stop_running).lower()}')

    if stop_running:
        stopped_pids = _stop_running_clawmind_processes(runner=process_runner, current_pid=active_pid)
        print(
            'upgrade_stopped_pids='
            f'{",".join(str(pid) for pid in stopped_pids) if stopped_pids else "none"}'
        )

    if _is_windows_entrypoint_self_upgrade(executable_path=executable_path):
        try:
            _launch_deferred_upgrade(command, current_pid=active_pid, launcher=launcher)
        except OSError as exc:
            print('upgrade_status=FAILED')
            print(f'error=failed to launch deferred upgrade helper: {exc}')
            return 1
        print('upgrade_status=DEFERRED')
        print(f'upgrade_wait_pid={active_pid}')
        return 0

    completed = runner(command, check=False, capture_output=True, text=True)
    _emit_completed_output(completed)
    if _is_uv_entrypoint_copy_false_failure(resolved_method, completed):
        print('upgrade_status=SUCCESS_WITH_ENTRYPOINT_WARNING')
        print('upgrade_warning=package upgraded but entrypoint replacement was blocked by a Windows file lock')
        return 0
    return int(completed.returncode)


def build_executor(llm_adapter: LlmAdapter) -> Callable[[ContextBundle, InstructionBundle], ExecutionResult]:
    codex_runner = CodexRunner(llm_adapter)

    def executor(context_bundle: ContextBundle, instruction_bundle: InstructionBundle) -> ExecutionResult:
        if instruction_bundle.executor_type in {ExecutorType.CODEX, ExecutorType.MIXED}:
            return codex_runner.run(context_bundle, instruction_bundle)
        return ExecutionResult(
            result_status=ResultStatus.SUCCESS,
            artifact_content=(
                '# Answer\n\n'
                f'Deterministic writeback for {context_bundle.task.task_id}.\n'
            ),
            artifact_type=ArtifactType.MARKDOWN,
            target_file=None,
            links_to_append=[],
            writeback_actions=['write_answer_page', 'append_journal_link'],
            unresolved_items=[],
            confidence=0.95,
            assumptions=['deterministic demo path'],
            audit_log={'tools_used': ['demo-deterministic'], 'model': instruction_bundle.model},
        )

    return executor


def build_runner(config: AppConfig, *, logseq_dir: Path) -> RunnerService:
    adapter = LogseqAdapter(
        logseq_dir,
        journal_scan_days=config.journal_scan_days,
        default_max_retries=config.max_retries,
    )
    return RunnerService(
        logseq_adapter=adapter,
        classifier_service=ClassifierService(),
        context_builder=ContextBuilder(
            logseq_dir,
            run_logs_dir=config.run_logs_dir,
            runtime_artifacts_dir=config.runtime_artifacts_dir,
        ),
        writeback_service=WritebackService(
            adapter,
            runtime_artifacts_dir=config.runtime_artifacts_dir,
        ),
        audit_service=AuditService(config.run_logs_dir),
        recovery_service=RecoveryService(adapter, run_logs_dir=config.run_logs_dir),
        lock_owner='main-runner',
    )


def run_once(config: AppConfig, *, logseq_dir: Path, llm_adapter: LlmAdapter) -> int:
    runner = build_runner(config, logseq_dir=logseq_dir)
    print(f'config_source={config.config_source} env_path={config.env_path}')
    outcome = runner.run_once(build_executor(llm_adapter))
    if outcome is None:
        print('No DOING task found.')
        return 0

    print(f'task_id={outcome.task_id}')
    print(f'run_id={outcome.run_id}')
    print(f'result_status={outcome.result_status}')
    print(f'final_keyword={outcome.final_keyword}')
    if outcome.answer_page is not None:
        print(f'answer_page={outcome.answer_page}')
    print(f'audit_log={outcome.audit_log_path}')
    return 0


def run_worker(
    config: AppConfig,
    *,
    logseq_dir: Path,
    llm_adapter: LlmAdapter,
    max_tasks: int | None = None,
    poll_interval_seconds: float = 10.0,
    heartbeat_interval_seconds: float = 60.0,
) -> int:
    runner = build_runner(config, logseq_dir=logseq_dir)
    print(f'config_source={config.config_source} env_path={config.env_path}')
    print(
        'running_worker_status=RUNNING '
        f'poll_interval_seconds={poll_interval_seconds} '
        f'heartbeat_interval_seconds={heartbeat_interval_seconds} '
        f'max_tasks={max_tasks if max_tasks is not None else "unbounded"} '
        f'logseq_dir={logseq_dir}'
    )
    outcome: WorkerOutcome = runner.run_running_worker(
        build_executor(llm_adapter),
        max_tasks=max_tasks,
        poll_interval_seconds=poll_interval_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        idle_callback=lambda idle_cycles, interval: print(
            'running_worker_status=IDLE '
            f'idle_cycles={idle_cycles} '
            f'next_poll_in_seconds={interval}'
        ),
        heartbeat_callback=lambda idle_cycles, idle_seconds: print(
            'running_worker_status=HEARTBEAT '
            f'idle_cycles={idle_cycles} '
            f'idle_seconds={idle_seconds}'
        ),
        flow_callback=_print_flow_event,
        outcome_callback=_print_task_summary,
        failure_callback=_print_task_failure,
    )
    if outcome.interrupted:
        print('running_worker_status=STOPPING reason=keyboard_interrupt')
    print(f'processed_count={outcome.processed_count}')
    print(f'idle_cycles={outcome.idle_cycles}')
    print(
        'running_worker_status=STOPPED '
        f'reason={outcome.stop_reason or "completed"} '
        f'processed_count={outcome.processed_count}'
    )
    return 1 if outcome.failure is not None else 0


def _add_runtime_options(parser: argparse.ArgumentParser, *, include_worker_options: bool) -> None:
    try:
        config = AppConfig()
        logseq_default = str(config.logseq_dir)
        codex_default = config.codex_cli_path
        timeout_default = config.codex_timeout_seconds
    except ValueError:
        logseq_default = './logseq'
        codex_default = 'codex'
        timeout_default = None
    parser.add_argument(
        '--logseq-dir',
        default=logseq_default,
        help='Logseq graph root to use for this run',
    )
    parser.add_argument(
        '--codex-cli-path',
        default=codex_default,
        help='Path to the Codex CLI executable',
    )
    parser.add_argument(
        '--codex-timeout-seconds',
        type=float,
        default=timeout_default,
        help='Timeout in seconds for each codex exec call',
    )
    if include_worker_options:
        parser.add_argument(
            '--max-tasks',
            type=int,
            default=None,
            help='Maximum number of tasks to process in worker mode',
        )
        parser.add_argument(
            '--poll-interval',
            type=float,
            default=10.0,
            help='Polling interval in seconds for the running worker',
        )
        parser.add_argument(
            '--heartbeat-interval',
            type=float,
            default=60.0,
            help='Heartbeat interval in seconds for the running worker',
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='ClawMind runner entrypoint')
    subparsers = parser.add_subparsers(dest='command')

    _add_runtime_options(subparsers.add_parser('run-once'), include_worker_options=False)
    _add_runtime_options(subparsers.add_parser('run-worker'), include_worker_options=True)
    subparsers.add_parser('version')
    subparsers.add_parser('install-info')
    upgrade_parser = subparsers.add_parser('upgrade')
    upgrade_parser.add_argument('--method', default='auto', choices=['auto', 'pipx', 'uv', 'pip'])
    upgrade_parser.add_argument(
        '--stop-running',
        action=argparse.BooleanOptionalAction,
        default=True,
        help='Stop other running clawmind processes before upgrading',
    )
    return parser


def build_llm_adapter(config: AppConfig, args: argparse.Namespace) -> LlmAdapter:
    llm_brand = getattr(config, 'llm_brand', 'codex_cli')
    if llm_brand == 'codex_cli':
        return CodexCliAdapter(
            codex_cli_path=args.codex_cli_path,
            working_dir=config.root_dir,
            extra_args=['--full-auto'],
            command_timeout_seconds=args.codex_timeout_seconds,
        )
    if llm_brand == 'gemini_api':
        return GeminiApiAdapter(
            api_key=config.gemini_api_key,
            flash_model=config.gemini_flash_model,
            pro_model=config.gemini_pro_model,
            working_dir=config.root_dir,
            command_timeout_seconds=args.codex_timeout_seconds,
            llm_brand=llm_brand,
        )
    raise ValueError(f'Unsupported llm_brand: {llm_brand}')

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or 'run-once'

    if command == 'version':
        return print_version()
    if command == 'install-info':
        return print_install_info()
    if command == 'upgrade':
        return run_upgrade(method=args.method, stop_running=args.stop_running)

    config = AppConfig()
    llm_adapter = build_llm_adapter(config, args)

    if command == 'run-once':
        return run_once(config, logseq_dir=Path(args.logseq_dir), llm_adapter=llm_adapter)
    if command == 'run-worker':
        return run_worker(
            config,
            logseq_dir=Path(args.logseq_dir),
            llm_adapter=llm_adapter,
            max_tasks=args.max_tasks,
            poll_interval_seconds=args.poll_interval,
            heartbeat_interval_seconds=args.heartbeat_interval,
        )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())





