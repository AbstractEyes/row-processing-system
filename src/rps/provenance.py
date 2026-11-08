"""The labels stamped on every row.

The runner stamps two labels on each row it emits; a model never supplies them
(a model-written value under a reserved key is dropped):

    writer        which model wrote the row, through which route, when, and with
                  what picture size
    source_label  where the row's subject came from: dataset, item id, license

A row is emitted only after `stamp_row`, and every writer of rows calls
`require_label` first, so no path produces a row without its writer label.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .config import ROUTE_KINDS
from .ids import TASK_ID_PATTERN

RESERVED_ROW_KEYS: tuple[str, ...] = ("row_id", "writer", "source_label")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Rows the dataset owner writes or rewrites by hand carry this writer label.
HUMAN_PROVIDER = "human"
HUMAN_MODEL = "phil"
HUMAN_ROUTE = "hand"
LABEL_ROUTES: tuple[str, ...] = ROUTE_KINDS + (HUMAN_ROUTE,)


class ProvenanceError(ValueError):
    """A label is missing or incomplete."""


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError(f"{name} must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True)
class WriterLabel:
    """Who wrote a row. `route` is the access path (api, batch, claude-code, codex,
    gemini-cli, or hand for a row written by a person).

    `image_size` is the longest side in pixels of the picture the writer saw, or
    None when it saw no picture. `rules_version` is the version of the writing rules
    the writer was given; it is part of the label only when rules were used, and so is
    `rules_profile`, the prompt profile of those rules the route wrote with (see rps.prompting).
    `package_version` is the version of this package that built the request (the task text
    is not part of the rules version, so this says which text the row was written from);
    it is part of the label only for a row a model wrote.
    """

    provider: str
    model: str
    route: str
    effort: str | None
    date: str
    task_id: str
    pool_version: str
    image_size: int | None
    rules_version: str | None = None
    package_version: str | None = None
    rules_profile: str | None = None

    def __post_init__(self) -> None:
        if self.rules_version is not None:
            _require_text(self.rules_version, "rules_version")
        if self.rules_profile is not None:
            _require_text(self.rules_profile, "rules_profile")
        if self.package_version is not None:
            _require_text(self.package_version, "package_version")
        _require_text(self.provider, "provider")
        _require_text(self.model, "model")
        if self.route not in LABEL_ROUTES:
            raise ProvenanceError(f"route must be one of {list(LABEL_ROUTES)}, got {self.route!r}")
        if self.effort is not None:
            _require_text(self.effort, "effort")
        if not isinstance(self.date, str) or not _DATE_PATTERN.match(self.date):
            raise ProvenanceError(f"date must look like YYYY-MM-DD, got {self.date!r}")
        if not isinstance(self.task_id, str) or not TASK_ID_PATTERN.match(self.task_id):
            raise ProvenanceError(f"task_id is not valid: {self.task_id!r}")
        _require_text(self.pool_version, "pool_version")
        if self.image_size is not None and (
            isinstance(self.image_size, bool) or not isinstance(self.image_size, int) or self.image_size < 1
        ):
            raise ProvenanceError(f"image_size must be a positive integer or None, got {self.image_size!r}")

    def to_dict(self) -> dict[str, Any]:
        label = {
            "provider": self.provider,
            "model": self.model,
            "route": self.route,
            "effort": self.effort,
            "date": self.date,
            "task_id": self.task_id,
            "pool_version": self.pool_version,
            "image_size": self.image_size,
        }
        if self.rules_version is not None:
            label["rules_version"] = self.rules_version
        if self.rules_profile is not None:
            label["rules_profile"] = self.rules_profile
        if self.package_version is not None:
            label["package_version"] = self.package_version
        return label

    @classmethod
    def from_dict(cls, data: Any) -> "WriterLabel":
        fields = ("provider", "model", "route", "effort", "date", "task_id", "pool_version", "image_size")
        if not isinstance(data, Mapping):
            raise ProvenanceError("writer label is not an object")
        missing = [name for name in fields if name not in data]
        extra = sorted(set(data) - set(fields) - {"rules_version", "rules_profile", "package_version"})
        if missing or extra:
            raise ProvenanceError(f"writer label fields are wrong (missing {missing}, unexpected {extra})")
        values = {name: data[name] for name in fields}
        return cls(
            **values,
            rules_version=data.get("rules_version"),
            package_version=data.get("package_version"),
            rules_profile=data.get("rules_profile"),
        )


@dataclass(frozen=True)
class SourceLabel:
    """Where a row's subject came from: dataset, item (seed or picture) id, license."""

    dataset: str
    item_id: str
    license: str

    def __post_init__(self) -> None:
        _require_text(self.dataset, "dataset")
        _require_text(self.item_id, "item_id")
        _require_text(self.license, "license")

    def to_dict(self) -> dict[str, Any]:
        return {"dataset": self.dataset, "item_id": self.item_id, "license": self.license}

    @classmethod
    def from_dict(cls, data: Any) -> "SourceLabel":
        fields = ("dataset", "item_id", "license")
        if not isinstance(data, Mapping):
            raise ProvenanceError("source label is not an object")
        missing = [name for name in fields if name not in data]
        extra = sorted(set(data) - set(fields))
        if missing or extra:
            raise ProvenanceError(f"source label fields are wrong (missing {missing}, unexpected {extra})")
        return cls(**{name: data[name] for name in fields})


