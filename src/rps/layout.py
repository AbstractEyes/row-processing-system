"""Where every file lives, and who may write it.

All stores share one path layout. Each file has exactly one writer:

    events/controller-<run>.<part>.jsonl   the controller run named in the path
    packets/<packet>/packet.json           the controller
    packets/<packet>/images/<row>.<ext>    the controller
    review/<reviewer>/<packet>.jsonl       the reviewer named in the path
    accepted/<route>/<task>.jsonl          the controller
    ledger/...                             the controller
    control/...                            the operator (for example a stop file)

No actor edits a file it does not own. `ActorStore` (in `rps.store.base`) applies
these rules to every write and delete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .ids import check_packet_id, check_task_id

EVENTS_DIR = "events"
PACKETS_DIR = "packets"
REVIEW_DIR = "review"
ACCEPTED_DIR = "accepted"
LEDGER_DIR = "ledger"
CONTROL_DIR = "control"

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_INSTANCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_EVENT_FILE = re.compile(r"^events/controller-(?P<instance>[A-Za-z0-9][A-Za-z0-9-]{0,63})\.(?P<part>\d{4})\.jsonl$")

CONTROL_STOP_PATH = f"{CONTROL_DIR}/STOP"
LEDGER_SUMMARY_PATH = f"{LEDGER_DIR}/summary.json"
LEDGER_TASKS_PATH = f"{LEDGER_DIR}/tasks.jsonl"


class PathError(ValueError):
    """A store path that is not a clean relative path."""


def normalize_path(path: str) -> str:
    """Return the path in canonical form, or raise PathError.

    Paths are relative, use "/" and contain no empty, "." or ".." segments.
    """
    if not isinstance(path, str) or not path:
        raise PathError("path must be a non-empty string")
    if "\\" in path or "\x00" in path:
        raise PathError(f"path contains a backslash or NUL: {path!r}")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise PathError(f"path must be relative: {path!r}")
    segments = path.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise PathError(f"path has an empty, '.' or '..' segment: {path!r}")
    return path


def check_name(value: str, what: str = "name") -> str:
    """Validate a route name or reviewer id used inside a path."""
    if not isinstance(value, str) or not NAME_PATTERN.match(value):
        raise PathError(f"invalid {what}: {value!r}")
    return value


def events_path(instance: str, part: int = 0) -> str:
    if not _INSTANCE_PATTERN.match(instance or ""):
        raise PathError(f"invalid run id: {instance!r}")
    if not isinstance(part, int) or part < 0 or part > 9999:
        raise PathError(f"invalid event part: {part!r}")
    return f"{EVENTS_DIR}/controller-{instance}.{part:04d}.jsonl"


def parse_events_path(path: str) -> tuple[str, int] | None:
    """Return (run id, part) for an event file path, or None for any other path."""
    match = _EVENT_FILE.match(path)
    if not match:
        return None
    return match.group("instance"), int(match.group("part"))


def sort_event_paths(paths: Iterable[str]) -> list[str]:
    """Event files in replay order: by run id (which sorts by start time), then part."""
    parsed = []
    for path in paths:
        key = parse_events_path(path)
        if key is not None:
            parsed.append((key, path))
    parsed.sort()
    return [path for _, path in parsed]


def packet_dir(packet: str) -> str:
    return f"{PACKETS_DIR}/{check_packet_id(packet)}"


def packet_json_path(packet: str) -> str:
    return f"{packet_dir(packet)}/packet.json"


def packet_images_prefix(packet: str) -> str:
    return f"{packet_dir(packet)}/images/"


def packet_image_path(packet: str, row: str, ext: str) -> str:
    check_name(row, "row id")
    if not re.match(r"^[a-z0-9]{2,5}$", ext):
        raise PathError(f"invalid image extension: {ext!r}")
    return f"{packet_dir(packet)}/images/{row}.{ext}"


def verdicts_path(reviewer: str, packet: str) -> str:
    check_name(reviewer, "reviewer id")
    return f"{REVIEW_DIR}/{reviewer}/{check_packet_id(packet)}.jsonl"


def review_prefix(reviewer: str | None = None) -> str:
    if reviewer is None:
        return f"{REVIEW_DIR}/"
    return f"{REVIEW_DIR}/{check_name(reviewer, 'reviewer id')}/"


def accepted_path(route: str, task: str) -> str:
    check_name(route, "route name")
    check_task_id(task)
    return f"{ACCEPTED_DIR}/{route}/{task}.jsonl"


@dataclass(frozen=True)
class Actor:
    """Who is writing: a controller run, a reviewer, or the operator."""

    kind: str
    name: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("controller", "reviewer", "operator"):
            raise ValueError(f"unknown actor kind: {self.kind!r}")
        if self.kind == "controller" and not _INSTANCE_PATTERN.match(self.name):
            raise ValueError(f"invalid run id: {self.name!r}")
        if self.kind == "reviewer" and not NAME_PATTERN.match(self.name):
            raise ValueError(f"invalid reviewer id: {self.name!r}")

    @property
    def label(self) -> str:
        """The name recorded in the `actor` field of events."""
        return "operator" if self.kind == "operator" else f"{self.kind}-{self.name}"


def controller_actor(instance: str) -> Actor:
    return Actor("controller", instance)


def reviewer_actor(reviewer_id: str) -> Actor:
    return Actor("reviewer", reviewer_id)


def operator_actor() -> Actor:
    return Actor("operator")


@dataclass(frozen=True)
class Owner:
    """The writer a path belongs to. An empty name means any actor of that kind."""

    kind: str
    name: str = ""


def owner_of(path: str) -> Owner | None:
    """The only writer allowed to write the path, or None if the path is unowned.

    Packets, accepted rows and the ledger belong to the controller as a whole (any
    run may write them). Event files belong to the run named in the file name.
    Review files belong to the reviewer named in the path.
    """
    path = normalize_path(path)
    parsed = parse_events_path(path)
    if parsed is not None:
        return Owner("controller", parsed[0])
    top, _, rest = path.partition("/")
    if top in (PACKETS_DIR, ACCEPTED_DIR, LEDGER_DIR) and rest:
        return Owner("controller")
    if top == REVIEW_DIR and rest:
        reviewer, _, tail = rest.partition("/")
        if tail and NAME_PATTERN.match(reviewer):
            return Owner("reviewer", reviewer)
        return None
    if top == CONTROL_DIR and rest:
        return Owner("operator")
    return None


def may_write(actor: Actor, path: str) -> bool:
    """True when the actor owns the path."""
    owner = owner_of(path)
    if owner is None or owner.kind != actor.kind:
        return False
    return owner.name == "" or owner.name == actor.name
