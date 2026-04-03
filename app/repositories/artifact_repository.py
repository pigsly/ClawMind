from __future__ import annotations

import json
from pathlib import Path

from app.domain.enums import ArtifactType
from app.domain.models import ExecutionResult


class ArtifactRepository:
    def __init__(self, runtime_artifacts_dir: Path | str) -> None:
        self.runtime_artifacts_dir = Path(runtime_artifacts_dir)

    def persist(
        self,
        *,
        task_id: str,
        run_id: str,
        result: ExecutionResult,
    ) -> Path | None:
        target_dir = self.runtime_artifacts_dir / task_id / run_id
        target_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = target_dir / 'execution_result.json'
        manifest_path.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        if result.artifact_content is None or result.artifact_type == ArtifactType.NONE:
            return None

        artifact_path = target_dir / self._resolve_filename(result.artifact_type)
        artifact_path.write_text(result.artifact_content, encoding='utf-8')
        return artifact_path

    def load_result(self, *, task_id: str, run_id: str) -> ExecutionResult:
        manifest_path = self.runtime_artifacts_dir / task_id / run_id / 'execution_result.json'
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
        return ExecutionResult.from_dict(payload)

    def _resolve_filename(self, artifact_type: ArtifactType) -> str:
        mapping = {
            ArtifactType.MARKDOWN: 'artifact.md',
            ArtifactType.JSON: 'artifact.json',
            ArtifactType.PATCH: 'artifact.patch',
            ArtifactType.TEXT: 'artifact.txt',
        }
        return mapping.get(artifact_type, 'artifact.txt')
