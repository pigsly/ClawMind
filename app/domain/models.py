from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.domain.contracts import WritebackContract
from app.domain.enums import (
    AnalysisMode,
    AnswerType,
    ArtifactType,
    ExecutorType,
    ResultStatus,
    RuntimeStatus,
    TaskKeyword,
    TaskType,
)
from app.policies.context_options import ContextOptions


@dataclass(slots=True)
class Task:
    task_id: str
    run_id: str
    idempotency_key: str
    task_keyword: TaskKeyword
    runtime_status: RuntimeStatus
    priority: int
    retry_count: int
    max_retries: int
    locked_at: str | None
    lock_owner: str | None
    created_at: str
    updated_at: str
    block_uuid: str
    page_id: str
    raw_block_text: str
    properties: dict[str, str] = field(default_factory=dict)
    page_links: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["task_keyword"] = self.task_keyword.value
        data["runtime_status"] = self.runtime_status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            task_id=str(data["task_id"]),
            run_id=str(data["run_id"]),
            idempotency_key=str(data["idempotency_key"]),
            task_keyword=TaskKeyword(data["task_keyword"]),
            runtime_status=RuntimeStatus(data["runtime_status"]),
            priority=int(data["priority"]),
            retry_count=int(data["retry_count"]),
            max_retries=int(data["max_retries"]),
            locked_at=data.get("locked_at"),
            lock_owner=data.get("lock_owner"),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            block_uuid=str(data["block_uuid"]),
            page_id=str(data["page_id"]),
            raw_block_text=str(data["raw_block_text"]),
            properties={str(k): str(v) for k, v in data.get("properties", {}).items()},
            page_links=[str(item) for item in data.get("page_links", [])],
        )


@dataclass(slots=True)
class InstructionBundle:
    task_type: TaskType
    analysis_mode: AnalysisMode
    executor_type: ExecutorType
    model: str | None = None
    template_id: str | None = None
    instruction_patch: str | None = None
    expected_output_type: str = "markdown"
    validation_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["task_type"] = self.task_type.value
        data["analysis_mode"] = self.analysis_mode.value
        data["executor_type"] = self.executor_type.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstructionBundle":
        return cls(
            task_type=TaskType(data["task_type"]),
            analysis_mode=AnalysisMode(data.get("analysis_mode", AnalysisMode.NORMAL.value)),
            executor_type=ExecutorType(data["executor_type"]),
            model=data.get("model"),
            template_id=data.get("template_id"),
            instruction_patch=data.get("instruction_patch"),
            expected_output_type=str(data.get("expected_output_type", "markdown")),
            validation_rules=[str(item) for item in data.get("validation_rules", [])],
        )


@dataclass(slots=True)
class ContextBundle:
    task: Task
    pages: dict[str, str] = field(default_factory=dict)
    memory: dict[str, str] = field(default_factory=dict)
    adr: dict[str, str] = field(default_factory=dict)
    skill_context: dict[str, str] = field(default_factory=dict)
    context_options: ContextOptions = field(default_factory=ContextOptions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "pages": dict(self.pages),
            "memory": dict(self.memory),
            "adr": dict(self.adr),
            "skill_context": dict(self.skill_context),
            "context_options": self.context_options.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextBundle":
        return cls(
            task=Task.from_dict(data["task"]),
            pages={str(k): str(v) for k, v in data.get("pages", {}).items()},
            memory={str(k): str(v) for k, v in data.get("memory", {}).items()},
            adr={str(k): str(v) for k, v in data.get("adr", {}).items()},
            skill_context={
                str(k): str(v) for k, v in data.get("skill_context", {}).items()
            },
            context_options=ContextOptions.from_dict(data.get("context_options")),
        )


@dataclass(slots=True)
class UncertaintyItem:
    type: str
    impact: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "impact": self.impact,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UncertaintyItem":
        return cls(
            type=str(data.get("type", "unspecified")),
            impact=str(data.get("impact", "medium")),
            description=str(data.get("description", "")),
        )


@dataclass(slots=True)
class ExecutionResult:
    result_status: ResultStatus
    artifact_content: str | None
    artifact_type: ArtifactType
    target_file: str | None
    links_to_append: list[str] = field(default_factory=list)
    writeback_actions: list[str] = field(default_factory=list)
    unresolved_items: list[str] = field(default_factory=list)
    answer_type: AnswerType = AnswerType.BEST_EFFORT
    summary: str = ""
    answer_paragraphs: list[str] = field(default_factory=list)
    uncertainty: list[UncertaintyItem] = field(default_factory=list)
    confidence: float = 0.0
    assumptions: list[str] = field(default_factory=list)
    audit_log: dict[str, Any] = field(default_factory=dict)
    writeback_contract: WritebackContract | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["result_status"] = self.result_status.value
        data["artifact_type"] = self.artifact_type.value
        data["answer_type"] = self.answer_type.value
        data["uncertainty"] = [item.to_dict() for item in self.uncertainty]
        if self.writeback_contract is not None:
            data["writeback_contract"] = self.writeback_contract.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionResult":
        writeback_data = data.get("writeback_contract")
        return cls(
            result_status=ResultStatus(data["result_status"]),
            artifact_content=data.get("artifact_content"),
            artifact_type=ArtifactType(data["artifact_type"]),
            target_file=data.get("target_file"),
            links_to_append=[str(item) for item in data.get("links_to_append", [])],
            writeback_actions=[str(item) for item in data.get("writeback_actions", [])],
            unresolved_items=[str(item) for item in data.get("unresolved_items", [])],
            answer_type=AnswerType(data.get("answer_type", AnswerType.BEST_EFFORT.value)),
            summary=str(data.get("summary", "")),
            answer_paragraphs=[str(item) for item in data.get("answer_paragraphs", [])],
            uncertainty=[UncertaintyItem.from_dict(item) for item in data.get("uncertainty", [])],
            confidence=float(data.get("confidence", 0.0)),
            assumptions=[str(item) for item in data.get("assumptions", [])],
            audit_log=dict(data.get("audit_log", {})),
            writeback_contract=(
                WritebackContract.from_dict(writeback_data) if writeback_data else None
            ),
        )
