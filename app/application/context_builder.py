from __future__ import annotations

from pathlib import Path

from app.domain.enums import AnalysisMode
from app.domain.models import ContextBundle, InstructionBundle, Task
from app.policies.context_options import ContextOptions


class ContextBuilder:
    def __init__(
        self,
        logseq_root: Path | str = 'logseq',
        *,
        run_logs_dir: Path | str = 'run_logs',
        runtime_artifacts_dir: Path | str = 'runtime_artifacts',
    ) -> None:
        self.logseq_root = Path(logseq_root)
        self.pages_dir = self.logseq_root / 'pages'
        self.journals_dir = self.logseq_root / 'journals'
        self.run_logs_dir = Path(run_logs_dir)
        self.runtime_artifacts_dir = Path(runtime_artifacts_dir)

    def build(
        self,
        task: Task,
        *,
        instruction_bundle: InstructionBundle | None = None,
        runtime_options: ContextOptions | None = None,
    ) -> ContextBundle:
        context_bundle, _ = self.build_with_audit(
            task,
            instruction_bundle=instruction_bundle,
            runtime_options=runtime_options,
        )
        return context_bundle

    def build_with_audit(
        self,
        task: Task,
        *,
        instruction_bundle: InstructionBundle | None = None,
        runtime_options: ContextOptions | None = None,
    ) -> tuple[ContextBundle, dict[str, object]]:
        options = self._resolve_options(task, runtime_options=runtime_options)
        analysis_mode = instruction_bundle.analysis_mode if instruction_bundle is not None else AnalysisMode.NORMAL
        pages, page_audit = self._build_pages(task, analysis_mode, load_linked_pages=options.load_linked_pages)
        memory = self._load_directory(self.pages_dir / 'memory') if options.load_memory else {}
        adr = self._load_directory(self.pages_dir / 'ADR') if (options.load_adr or analysis_mode in {AnalysisMode.REASONING_ANALYSIS, AnalysisMode.CROSS_PAGE_SYNTHESIS}) else {}
        skill_context = self._build_debug_context(task) if options.debugging_mode else {}
        return ContextBundle(
            task=task,
            pages=pages,
            memory=memory,
            adr=adr,
            skill_context=skill_context,
            context_options=options,
        ), {
            'linked_page_context': page_audit,
        }

    def _resolve_options(
        self,
        task: Task,
        *,
        runtime_options: ContextOptions | None,
    ) -> ContextOptions:
        runtime_options = runtime_options or ContextOptions()
        analysis_mode = task.properties.get('analysis_mode', AnalysisMode.NORMAL.value)
        load_adr = task.properties.get('load_adr', 'false').lower() == 'true' or analysis_mode in {
            AnalysisMode.REASONING_ANALYSIS.value,
            AnalysisMode.CROSS_PAGE_SYNTHESIS.value,
        }
        return ContextOptions(
            load_memory=task.properties.get('load_memory', 'false').lower() == 'true',
            load_adr=load_adr,
            load_linked_pages=task.properties.get('load_linked_pages', 'true').lower() == 'true',
            debugging_mode=runtime_options.debugging_mode,
            execution_mode=task.properties.get('execution_mode', runtime_options.execution_mode),
        )

    def _build_pages(
        self,
        task: Task,
        analysis_mode: AnalysisMode,
        *,
        load_linked_pages: bool,
    ) -> tuple[dict[str, str], dict[str, object]]:
        pages: dict[str, str] = {}
        page_audit: dict[str, object] = {
            'analysis_mode': analysis_mode.value,
            'load_linked_pages': load_linked_pages,
            'requested_page_links': list(task.page_links),
            'selected_page_links': [],
            'resolution': [],
        }
        if analysis_mode in {AnalysisMode.REASONING_ANALYSIS, AnalysisMode.CROSS_PAGE_SYNTHESIS}:
            current_page = self._load_current_page(task)
            if current_page is not None:
                pages[task.page_id] = current_page
        if not load_linked_pages:
            return pages, page_audit

        linked_page_names = list(task.page_links)
        if analysis_mode == AnalysisMode.NORMAL:
            linked_page_names = linked_page_names[:1]
        elif analysis_mode == AnalysisMode.REASONING_ANALYSIS:
            linked_page_names = linked_page_names[:2]
        page_audit['selected_page_links'] = list(linked_page_names)

        resolution: list[dict[str, object]] = []
        selected_page_names = set(linked_page_names)
        for page_name in task.page_links:
            resolved = self._resolve_page_path(page_name)
            found = resolved is not None and resolved.exists()
            loaded_to_context = page_name in selected_page_names and found
            resolution.append(
                {
                    'page_name': page_name,
                    'selected_for_context': page_name in selected_page_names,
                    'found_page': found,
                    'loaded_to_context': loaded_to_context,
                    'resolved_path': str(resolved) if found and resolved is not None else None,
                }
            )
        page_audit['resolution'] = resolution

        for page_name in linked_page_names:
            resolved = self._resolve_page_path(page_name)
            if resolved is None or not resolved.exists():
                continue
            pages[page_name] = resolved.read_text(encoding='utf-8')
        return pages, page_audit

    def _load_current_page(self, task: Task) -> str | None:
        journal_path = self.journals_dir / f'{task.page_id}.md'
        if journal_path.exists():
            return journal_path.read_text(encoding='utf-8')
        page_path = self.pages_dir / f'{task.page_id}.md'
        if page_path.exists():
            return page_path.read_text(encoding='utf-8')
        return None

    def _resolve_page_path(self, page_name: str) -> Path | None:
        candidates = [
            self.pages_dir / f'{page_name}.md',
            self.pages_dir / 'answer' / f'{page_name}.md',
            self.pages_dir / 'memory' / f'{page_name}.md',
            self.pages_dir / 'ADR' / f'{page_name}.md',
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _load_directory(self, directory: Path) -> dict[str, str]:
        if not directory.exists():
            return {}
        bundle: dict[str, str] = {}
        for path in sorted(directory.glob('*.md')):
            bundle[path.stem] = path.read_text(encoding='utf-8')
        return bundle

    def _build_debug_context(self, task: Task) -> dict[str, str]:
        debug_context: dict[str, str] = {}
        task_run_log_dir = self.run_logs_dir / task.task_id
        if task_run_log_dir.exists():
            for path in sorted(task_run_log_dir.glob('*.json')):
                debug_context[f'run_logs/{path.name}'] = path.read_text(encoding='utf-8')
        task_artifact_dir = self.runtime_artifacts_dir / task.task_id
        if task_artifact_dir.exists():
            for path in sorted(task_artifact_dir.rglob('*')):
                if path.is_file():
                    relative = path.relative_to(self.runtime_artifacts_dir)
                    debug_context[f'runtime_artifacts/{relative.as_posix()}'] = path.read_text(encoding='utf-8')
        return debug_context

