from pathlib import Path
import os
import shutil
import unittest
import uuid
from unittest.mock import patch

from app.config import AppConfig


class AppConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path('tests/_tmp_config') / uuid.uuid4().hex
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(self.temp_root, ignore_errors=True))

    def test_prefers_explicit_env_path_over_cwd_and_project_root(self) -> None:
        explicit_env = self.temp_root / 'explicit.env'
        explicit_env.write_text('LOGSEQ_PATH=D:/explicit\n', encoding='utf-8')
        cwd_dir = self.temp_root / 'cwd'
        cwd_dir.mkdir()
        (cwd_dir / '.env').write_text('LOGSEQ_PATH=D:/cwd\n', encoding='utf-8')
        project_root = self.temp_root / 'project'
        project_root.mkdir()
        (project_root / '.env').write_text('LOGSEQ_PATH=D:/project\n', encoding='utf-8')

        with patch.dict(os.environ, {'CLAWMIND_ENV_PATH': str(explicit_env)}, clear=False):
            with patch('pathlib.Path.cwd', return_value=cwd_dir):
                config = AppConfig(root_dir=project_root)

        self.assertEqual(config.env_path, explicit_env)
        self.assertEqual(config.config_source, 'env_var:CLAWMIND_ENV_PATH')
        self.assertEqual(config.logseq_dir, Path('D:/explicit'))

    def test_runtime_output_dirs_follow_execution_directory(self) -> None:
        project_root = self.temp_root / 'project'
        project_root.mkdir()
        runtime_root = self.temp_root / 'runtime'
        runtime_root.mkdir()

        with patch.dict(os.environ, {}, clear=True):
            with patch('pathlib.Path.cwd', return_value=runtime_root):
                config = AppConfig(root_dir=project_root)

        self.assertEqual(config.root_dir, project_root)
        self.assertEqual(config.runtime_root_dir, runtime_root)
        self.assertEqual(config.run_logs_dir, runtime_root / 'run_logs')
        self.assertEqual(config.runtime_artifacts_dir, runtime_root / 'runtime_artifacts')

    def test_repo_mode_runtime_outputs_still_land_in_repo_root_when_executed_there(self) -> None:
        project_root = self.temp_root / 'project'
        project_root.mkdir()

        with patch.dict(os.environ, {}, clear=True):
            with patch('pathlib.Path.cwd', return_value=project_root):
                config = AppConfig(root_dir=project_root)

        self.assertEqual(config.run_logs_dir, project_root / 'run_logs')
        self.assertEqual(config.runtime_artifacts_dir, project_root / 'runtime_artifacts')

    def test_defaults_llm_brand_to_codex_cli(self) -> None:
        project_root = self.temp_root / 'project'
        project_root.mkdir()

        with patch.dict(os.environ, {}, clear=True):
            with patch('pathlib.Path.cwd', return_value=self.temp_root):
                config = AppConfig(root_dir=project_root)

        self.assertEqual(config.llm_brand, 'codex_cli')
        self.assertEqual(config.gemini_flash_model, 'gemini-2.5-flash')
        self.assertEqual(config.gemini_pro_model, 'gemini-2.5-pro')
        self.assertIsNone(config.gemini_api_key)

    def test_reads_gemini_api_settings_from_env_file(self) -> None:
        project_root = self.temp_root / 'project'
        project_root.mkdir()
        (project_root / '.env').write_text(
            'LLM_BRAND=gemini_api\nGEMINI_API_KEY=test-key\nGEMINI_FLASH_MODEL=gemini-2.5-flash\nGEMINI_PRO_MODEL=gemini-2.5-pro\n',
            encoding='utf-8',
        )

        with patch.dict(os.environ, {}, clear=True):
            with patch('pathlib.Path.cwd', return_value=self.temp_root):
                config = AppConfig(root_dir=project_root)

        self.assertEqual(config.llm_brand, 'gemini_api')
        self.assertEqual(config.gemini_api_key, 'test-key')
        self.assertEqual(config.gemini_flash_model, 'gemini-2.5-flash')
        self.assertEqual(config.gemini_pro_model, 'gemini-2.5-pro')

    def test_rejects_unknown_llm_brand(self) -> None:
        project_root = self.temp_root / 'project'
        project_root.mkdir()
        (project_root / '.env').write_text('LLM_BRAND=unknown_brand\n', encoding='utf-8')

        with patch.dict(os.environ, {}, clear=True):
            with patch('pathlib.Path.cwd', return_value=self.temp_root):
                with self.assertRaisesRegex(ValueError, 'Unsupported LLM_BRAND'):
                    AppConfig(root_dir=project_root)


if __name__ == '__main__':
    unittest.main()