def reserved_keys_in(model_row: Mapping[str, Any]) -> list[str]:
    """Names of reserved keys that a model put in its row."""
    return [key for key in RESERVED_ROW_KEYS if key in model_row]


def stamp_row(model_row: Mapping[str, Any], *, row_id: str, writer: WriterLabel, source: SourceLabel) -> dict[str, Any]:
    """Return the model's row with the runner's labels added.

    Reserved keys in the model's row are dropped, never trusted.
    """
    if not isinstance(model_row, Mapping):
        raise ProvenanceError("a row must be an object")
    if not isinstance(writer, WriterLabel) or not isinstance(source, SourceLabel):
        raise ProvenanceError("a row needs a WriterLabel and a SourceLabel")
    _require_text(row_id, "row_id")
    stamped = {key: value for key, value in model_row.items() if key not in RESERVED_ROW_KEYS}
    stamped["row_id"] = row_id
    stamped["writer"] = writer.to_dict()
    stamped["source_label"] = source.to_dict()
    return stamped


def human_writer_label(*, task_id: str, date: str, pool_version: str, image_size: int | None = None) -> WriterLabel:
    """The writer label for a row the owner wrote or rewrote by hand."""
    return WriterLabel(
        provider=HUMAN_PROVIDER,
        model=HUMAN_MODEL,
        route=HUMAN_ROUTE,
        effort=None,
        date=date,
        task_id=task_id,
        pool_version=pool_version,
        image_size=image_size,
    )


def stamp_human_row(
    row: Mapping[str, Any],
    *,
    row_id: str,
    task_id: str,
    source: SourceLabel,
    date: str,
    pool_version: str,
    image_size: int | None = None,
) -> dict[str, Any]:
    """Stamp a hand-written row with the human writer label."""
    writer = human_writer_label(task_id=task_id, date=date, pool_version=pool_version, image_size=image_size)
    return stamp_row(row, row_id=row_id, writer=writer, source=source)


def is_human_row(row: Mapping[str, Any]) -> bool:
    """True when the row carries the human writer label."""
    writer = row.get("writer") if isinstance(row, Mapping) else None
    return (
        isinstance(writer, Mapping)
        and writer.get("provider") == HUMAN_PROVIDER
        and writer.get("model") == HUMAN_MODEL
        and writer.get("route") == HUMAN_ROUTE
    )


def require_label(row: Any) -> None:
    """Raise ProvenanceError unless the row carries a complete writer label.

    Also checks the source label and row id, which are stamped together with it.
    """
    if not isinstance(row, Mapping):
        raise ProvenanceError("a row must be an object")
    _require_text(row.get("row_id"), "row_id")
    WriterLabel.from_dict(row.get("writer"))
    SourceLabel.from_dict(row.get("source_label"))


def has_label(row: Any) -> bool:
    try:
        require_label(row)
    except ProvenanceError:
        return False
    return True


def rows_to_lines(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """JSON lines for a file of rows. Refuses any row without its labels."""
    lines = []
    for row in rows:
        require_label(row)
        lines.append(json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=False, allow_nan=False))
    return lines
