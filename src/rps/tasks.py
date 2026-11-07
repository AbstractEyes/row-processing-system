"""Task definitions: what to write, from which source, through which route.

A task is a batch of spec lines from one source, written through one route for one
purpose. Planners produce `TaskSpec` objects; the controller records each as a
`task_created` event the first time it sees it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import ids
from .config import PURPOSES, ConfigError
from .layout import NAME_PATTERN
from .routing import SOURCE_CLASSES
from .store.base import NotFound, Store


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


def parse_task_specs(text: str, where: str) -> list[TaskSpec]:
    """The tasks in the text of a tasks file (a JSON list; see `TaskSpec.from_dict`). `where` names the file in errors."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"cannot read {where}: {error}") from None
    if not isinstance(data, list):
        raise ConfigError(f"{where} must hold a list of tasks")
    for index, item in enumerate(data):
        if not isinstance(item, Mapping):
            raise ConfigError(f"{where}: task {index} must be an object")
    return [TaskSpec.from_dict(item) for item in data]


def load_task_specs(path: str | Path) -> list[TaskSpec]:
    """Read a JSON file holding a list of tasks (see `TaskSpec.from_dict`)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read tasks file {str(path)!r}: {error}") from None
    return parse_task_specs(text, f"tasks file {str(path)!r}")


class FilePlanner:
    """A planner that reads tasks from a JSON file each time it is asked.

    The controller ignores tasks it has already recorded, so the file can grow
    between passes.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __call__(self, state: Any) -> list[TaskSpec]:
        return load_task_specs(self.path)


class StorePlanner:
    """A planner that reads the tasks file from the state store (a bucket or a local store).

    The file is read through the store each time the planner is asked, so tasks can be added
    from outside while a run goes on and the run holds nothing in memory. A file that does not
    exist means no tasks (it may be added later). With `min_interval_seconds` above zero the
    planner reads at most that often and, in between, hands back what it read last: a store
    behind a network is not asked several times a second. `clock` returns monotonic seconds.
    """

    def __init__(
        self,
        store: Store,
        path: str,
        *,
        min_interval_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.path = path
        self.min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._last_read = float("-inf")
        self._last_specs: list[TaskSpec] = []

    def __call__(self, state: Any) -> list[TaskSpec]:
        now = self._clock()
        if self.min_interval_seconds > 0 and now - self._last_read < self.min_interval_seconds:
            return list(self._last_specs)
        self._last_read = now
        try:
            text = self.store.read_text(self.path)
        except NotFound:
            self._last_specs = []
            return []
        specs = parse_task_specs(text, f"tasks file {self.path!r} in the state store")
        self._last_specs = specs
        return list(specs)
