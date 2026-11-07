"""The shape of what a check reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

ERROR = "error"  # the writer must fix it: the row is rejected and rebounded once
FLAG = "flag"  # a reviewer should look; the row is not rejected for it


@dataclass(frozen=True)
class Problem:
    """One finding: a short lower-case code, the row (or series, or batch) it is about, and a plain message."""

    code: str
    row_id: str
    message: str
    severity: str = ERROR

    def line(self) -> str:
        """The text a writer is sent back for an error."""
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "row_id": self.row_id, "message": self.message, "severity": self.severity}


def errors(problems: Iterable[Problem]) -> list[Problem]:
    return [p for p in problems if p.severity == ERROR]


def flags(problems: Iterable[Problem]) -> list[Problem]:
    return [p for p in problems if p.severity == FLAG]


def item_ids(item: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The row ids a spec item allots (empty when the item is not a spec item)."""
    if isinstance(item, Mapping) and isinstance(item.get("ids"), (list, tuple)):
        return tuple(str(i) for i in item["ids"])
    return ()
