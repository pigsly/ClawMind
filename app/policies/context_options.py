from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class ContextOptions:
    load_memory: bool = False
    load_adr: bool = False
    load_linked_pages: bool = True
    debugging_mode: bool = False
    execution_mode: str = "codex"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ContextOptions":
        if not data:
            return cls()
        return cls(
            load_memory=bool(data.get("load_memory", False)),
            load_adr=bool(data.get("load_adr", False)),
            load_linked_pages=bool(data.get("load_linked_pages", True)),
            debugging_mode=bool(data.get("debugging_mode", False)),
            execution_mode=str(data.get("execution_mode", "codex")),
        )
