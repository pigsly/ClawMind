from pathlib import Path
import json
import shutil
import unittest
import uuid

from app.domain.enums import ArtifactType, ResultStatus
from app.domain.models import ExecutionResult
from app.repositories.artifact_repository import ArtifactRepository


class ArtifactRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent / '_tmp_artifacts' / uuid.uuid4().hex
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.repository = ArtifactRepository(self.root)

    def test_persist_writes_manifest_and_artifact_content(self) -> None:
        result = ExecutionResult(
            result_status=ResultStatus.SUCCESS,
            artifact_content='# Answer\n\n內容',
            artifact_type=ArtifactType.MARKDOWN,
            target_file=None,
            unresolved_items=[],
            confidence=0.9,
        )

        artifact_path = self.repository.persist(task_id='task-1', run_id='run-1', result=result)

        assert artifact_path is not None
        manifest_path = self.root / 'task-1' / 'run-1' / 'execution_result.json'
        self.assertEqual(artifact_path, self.root / 'task-1' / 'run-1' / 'artifact.md')
        self.assertEqual(artifact_path.read_text(encoding='utf-8'), '# Answer\n\n內容')
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
        self.assertEqual(payload['artifact_type'], ArtifactType.MARKDOWN.value)
        self.assertEqual(payload['result_status'], ResultStatus.SUCCESS.value)

    def test_persist_writes_manifest_without_artifact_file_for_none_type(self) -> None:
        result = ExecutionResult(
            result_status=ResultStatus.FAILED,
            artifact_content=None,
            artifact_type=ArtifactType.NONE,
            target_file=None,
            unresolved_items=['timeout'],
            confidence=0.1,
        )

        artifact_path = self.repository.persist(task_id='task-2', run_id='run-2', result=result)

        self.assertIsNone(artifact_path)
        manifest_path = self.root / 'task-2' / 'run-2' / 'execution_result.json'
        self.assertTrue(manifest_path.exists())
        self.assertEqual(len(list((self.root / 'task-2' / 'run-2').glob('artifact.*'))), 0)

    def test_load_result_rehydrates_execution_result_from_manifest(self) -> None:
        result = ExecutionResult(
            result_status=ResultStatus.SUCCESS,
            artifact_content='# Answer\n\nreplay',
            artifact_type=ArtifactType.MARKDOWN,
            target_file=None,
            unresolved_items=[],
            confidence=0.7,
            assumptions=['cached'],
            audit_log={'tools_used': ['codex']},
        )
        self.repository.persist(task_id='task-3', run_id='run-3', result=result)

        loaded = self.repository.load_result(task_id='task-3', run_id='run-3')

        self.assertEqual(loaded.result_status, ResultStatus.SUCCESS)
        self.assertEqual(loaded.artifact_content, '# Answer\n\nreplay')
        self.assertEqual(loaded.artifact_type, ArtifactType.MARKDOWN)
        self.assertEqual(loaded.assumptions, ['cached'])
        self.assertEqual(loaded.audit_log['tools_used'], ['codex'])


if __name__ == '__main__':
    unittest.main()
