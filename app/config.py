from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    project_name: str = 'ClawMind'
    root_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    config_source: str = field(init=False)
    env_path: Path = field(init=False)
    logseq_dir: Path = field(init=False)
    run_logs_dir: Path = field(init=False)
    runtime_artifacts_dir: Path = field(init=False)
    codex_cli_path: str = field(init=False)
    journal_scan_days: int | None = field(init=False)
    max_retries: int = field(init=False)
    codex_timeout_seconds: int | None = field(init=False)

    def __post_init__(self) -> None:
        self.env_path, self.config_source = self._resolve_env_path()
        env_values = self._load_env_file(self.env_path)
        logseq_value = env_values.get('LOGSEQ_PATH') or os.environ.get('LOGSEQ_PATH')
        codex_value = env_values.get('CODEX_CLI_PATH') or os.environ.get('CODEX_CLI_PATH') or 'codex'
        scan_days_value = env_values.get('JOURNAL_SCAN_DAYS') or os.environ.get('JOURNAL_SCAN_DAYS')
        max_retries_value = env_values.get('MAX_RETRIES') or os.environ.get('MAX_RETRIES')
        codex_timeout_value = env_values.get('CODEX_TIMEOUT_SECONDS') or os.environ.get('CODEX_TIMEOUT_SECONDS')
        self.logseq_dir = Path(logseq_value) if logseq_value else self.root_dir / 'logseq'
        self.run_logs_dir = self.root_dir / 'run_logs'
        self.runtime_artifacts_dir = self.root_dir / 'runtime_artifacts'
        self.codex_cli_path = codex_value
        self.journal_scan_days = self._parse_optional_positive_int(scan_days_value)
        self.max_retries = self._parse_positive_int_with_default(max_retries_value, default=2)
        self.codex_timeout_seconds = self._parse_optional_positive_int(codex_timeout_value)

    def _resolve_env_path(self) -> tuple[Path, str]:
        explicit_env = os.environ.get('CLAWMIND_ENV_PATH')
        if explicit_env:
            return Path(explicit_env), 'env_var:CLAWMIND_ENV_PATH'
        cwd_env_path = Path.cwd() / '.env'
        if cwd_env_path.exists():
            return cwd_env_path, 'cwd:.env'
        return self.root_dir / '.env', 'project_root:.env'

    def _load_env_file(self, env_path: Path) -> dict[str, str]:
        if not env_path.exists():
            return {}
        values: dict[str, str] = {}
        for raw_line in env_path.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
        return values

    def _parse_optional_positive_int(self, value: str | None) -> int | None:
        if value is None or not value.strip():
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None

    def _parse_positive_int_with_default(self, value: str | None, *, default: int) -> int:
        if value is None or not value.strip():
            return default
        parsed = int(value)
        return parsed if parsed > 0 else default
