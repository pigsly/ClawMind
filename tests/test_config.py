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

    def test_prefers_cwd_env_when_explicit_path_is_missing(self) -> None:
        cwd_dir = self.temp_root / 'cwd'
        cwd_dir.mkdir()
        cwd_env = cwd_dir / '.env'
        cwd_env.write_text('LOGSEQ_PATH=D:/cwd\n', encoding='utf-8')
        project_root = self.temp_root / 'project'
        project_root.mkdir()
        (project_root / '.env').write_text('LOGSEQ_PATH=D:/project\n', encoding='utf-8')

        with patch.dict(os.environ, {}, clear=True):
            with patch('pathlib.Path.cwd', return_value=cwd_dir):
                config = AppConfig(root_dir=project_root)

        self.assertEqual(config.env_path, cwd_env)
        self.assertEqual(config.config_source, 'cwd:.env')
        self.assertEqual(config.logseq_dir, Path('D:/cwd'))

    def test_falls_back_to_project_root_env(self) -> None:
        cwd_dir = self.temp_root / 'cwd'
        cwd_dir.mkdir()
        project_root = self.temp_root / 'project'
        project_root.mkdir()
        project_env = project_root / '.env'
        project_env.write_text('LOGSEQ_PATH=D:/project\n', encoding='utf-8')

        with patch.dict(os.environ, {}, clear=True):
            with patch('pathlib.Path.cwd', return_value=cwd_dir):
                config = AppConfig(root_dir=project_root)

        self.assertEqual(config.env_path, project_env)
        self.assertEqual(config.config_source, 'project_root:.env')
        self.assertEqual(config.logseq_dir, Path('D:/project'))


if __name__ == '__main__':
    unittest.main()
