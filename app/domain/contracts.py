from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class WritebackContract:
    task_id: str
    run_id: str
    idempotency_key: str
    result_status: str
    target_file: str | None = None
    links_to_append: list[str] = field(default_factory=list)
    writeback_actions: list[str] = field(default_factory=list)
    writeback_status: str = "PENDING"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WritebackContract":
        return cls(
            task_id=str(data["task_id"]),
            run_id=str(data["run_id"]),
            idempotency_key=str(data["idempotency_key"]),
            result_status=str(data["result_status"]),
            target_file=data.get("target_file"),
            links_to_append=[str(item) for item in data.get("links_to_append", [])],
            writeback_actions=[str(item) for item in data.get("writeback_actions", [])],
            writeback_status=str(data.get("writeback_status", "PENDING")),
        )
