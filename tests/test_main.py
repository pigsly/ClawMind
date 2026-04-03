from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from app.adapters.llm_adapter import LlmAdapter
from app.main import build_install_info, detect_install_method, main, run_upgrade, run_worker
from app.application.runner_service import FlowEvent as RunnerFlowEvent, RunnerOutcome, TaskFailure, WorkerOutcome


class FakeLlmAdapter(LlmAdapter):
    def complete_structured(self, context_bundle, instruction_bundle):
        return {
            'result_status': 'SUCCESS',
            'answer_type': 'DIRECT_ANSWER',
            'summary': 'stub',
            'answer_paragraphs': ['stub'],
            'uncertainty': [],
            'artifact_content': None,
            'artifact_type': 'MARKDOWN',
            'target_file': None,
            'links_to_append': [],
            'writeback_actions': ['write_answer_page'],
            'confidence': 1.0,
            'assumptions': ['test stub'],
            'audit_log': {'tools_used': ['fake-llm'], 'notes': None},
        }


class FakeRunner:
    def __init__(self, outcome: WorkerOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def run_running_worker(
        self,
        executor,
        *,
        poll_interval_seconds: float,
        heartbeat_interval_seconds: float,
        max_tasks: int | None = None,
        idle_callback=None,
        heartbeat_callback=None,
        flow_callback=None,
        outcome_callback=None,
        failure_callback=None,
    ) -> WorkerOutcome:
        self.calls.append(
            {
                'executor': executor,
                'poll_interval_seconds': poll_interval_seconds,
                'heartbeat_interval_seconds': heartbeat_interval_seconds,
                'max_tasks': max_tasks,
                'idle_callback': idle_callback,
                'heartbeat_callback': heartbeat_callback,
                'flow_callback': flow_callback,
                'outcome_callback': outcome_callback,
                'failure_callback': failure_callback,
            }
        )
        if idle_callback is not None:
            idle_callback(1, poll_interval_seconds)
        if heartbeat_callback is not None:
            heartbeat_callback(2, heartbeat_interval_seconds)
        if flow_callback is not None:
            for item in self.outcome.outcomes:
                for flow_name, duration_ms in item.flow_timings.items():
                    flow_callback(RunnerFlowEvent(item.task_id, item.run_id, flow_name, duration_ms))
            if self.outcome.failure is not None:
                for flow_name, duration_ms in self.outcome.failure.flow_timings.items():
                    flow_callback(RunnerFlowEvent(self.outcome.failure.task_id, self.outcome.failure.run_id, flow_name, duration_ms))
        if outcome_callback is not None:
            for item in self.outcome.outcomes:
                outcome_callback(item)
        if failure_callback is not None and self.outcome.failure is not None:
            failure_callback(self.outcome.failure)
        return self.outcome


class MainRunWorkerTests(unittest.TestCase):
    def test_run_worker_prints_running_idle_heartbeat_timing_and_stopped_messages(self) -> None:
        fake_runner = FakeRunner(
            WorkerOutcome(
                processed_count=1,
                outcomes=[
                    RunnerOutcome(
                        task_id='fe43d9ec-1750-47a0-9ee7-d7f61fbca49a',
                        run_id='run-001',
                        idempotency_key='wb:abc123',
                        audit_log_path=Path('D:/run_logs/run-001.json'),
                        answer_page=Path('D:/logseq/pages/answer/demo__fe43d9ec.md'),
                        final_keyword='TODO',
                        executor_type='CODEX',
                        result_status='SUCCESS',
                        flow_timings={
                            'intake': 12,
                            'dispatch': 0,
                            'execute_start': 0,
                            'execute': 230,
                            'writeback': 45,
                            'statusback': 10,
                        },
                        total_duration_ms=297,
                    )
                ],
                interrupted=True,
                stop_reason='keyboard_interrupt',
                idle_cycles=2,
            )
        )
        config = SimpleNamespace(config_source='project_root:.env', env_path=Path('D:/PY_REPO/ClawMind/.env'))

        with patch('app.main.build_runner', return_value=fake_runner):
            with patch('builtins.print') as mock_print:
                exit_code = run_worker(
                    config,
                    logseq_dir=Path('D:/logseq'),
                    llm_adapter=FakeLlmAdapter(),
                    max_tasks=None,
                    poll_interval_seconds=1.5,
                    heartbeat_interval_seconds=6.0,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(fake_runner.calls[0]['poll_interval_seconds'], 1.5)
        self.assertEqual(fake_runner.calls[0]['heartbeat_interval_seconds'], 6.0)
        self.assertIsNone(fake_runner.calls[0]['max_tasks'])
        self.assertIsNotNone(fake_runner.calls[0]['idle_callback'])
        self.assertIsNotNone(fake_runner.calls[0]['heartbeat_callback'])
        self.assertIsNotNone(fake_runner.calls[0]['flow_callback'])
        self.assertIsNotNone(fake_runner.calls[0]['outcome_callback'])
        self.assertIsNotNone(fake_runner.calls[0]['failure_callback'])
        printed_lines = [call.args[0] for call in mock_print.call_args_list]
        self.assertIn('config_source=project_root:.env env_path=D:\\PY_REPO\\ClawMind\\.env', printed_lines)
        self.assertIn(
            'running_worker_status=RUNNING poll_interval_seconds=1.5 heartbeat_interval_seconds=6.0 max_tasks=unbounded logseq_dir=D:\\logseq',
            printed_lines,
        )
        self.assertIn('running_worker_status=IDLE idle_cycles=1 next_poll_in_seconds=1.5', printed_lines)
        self.assertIn('running_worker_status=HEARTBEAT idle_cycles=2 idle_seconds=6.0', printed_lines)
        self.assertIn('task_flow_summary short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-001 result_status=SUCCESS total_duration_ms=297', printed_lines)
        self.assertIn('task_flow short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-001 flow_name=intake duration_ms=12', printed_lines)
        self.assertIn('task_flow short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-001 flow_name=dispatch duration_ms=0', printed_lines)
        self.assertIn('task_flow short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-001 flow_name=execute_start duration_ms=0', printed_lines)
        self.assertIn('task_flow short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-001 flow_name=execute duration_ms=230', printed_lines)
        self.assertIn('task_flow short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-001 flow_name=writeback duration_ms=45', printed_lines)
        self.assertIn('task_flow short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-001 flow_name=statusback duration_ms=10', printed_lines)
        self.assertIn('running_worker_status=STOPPING reason=keyboard_interrupt', printed_lines)
        self.assertIn('running_worker_status=STOPPED reason=keyboard_interrupt processed_count=1', printed_lines)
        self.assertIn('idle_cycles=2', printed_lines)

    def test_run_worker_prints_failed_flow_and_returns_nonzero(self) -> None:
        fake_runner = FakeRunner(
            WorkerOutcome(
                processed_count=0,
                outcomes=[],
                interrupted=False,
                stop_reason='task_failed',
                idle_cycles=0,
                failure=TaskFailure(
                    task_id='fe43d9ec-1750-47a0-9ee7-d7f61fbca49a',
                    run_id='run-002',
                    failed_flow='writeback',
                    flow_timings={
                        'intake': 4,
                        'dispatch': 0,
                        'execute_start': 0,
                        'execute': 180,
                        'writeback': 12,
                        'statusback': 0,
                    },
                    total_duration_ms=196,
                    error_message='simulated writeback failure',
                ),
            )
        )
        config = SimpleNamespace(config_source='project_root:.env', env_path=Path('D:/PY_REPO/ClawMind/.env'))

        with patch('app.main.build_runner', return_value=fake_runner):
            with patch('builtins.print') as mock_print:
                exit_code = run_worker(
                    config,
                    logseq_dir=Path('D:/logseq'),
                    llm_adapter=FakeLlmAdapter(),
                    max_tasks=1,
                    poll_interval_seconds=1.0,
                    heartbeat_interval_seconds=5.0,
                )

        self.assertEqual(exit_code, 1)
        printed_lines = [call.args[0] for call in mock_print.call_args_list]
        self.assertIn('task_flow_summary short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-002 result_status=FAILED failed_flow=writeback total_duration_ms=196', printed_lines)
        self.assertIn('task_flow short_id=fe43d9ec task_id=fe43d9ec-1750-47a0-9ee7-d7f61fbca49a run_id=run-002 flow_name=writeback duration_ms=12', printed_lines)
        self.assertIn('task_flow_failure short_id=fe43d9ec failed_flow=writeback error_message=simulated writeback failure', printed_lines)
        self.assertIn('running_worker_status=STOPPED reason=task_failed processed_count=0', printed_lines)

    def test_main_uses_config_logseq_dir_as_default(self) -> None:
        fake_config = SimpleNamespace(
            logseq_dir=Path('E:/logseq'),
            codex_cli_path='codex',
            root_dir=Path('D:/PY_REPO/ClawMind'),
            config_source='project_root:.env',
            env_path=Path('D:/PY_REPO/ClawMind/.env'),
            codex_timeout_seconds=300,
        )

        with patch('app.main.AppConfig', return_value=fake_config):
            with patch('app.main.run_worker', return_value=0) as mock_run_worker:
                with patch.object(sys, 'argv', ['prog', 'run-worker']):
                    exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_run_worker.call_args.kwargs['logseq_dir'], Path('E:/logseq'))

    def test_main_version_prints_cli_version(self) -> None:
        with patch('app.main.get_cli_version', return_value='0.1.0-test'):
            with patch('builtins.print') as mock_print:
                exit_code = main(['version'])

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_print.call_args_list[0].args[0], 'clawmind 0.1.0-test')

    def test_main_install_info_prints_detected_install_metadata(self) -> None:
        with patch(
            'app.main.build_install_info',
            return_value=SimpleNamespace(
                package_name='clawmind',
                package_version='0.1.0',
                executable_path='C:/tools/clawmind.exe',
                python_path='C:/Python313/python.exe',
                install_method='pipx',
                install_hint='path_contains:pipx',
            ),
        ):
            with patch('builtins.print') as mock_print:
                exit_code = main(['install-info'])

        self.assertEqual(exit_code, 0)
        printed_lines = [call.args[0] for call in mock_print.call_args_list]
        self.assertIn('package_name=clawmind', printed_lines)
        self.assertIn('package_version=0.1.0', printed_lines)
        self.assertIn('install_method=pipx', printed_lines)
        self.assertIn('install_hint=path_contains:pipx', printed_lines)

    def test_detect_install_method_prefers_pipx_markers(self) -> None:
        method, hint = detect_install_method(
            executable_path='C:/Users/demo/pipx/venvs/clawmind/Scripts/clawmind.exe',
            python_path='C:/Users/demo/pipx/venvs/clawmind/Scripts/python.exe',
            env={},
        )

        self.assertEqual(method, 'pipx')
        self.assertEqual(hint, 'path_contains:pipx')

    def test_detect_install_method_prefers_uv_tool_markers(self) -> None:
        method, hint = detect_install_method(
            executable_path='C:/Users/demo/.local/share/uv/tools/clawmind/Scripts/clawmind.exe',
            python_path='C:/Users/demo/.local/share/uv/tools/clawmind/Scripts/python.exe',
            env={},
        )

        self.assertEqual(method, 'uv')
        self.assertEqual(hint, 'path_contains:uv_tools')

    def test_build_install_info_falls_back_to_pip(self) -> None:
        with patch('app.main.get_cli_version', return_value='0.1.0'):
            info = build_install_info(
                executable_path='C:/Python313/Scripts/clawmind.exe',
                python_path='C:/Python313/python.exe',
                env={},
            )

        self.assertEqual(info.install_method, 'pip')
        self.assertEqual(info.install_hint, 'fallback:python_env')

    def test_run_upgrade_uses_auto_detected_pipx(self) -> None:
        fake_completed = SimpleNamespace(returncode=0)
        with patch(
            'app.main.build_install_info',
            return_value=SimpleNamespace(
                install_method='pipx',
            ),
        ):
            with patch('builtins.print') as mock_print:
                exit_code = run_upgrade(
                    method='auto',
                    runner=lambda command, check=False: fake_completed,
                    which=lambda command: f'C:/bin/{command}.exe',
                )

        self.assertEqual(exit_code, 0)
        printed_lines = [call.args[0] for call in mock_print.call_args_list]
        self.assertIn('upgrade_method=pipx', printed_lines)
        self.assertIn('upgrade_command=pipx upgrade clawmind', printed_lines)

    def test_run_upgrade_uses_explicit_uv_method(self) -> None:
        fake_completed = SimpleNamespace(returncode=0)
        with patch('builtins.print') as mock_print:
            exit_code = run_upgrade(
                method='uv',
                runner=lambda command, check=False: fake_completed,
                which=lambda command: f'C:/bin/{command}.exe',
            )

        self.assertEqual(exit_code, 0)
        printed_lines = [call.args[0] for call in mock_print.call_args_list]
        self.assertIn('upgrade_method=uv', printed_lines)
        self.assertIn('upgrade_command=uv tool upgrade clawmind', printed_lines)

    def test_run_upgrade_falls_back_to_python_m_pip(self) -> None:
        fake_completed = SimpleNamespace(returncode=0)
        with patch('app.main.sys.executable', 'C:/Python313/python.exe'):
            with patch('builtins.print') as mock_print:
                exit_code = run_upgrade(
                    method='pip',
                    runner=lambda command, check=False: fake_completed,
                )

        self.assertEqual(exit_code, 0)
        printed_lines = [call.args[0] for call in mock_print.call_args_list]
        self.assertIn('upgrade_method=pip', printed_lines)
        self.assertIn('upgrade_command=C:/Python313/python.exe -m pip install -U clawmind', printed_lines)


if __name__ == '__main__':
    unittest.main()
