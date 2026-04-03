from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from hashlib import sha1
from uuid import uuid4

from app.domain.enums import RuntimeStatus, TaskKeyword
from app.domain.models import Task

TASK_LINE_PATTERN = re.compile(
    r"^(?P<indent>\s*)- (?P<keyword>TODO|DOING|WAITING)\s+(?P<text>.+)$"
)
PROPERTY_PATTERN = re.compile(
    r"^(?P<indent>\s+)(?P<key>[A-Za-z0-9_\-]+)::\s*(?P<value>.*)$"
)
PAGE_LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")
TRAILING_GENERATED_LINK_PATTERN = re.compile(r"\s+\[\[[^\]]*__[^\]]+\]\]$")


@dataclass(slots=True)
class TaskRecord:
    task: Task
    journal_path: Path
    line_index: int
    property_start: int
    property_end: int
    indent: str


class LogseqAdapter:
    def __init__(
        self,
        logseq_root: Path | str,
        *,
        journal_scan_days: int | None = None,
        default_max_retries: int = 2,
        reference_date: date | None = None,
    ) -> None:
        self.logseq_root = Path(logseq_root)
        self.journals_dir = self.logseq_root / "journals"
        self.pages_dir = self.logseq_root / "pages"
        self.answer_dir = self.pages_dir / "answer"
        self.journal_scan_days = journal_scan_days
        self.default_max_retries = default_max_retries
        self.reference_date = reference_date

    def scan_doing_tasks(self) -> list[TaskRecord]:
        return self._scan_tasks(TaskKeyword.DOING)

    def scan_waiting_tasks(self) -> list[TaskRecord]:
        return self._scan_tasks(TaskKeyword.WAITING)

    def normalize_task_id(self, record: TaskRecord) -> TaskRecord:
        if record.task.task_id:
            return self._parse_record(record.journal_path, record.line_index)

        lines = self._read_lines(record.journal_path)
        current = self._parse_record(record.journal_path, record.line_index)
        properties = dict(current.task.properties)
        properties["id"] = str(uuid4())
        lines = self._replace_property_block(lines, current, properties)
        self._write_lines(record.journal_path, lines)

        normalized = self._parse_record(record.journal_path, record.line_index)
        if not normalized.task.task_id:
            raise ValueError("UUID normalization failed to persist id:: before lock.")
        return normalized

    def lock_task(
        self,
        record: TaskRecord,
        *,
        lock_owner: str,
        locked_at: str,
        run_id: str,
        idempotency_key: str,
    ) -> TaskRecord:
        current = self._parse_record(record.journal_path, record.line_index)
        if not current.task.task_id:
            raise ValueError("Task must be normalized with id:: before lock.")

        lines = self._read_lines(record.journal_path)
        current_line = lines[record.line_index]
        current_match = TASK_LINE_PATTERN.match(current_line)
        if current_match is None or current_match.group("keyword") != TaskKeyword.DOING.value:
            raise ValueError("Task is no longer in DOING state.")

        text = current_match.group("text")
        lines[record.line_index] = f"{record.indent}- {TaskKeyword.WAITING.value} {text}"

        properties = dict(current.task.properties)
        lines = self._replace_property_block(lines, current, properties)
        self._write_lines(current.journal_path, lines)
        locked = self._parse_record(current.journal_path, current.line_index)
        locked.task.run_id = run_id
        locked.task.idempotency_key = idempotency_key
        locked.task.locked_at = locked_at
        locked.task.lock_owner = lock_owner
        locked.task.runtime_status = RuntimeStatus.RUNNING
        locked.task.updated_at = locked_at
        return locked

    def update_task_keyword(self, record: TaskRecord, keyword: TaskKeyword) -> TaskRecord:
        lines = self._read_lines(record.journal_path)
        match = TASK_LINE_PATTERN.match(lines[record.line_index])
        if match is None:
            raise ValueError("Task line is missing or malformed.")
        lines[record.line_index] = f"{record.indent}- {keyword.value} {match.group('text')}"
        self._write_lines(record.journal_path, lines)
        return self._parse_record(record.journal_path, record.line_index)

    def update_block_properties(
        self,
        record: TaskRecord,
        updates: dict[str, str],
    ) -> TaskRecord:
        properties = dict(record.task.properties)
        properties.update(updates)
        lines = self._read_lines(record.journal_path)
        lines = self._replace_property_block(lines, record, properties)
        self._write_lines(record.journal_path, lines)
        return self._parse_record(record.journal_path, record.line_index)

    def build_answer_page_name(self, record: TaskRecord) -> str:
        date_segment = self._build_answer_page_date_segment(record)
        short_uuid = self._build_short_uuid(record.task.task_id)
        return f"{date_segment}__{short_uuid}"

    def build_answer_page_filename(self, record: TaskRecord) -> str:
        return f"{self.build_answer_page_name(record)}.md"

    def build_answer_page_path(self, record: TaskRecord) -> Path:
        return self.answer_dir / self.build_answer_page_filename(record)

    def build_answer_page_relative_path(self, record: TaskRecord) -> str:
        return f"answer/{self.build_answer_page_filename(record)}"

    def write_answer_page(self, record: TaskRecord, content: str) -> Path:
        self.answer_dir.mkdir(parents=True, exist_ok=True)
        target = self.build_answer_page_path(record)
        target.write_text(content, encoding="utf-8")
        return target

    def append_journal_link(self, record: TaskRecord, page_name: str) -> bool:
        lines = self._read_lines(record.journal_path)
        task_line = lines[record.line_index]
        link = f"[[{page_name}]]"
        if link in task_line:
            return False
        lines[record.line_index] = f"{task_line} {link}"
        self._write_lines(record.journal_path, lines)
        return True

    def _scan_tasks(self, target_keyword: TaskKeyword) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for journal_path in self._iter_journal_paths():
            records.extend(self._parse_journal(journal_path, target_keyword=target_keyword))
        return records

    def _iter_journal_paths(self) -> list[Path]:
        journal_paths = sorted(self.journals_dir.glob("*.md"))
        if self.journal_scan_days is None:
            return journal_paths

        today = self.reference_date or date.today()
        earliest = today - timedelta(days=self.journal_scan_days - 1)
        filtered: list[Path] = []
        for journal_path in journal_paths:
            journal_date = self._parse_journal_date(journal_path)
            if journal_date is None:
                continue
            if earliest <= journal_date <= today:
                filtered.append(journal_path)
        return filtered

    def _parse_journal_date(self, journal_path: Path) -> date | None:
        try:
            return date.fromisoformat(journal_path.stem.replace("_", "-"))
        except ValueError:
            return None

    def _parse_journal(
        self,
        journal_path: Path,
        *,
        target_keyword: TaskKeyword | None = None,
    ) -> list[TaskRecord]:
        lines = self._read_lines(journal_path)
        records: list[TaskRecord] = []
        for line_index, line in enumerate(lines):
            match = TASK_LINE_PATTERN.match(line)
            if match is None:
                continue
            keyword = TaskKeyword(match.group("keyword"))
            if target_keyword is not None and keyword != target_keyword:
                continue
            records.append(self._build_record(journal_path, lines, line_index, match))
        return records

    def _parse_record(self, journal_path: Path, line_index: int) -> TaskRecord:
        lines = self._read_lines(journal_path)
        match = TASK_LINE_PATTERN.match(lines[line_index])
        if match is None:
            raise ValueError("Task line is missing or malformed.")
        return self._build_record(journal_path, lines, line_index, match)

    def _build_record(
        self,
        journal_path: Path,
        lines: list[str],
        line_index: int,
        match: re.Match[str],
    ) -> TaskRecord:
        keyword = TaskKeyword(match.group("keyword"))
        indent = match.group("indent")
        text = match.group("text")
        properties, property_end = self._extract_properties(lines, line_index, indent)
        block_uuid = properties.get("id", "")
        task_id = block_uuid
        raw_block_text = self._collect_block_text(lines, line_index, indent)
        page_links = sorted(set(PAGE_LINK_PATTERN.findall(raw_block_text)))

        task = Task(
            task_id=task_id,
            run_id=properties.get("run_id", ""),
            idempotency_key=properties.get("idempotency_key", ""),
            task_keyword=keyword,
            runtime_status=RuntimeStatus(
                properties.get("task_runner_status", RuntimeStatus.PENDING.value)
            ),
            priority=int(properties.get("priority", "0") or 0),
            retry_count=int(properties.get("retry_count", "0") or 0),
            max_retries=int(
                properties.get("max_retries", str(self.default_max_retries)) or self.default_max_retries
            ),
            locked_at=properties.get("locked_at"),
            lock_owner=properties.get("lock_owner"),
            created_at=properties.get("task_created_at", ""),
            updated_at=properties.get("updated_at", ""),
            block_uuid=block_uuid,
            page_id=journal_path.stem,
            raw_block_text=raw_block_text,
            properties=properties,
            page_links=page_links,
        )
        return TaskRecord(
            task=task,
            journal_path=journal_path,
            line_index=line_index,
            property_start=line_index + 1,
            property_end=property_end,
            indent=indent,
        )

    def _extract_properties(
        self,
        lines: list[str],
        line_index: int,
        indent: str,
    ) -> tuple[dict[str, str], int]:
        properties: dict[str, str] = {}
        property_end = line_index + 1
        expected_indent_length = len(indent) + 2
        for index in range(line_index + 1, len(lines)):
            line = lines[index]
            if not line.strip():
                property_end = index + 1
                continue
            task_match = TASK_LINE_PATTERN.match(line)
            if task_match and len(task_match.group("indent")) <= len(indent):
                break
            property_match = PROPERTY_PATTERN.match(line)
            if property_match and len(property_match.group("indent")) >= expected_indent_length:
                properties[property_match.group("key")] = property_match.group("value").strip()
                property_end = index + 1
                continue
            property_end = index
            break
        return properties, property_end

    def _collect_block_text(self, lines: list[str], line_index: int, indent: str) -> str:
        block_lines = [lines[line_index].rstrip()]
        for index in range(line_index + 1, len(lines)):
            line = lines[index]
            if not line.strip():
                block_lines.append("")
                continue
            task_match = TASK_LINE_PATTERN.match(line)
            if task_match and len(task_match.group("indent")) <= len(indent):
                break
            block_lines.append(line.rstrip())
        return "\n".join(block_lines)

    def _replace_property_block(
        self,
        lines: list[str],
        record: TaskRecord,
        properties: dict[str, str],
    ) -> list[str]:
        property_lines = [
            f"{record.indent}  {key}:: {value}".rstrip()
            for key, value in properties.items()
        ]
        return lines[: record.property_start] + property_lines + lines[record.property_end :]

    def _build_answer_page_date_segment(self, record: TaskRecord) -> str:
        journal_date = self._parse_journal_date(record.journal_path)
        if journal_date is None:
            raise ValueError('Journal page date is not a stable YYYY_MM_DD value.')
        return journal_date.strftime('%Y%m%d')

    def _build_short_uuid(self, task_id: str) -> str:
        compact = re.sub(r'[^a-z0-9]', '', task_id.lower())
        if compact:
            return compact[:8]
        return sha1(task_id.encode('utf-8')).hexdigest()[:8]

    def _read_lines(self, path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines()

    def _write_lines(self, path: Path, lines: list[str]) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")



