"""Where every file lives, and who may write it.

All stores share one path layout. Each file has exactly one writer:

    events/controller-<run>.<part>.jsonl            the controller run named in the path
    packets/<reviewer>/<packet>/packet.json         the controller (addressed to that reviewer)
    packets/<reviewer>/<packet>/images/<row>.<ext>  the controller
    review/<reviewer>/<packet>.jsonl                the reviewer named in the path
    accepted/<route>/<task>.jsonl                   the controller
    ledger/...                                      the controller
    hand/queue.jsonl                                the controller (rows waiting for the owner)
    control/...                                     the operator (a stop file, hand decisions)

No actor edits a file it does not own. `ActorStore` (in `rps.store.base`) applies
these rules to every write and delete.

All paths here are relative to the store's root. A storage bucket may be given with a
folder ("owner/name/folder", see `parse_bucket`): the store then keeps every one of these
paths under that folder, so two runs that name different folders of one bucket share no file.
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
_PACKET_JSON = re.compile(r"^packets/(?P<reviewer>[A-Za-z0-9][A-Za-z0-9._-]{0,63})/(?P<packet>p[0-9a-f]{12})/packet\.json$")

HAND_DIR = "hand"
HAND_QUEUE_PATH = f"{HAND_DIR}/queue.jsonl"
CONTROL_STOP_PATH = f"{CONTROL_DIR}/STOP"
CONTROL_HAND_PREFIX = f"{CONTROL_DIR}/hand/"
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


# The directories at the top of the state store's layout.
STATE_DIRS = (EVENTS_DIR, PACKETS_DIR, REVIEW_DIR, ACCEPTED_DIR, LEDGER_DIR, HAND_DIR, CONTROL_DIR)
_BUCKET_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def parse_bucket(value: str) -> tuple[str, str]:
    """Split a `bucket` setting, "owner/name" or "owner/name/folder[/more]", into (bucket id, folder).

    The folder is where every state path lives inside the bucket; "" when none is named, which keeps
    the layout at the bucket's root. Test runs and production can share one bucket by each naming a
    folder of its own. Each folder part is a plain name, and none may be one of the state store's own
    directories, so the files of one folder can never be mistaken for the layout of another store of
    the same bucket (one with no folder, or with a shorter or a longer one).
    """
    if not isinstance(value, str) or not value:
        raise PathError("a bucket is named owner/name, or owner/name/folder")
    parts = value.split("/")
    if len(parts) < 2 or not all(_BUCKET_PART.match(part) for part in parts[:2]):
        raise PathError(f"a bucket is named owner/name, or owner/name/folder; got {value!r}")
    folder = parts[2:]
    for part in folder:
        if not NAME_PATTERN.match(part):
            raise PathError(
                f"the folder in {value!r} has a part {part!r} that is not a plain name "
                "(letters, digits, '.', '_' and '-', up to 64 characters, no empty parts)"
            )
        if part in STATE_DIRS:
            raise PathError(
                f"the folder in {value!r} has a part {part!r}, which is one of the state store's own "
                f"directories ({', '.join(STATE_DIRS)}); give it another name"
            )
    return "/".join(parts[:2]), "/".join(folder)


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


def packets_prefix(reviewer: str) -> str:
    """Everything the controller has published for one reviewer."""
    return f"{PACKETS_DIR}/{check_name(reviewer, 'reviewer id')}/"


def packet_dir(reviewer: str, packet: str) -> str:
    return f"{PACKETS_DIR}/{check_name(reviewer, 'reviewer id')}/{check_packet_id(packet)}"


def packet_json_path(reviewer: str, packet: str) -> str:
    return f"{packet_dir(reviewer, packet)}/packet.json"


def packet_images_prefix(reviewer: str, packet: str) -> str:
    return f"{packet_dir(reviewer, packet)}/images/"


def packet_image_path(reviewer: str, packet: str, row: str, ext: str) -> str:
    check_name(row, "row id")
    if not re.match(r"^[a-z0-9]{2,5}$", ext):
        raise PathError(f"invalid image extension: {ext!r}")
    return f"{packet_dir(reviewer, packet)}/images/{row}.{ext}"


def parse_packet_json_path(path: str) -> tuple[str, str] | None:
    """Return (reviewer id, packet id) for a packet.json path, or None for any other path."""
    match = _PACKET_JSON.match(path)
    if not match:
        return None
    return match.group("reviewer"), match.group("packet")


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
    if top in (PACKETS_DIR, ACCEPTED_DIR, LEDGER_DIR, HAND_DIR) and rest:
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
