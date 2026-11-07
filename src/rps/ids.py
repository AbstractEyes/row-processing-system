"""Deterministic identifiers for tasks, calls, rows and review packets.

The same inputs always give the same id, on any machine, so a restarted run
finds the work an earlier run already recorded instead of creating it again.

    task_id   "t" + 16 hex characters, from (source, batch, spec line set)
    call_id   "{task_id}:{line}:{attempt}"
    custom_id the call id in a form that provider batch APIs accept
              ("{task_id}_{line}_{attempt}"), reversible
    row_id    "{custom_id}_{index}", safe to use as a file name
    packet_id "p" + 12 hex characters, from the packet's row ids
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

LINE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")
TASK_ID_PATTERN = re.compile(r"^t[0-9a-f]{16}$")
PACKET_ID_PATTERN = re.compile(r"^p[0-9a-f]{12}$")
INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")

# Anthropic batch custom ids allow letters, digits, "_" and "-", up to 64 characters.
CUSTOM_ID_MAX_LENGTH = 64


def canonical_json(obj: Any) -> str:
    """Serialize to JSON with sorted keys and no spaces, so equal data gives equal text."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def check_line_id(line_id: str) -> str:
    """Return the line id if it is valid (letters, digits and "-", up to 32 characters)."""
    if not isinstance(line_id, str) or not LINE_ID_PATTERN.match(line_id):
        raise ValueError(f"invalid spec line id: {line_id!r}")
    return line_id


def normalize_lines(spec_lines: Any) -> list[tuple[str, Any]]:
    """Return the spec lines as (line_id, spec) pairs sorted by line id.

    Accepts a mapping of line id to spec, an iterable of (line_id, spec) pairs,
    or an iterable of objects with `line_id` and `spec` attributes.
    """
    if isinstance(spec_lines, Mapping):
        pairs = list(spec_lines.items())
    else:
        pairs = []
        for item in spec_lines:
            if hasattr(item, "line_id") and hasattr(item, "spec"):
                pairs.append((item.line_id, item.spec))
            else:
                line_id, spec = item
                pairs.append((line_id, spec))
    for line_id, _ in pairs:
        check_line_id(line_id)
    ids = [line_id for line_id, _ in pairs]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate spec line ids")
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def task_id(source: str, batch: str, spec_lines: Any, variant: str = "") -> str:
    """Id of a task, from its source, batch and the set of spec lines.

    The order of the lines does not matter. `variant` is empty by default; give
    each task a distinct variant (for example the route name) when the same
    batch is written through several routes.
    """
    payload = {
        "source": str(source),
        "batch": str(batch),
        "variant": str(variant),
        "lines": [[line_id, spec] for line_id, spec in normalize_lines(spec_lines)],
    }
    return "t" + _digest(payload)[:16]


def check_task_id(value: str) -> str:
    if not isinstance(value, str) or not TASK_ID_PATTERN.match(value):
        raise ValueError(f"invalid task id: {value!r}")
    return value


def call_id(task: str, line: str, attempt: int) -> str:
    """Id of one call: "{task_id}:{line}:{attempt}" with attempt counting from 1."""
    check_task_id(task)
    check_line_id(line)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError(f"attempt must be an integer >= 1, got {attempt!r}")
    return f"{task}:{line}:{attempt}"


def parse_call_id(value: str) -> tuple[str, str, int]:
    """Split a call id into (task_id, line, attempt)."""
    parts = value.split(":") if isinstance(value, str) else []
    if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) < 1:
        raise ValueError(f"invalid call id: {value!r}")
    task, line, attempt_text = parts
    return check_task_id(task), check_line_id(line), int(attempt_text)


def custom_id(call: str) -> str:
    """The call id as a provider custom_id (":" becomes "_").

    Task ids and line ids contain neither ":" nor "_", so the change is reversible.
    """
    task, line, attempt = parse_call_id(call)
    text = f"{task}_{line}_{attempt}"
    if len(text) > CUSTOM_ID_MAX_LENGTH:
        raise ValueError(f"custom id longer than {CUSTOM_ID_MAX_LENGTH} characters: {text!r}")
    return text


def call_id_from_custom_id(value: str) -> str:
    """Inverse of `custom_id`."""
    parts = value.split("_") if isinstance(value, str) else []
    if len(parts) != 3:
        raise ValueError(f"invalid custom id: {value!r}")
    task, line, attempt_text = parts
    if not attempt_text.isdigit():
        raise ValueError(f"invalid custom id: {value!r}")
    return call_id(task, line, int(attempt_text))


def row_id(call: str, index: int) -> str:
    """Id of the index-th row written by a call (file-name safe)."""
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError(f"row index must be an integer >= 0, got {index!r}")
    return f"{custom_id(call)}_{index}"


def parse_row_id(value: str) -> tuple[str, int]:
    """Split a row id into (call_id, index)."""
    head, sep, tail = value.rpartition("_") if isinstance(value, str) else ("", "", "")
    if not sep or not tail.isdigit():
        raise ValueError(f"invalid row id: {value!r}")
    return call_id_from_custom_id(head), int(tail)


def packet_id(keys: Iterable[str]) -> str:
    """Id of a review packet, from the set of keys that identify its contents.

    Callers pass one key per assignment (for example reviewer, row and round) so
    that the same row sent to two reviewers makes two different packets.
    """
    ordered = sorted(set(keys))
    if not ordered:
        raise ValueError("a packet needs at least one row")
    return "p" + _digest(ordered)[:12]


def check_packet_id(value: str) -> str:
    if not isinstance(value, str) or not PACKET_ID_PATTERN.match(value):
        raise ValueError(f"invalid packet id: {value!r}")
    return value


def instance_id(now: datetime | None = None, suffix: str | None = None) -> str:
    """A fresh id for one controller run, such as "20261107T091233Z-ab12".

    Ids sort in time order. Each run writes its own event files named after it.
    """
    moment = now or datetime.now(timezone.utc)
    if suffix is None:
        suffix = secrets.token_hex(2)
    value = f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"
    if not INSTANCE_ID_PATTERN.match(value):
        raise ValueError(f"invalid instance id: {value!r}")
    return value
