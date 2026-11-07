"""Task definitions: what to write, from which source, through which route.

A task is a batch of spec lines from one source, written through one route for one
purpose. Planners produce `TaskSpec` objects; the controller records each as a
`task_created` event the first time it sees it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import ids
from .config import PURPOSES, ConfigError
from .layout import NAME_PATTERN
from .routing import SOURCE_CLASSES


@dataclass(frozen=True)
class SpecLine:
    """One line of a batch's spec.

    `spec` is whatever the writer needs (text or a JSON object). `item_id` is the seed
    or picture id for the source label (the line id when omitted). `image` names the
    line's picture file under the source's images folder. `license` overrides the
    source's license. `image_class` overrides the source's picture class (see
    `rps.routing`): a narration of a COCO picture is COCO class even when its source
    is a collection of narrations.
    """

    line_id: str
    spec: Any
    item_id: str | None = None
    image: str | None = None
    license: str | None = None
    image_class: str | None = None

    def __post_init__(self) -> None:
        ids.check_line_id(self.line_id)
        if self.image_class is not None and self.image_class not in SOURCE_CLASSES:
            raise ConfigError(
                f"spec line {self.line_id}: image_class must be one of {list(SOURCE_CLASSES)}, got {self.image_class!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "spec": self.spec,
            "item_id": self.item_id or self.line_id,
            "image": self.image,
            "license": self.license,
            "image_class": self.image_class,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SpecLine":
        unknown = sorted(set(data) - {"line_id", "spec", "item_id", "image", "license", "image_class"})
        if unknown:
            raise ConfigError(f"spec line: unknown key(s) {unknown}")
        if "line_id" not in data or "spec" not in data:
            raise ConfigError("spec line: 'line_id' and 'spec' are required")
        return cls(
            line_id=data["line_id"],
            spec=data["spec"],
            item_id=data.get("item_id"),
            image=data.get("image"),
            license=data.get("license"),
            image_class=data.get("image_class"),
        )


@dataclass(frozen=True)
class TaskSpec:
    """A batch of spec lines for one source and route.

    `variant` separates tasks that share a source, batch and lines but go through
    different routes (give each the route name).
    """

    source: str
    batch: str
    route: str
    lines: tuple[SpecLine, ...]
    purpose: str = "production"
    variant: str = ""

    def __post_init__(self) -> None:
        if not self.source or not self.batch:
            raise ConfigError("a task needs a source and a batch")
        if not NAME_PATTERN.match(self.route):
            raise ConfigError(f"task route {self.route!r} is not a valid route name")
        if self.purpose not in PURPOSES:
            raise ConfigError(f"task purpose {self.purpose!r} must be one of {list(PURPOSES)}")
        if not self.lines:
            raise ConfigError("a task needs at least one spec line")
        ids.normalize_lines(self.lines)  # checks ids are valid and unique

    @property
    def task_id(self) -> str:
        return ids.task_id(self.source, self.batch, [(line.line_id, line.spec) for line in self.lines], self.variant)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec":
        unknown = sorted(set(data) - {"source", "batch", "route", "lines", "purpose", "variant"})
        if unknown:
            raise ConfigError(f"task: unknown key(s) {unknown}")
        for key in ("source", "batch", "route", "lines"):
            if key not in data:
                raise ConfigError(f"task: {key!r} is required")
        return cls(
            source=str(data["source"]),
            batch=str(data["batch"]),
            route=str(data["route"]),
            lines=tuple(SpecLine.from_dict(item) for item in data["lines"]),
            purpose=str(data.get("purpose", "production")),
            variant=str(data.get("variant", "")),
        )


Planner = Callable[[Any], Sequence[TaskSpec]]


def load_task_specs(path: str | Path) -> list[TaskSpec]:
    """Read a JSON file holding a list of tasks (see `TaskSpec.from_dict`)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read tasks file {str(path)!r}: {error}") from None
    if not isinstance(data, list):
        raise ConfigError(f"tasks file {str(path)!r} must hold a list of tasks")
    return [TaskSpec.from_dict(item) for item in data]


class FilePlanner:
    """A planner that reads tasks from a JSON file each time it is asked.

    The controller ignores tasks it has already recorded, so the file can grow
    between passes.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __call__(self, state: Any) -> list[TaskSpec]:
        return load_task_specs(self.path)
