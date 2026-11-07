"""Review packets and the verdict format.

A packet is a folder addressed to one reviewer:

    packets/<reviewer>/<packet>/packet.json
    packets/<reviewer>/<packet>/images/<row_id>.<ext>

packet.json holds the rows to review, the review brief version and the verdict
schema; the picture for a row (when it has one) is named by the row's id.

Reviewers are blind to the writer. Each entry in packet.json carries a review view
of the stored row, never the row itself: the writer label is removed and the row's
own `source.model` is blanked. Beside the view goes the task's spec line for the
row (under "task"), so the reviewer can see what the writer was asked to do. The
controller keeps the row id to writer mapping in its own events and matches
verdicts back by row id.

The reviewer returns verdicts only, one JSON line per row in
review/<reviewer>/<packet>.jsonl, and never an edited row. A verdict has exactly
these fields:

    row_id    which row
    verdict   keep, fix or reject
    reasons   short codes (lowercase letters, digits and "_"); required for fix and reject
    notes     free text: name the problem and where it is (which field or thought)
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .. import layout
from ..provenance import require_label
from ..store.base import Store

PACKET_FORMAT = 1
VERDICT_VALUES: tuple[str, ...] = ("keep", "fix", "reject")
REASON_PATTERN = re.compile(r"^[a-z0-9_]{1,40}$")
MAX_REASONS = 10
MAX_NOTES_LENGTH = 2000
VERDICT_FIELDS = ("row_id", "verdict", "reasons", "notes")

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["row_id", "verdict"],
    "properties": {
        "row_id": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICT_VALUES)},
        "reasons": {
            "type": "array",
            "maxItems": MAX_REASONS,
            "items": {"type": "string", "pattern": REASON_PATTERN.pattern},
            "description": "short codes; at least one is required for fix and reject",
        },
        "notes": {
            "type": "string",
            "maxLength": MAX_NOTES_LENGTH,
            "description": "name the problem and where it is (which field or thought)",
        },
    },
}


class PacketError(ValueError):
    """A packet or verdict that is malformed."""


def validate_verdict(obj: Any, allowed_row_ids: Iterable[str] | None = None) -> list[str]:
    """Return the problems with a verdict (empty when it is valid).

    Fields other than the four above are refused: a reviewer returns a verdict, never
    a rewritten row.
    """
    if not isinstance(obj, Mapping):
        return ["the verdict is not an object"]
    problems: list[str] = []
    for key in obj:
        if key not in VERDICT_FIELDS:
            problems.append(f"unexpected field {key!r} (reviewers return verdicts only)")
    row_id = obj.get("row_id")
    if not isinstance(row_id, str) or not row_id:
        problems.append("row_id must be a non-empty string")
    elif allowed_row_ids is not None and row_id not in set(allowed_row_ids):
        problems.append(f"row_id {row_id!r} is not in the packet")
    verdict = obj.get("verdict")
    if verdict not in VERDICT_VALUES:
        problems.append(f"verdict must be one of {list(VERDICT_VALUES)}")
    reasons = obj.get("reasons", [])
    if not isinstance(reasons, list) or len(reasons) > MAX_REASONS:
        problems.append(f"reasons must be a list of at most {MAX_REASONS} codes")
    else:
        for reason in reasons:
            if not isinstance(reason, str) or not REASON_PATTERN.match(reason):
                problems.append(f"reason {reason!r} is not a short code (lowercase letters, digits, '_')")
        if verdict in ("fix", "reject") and not reasons:
            problems.append("a fix or reject verdict needs at least one reason code")
    notes = obj.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES_LENGTH:
        problems.append(f"notes must be text of at most {MAX_NOTES_LENGTH} characters")
    return problems


def normalize_verdict(obj: Mapping[str, Any]) -> dict[str, Any]:
    """A valid verdict with every field present."""
    problems = validate_verdict(obj)
    if problems:
        raise PacketError("; ".join(problems))
    return {
        "row_id": obj["row_id"],
        "verdict": obj["verdict"],
        "reasons": list(obj.get("reasons", [])),
        "notes": obj.get("notes", ""),
    }


def review_view(stored_row: Mapping[str, Any], line: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The blind view of a stored row, as a reviewer sees it.

    The stored row must carry its writer label (a row that does not is refused). The
    view drops the label and blanks the row's own `source.model`; everything else in
    the row stays. `line` is the task's spec line for the row (line id, item id and
    spec, which for seeded items includes the seed id and hint text); it goes under
    "task". Returns {"row_id", "row", "task"}.
    """
    require_label(stored_row)
    row = copy.deepcopy({key: value for key, value in stored_row.items() if key != "writer"})
    source = row.get("source")
    if isinstance(source, dict) and "model" in source:
        source["model"] = ""
    task = None
    if line is not None:
        task = {
            "line_id": line.get("line_id"),
            "item_id": line.get("item_id"),
            "spec": copy.deepcopy(line.get("spec")),
        }
    return {"row_id": stored_row["row_id"], "row": row, "task": task}


@dataclass
class PacketRow:
    """A row in a packet: its review view and picture (bytes while a packet is being built)."""

    view: dict[str, Any]
    image_ext: str | None = None
    image_bytes: bytes | None = field(default=None, repr=False)

    @property
    def row_id(self) -> str:
        return self.view["row_id"]

    @property
    def image_name(self) -> str | None:
        return f"{self.row_id}.{self.image_ext}" if self.image_bytes is not None and self.image_ext else None


@dataclass
class Packet:
    packet_id: str
    reviewer_id: str
    brief_version: str
    created: str
    rows: list[PacketRow]

    def items(self) -> list[dict[str, Any]]:
        """Row ids and picture paths, as recorded in the packet_published event."""
        return [
            {"row_id": item.row_id, "image": f"images/{item.image_name}" if item.image_name else None}
            for item in self.rows
        ]


def build_packet(
    *,
    packet_id: str,
    reviewer_id: str,
    brief_version: str,
    created: str,
    rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None, bytes | None, str | None]],
) -> Packet:
    """Assemble a packet from (stored row, spec line, picture bytes, picture extension) tuples.

    Each stored row is turned into its blind review view (see `review_view`); a row
    without a writer label is refused.
    """
    if not rows:
        raise PacketError("a packet needs at least one row")
    items: list[PacketRow] = []
    seen: set[str] = set()
    for stored_row, line, image_bytes, ext in rows:
        view = review_view(stored_row, line)
        if view["row_id"] in seen:
            raise PacketError(f"row {view['row_id']!r} appears twice in the packet")
        seen.add(view["row_id"])
        items.append(PacketRow(view=view, image_ext=ext if image_bytes is not None else None, image_bytes=image_bytes))
    return Packet(packet_id=packet_id, reviewer_id=reviewer_id, brief_version=brief_version, created=created, rows=items)


def packet_document(packet: Packet) -> dict[str, Any]:
    """The contents of packet.json."""
    return {
        "format": PACKET_FORMAT,
        "packet_id": packet.packet_id,
        "reviewer_id": packet.reviewer_id,
        "brief_version": packet.brief_version,
        "created": packet.created,
        "verdict_schema": VERDICT_SCHEMA,
        "rows": [
            {
                "row_id": item.row_id,
                "row": item.view["row"],
                "task": item.view["task"],
                "image": f"images/{item.image_name}" if item.image_name else None,
            }
            for item in packet.rows
        ],
    }


def packet_files(packet: Packet) -> dict[str, bytes]:
    """Store path -> bytes for everything in a packet. Pictures come first and packet.json
    last, so a packet.json that can be read always has its pictures beside it."""
    files: dict[str, bytes] = {}
    for item in packet.rows:
        if item.image_name and item.image_bytes is not None:
            files[layout.packet_image_path(packet.reviewer_id, packet.packet_id, item.row_id, item.image_ext or "jpg")] = item.image_bytes
    text = json.dumps(packet_document(packet), ensure_ascii=True, indent=1, allow_nan=False)
    files[layout.packet_json_path(packet.reviewer_id, packet.packet_id)] = text.encode("utf-8")
    return files


def publish_packet(store: Store, packet: Packet) -> list[str]:
    """Write a packet into a store; returns the paths written."""
    paths = []
    for path, data in packet_files(packet).items():
        store.write_bytes(path, data)
        paths.append(path)
    return paths


def parse_packet(text: str) -> dict[str, Any]:
    """Parse and check packet.json; returns the document."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise PacketError(f"packet.json is not valid JSON: {error.msg}") from None
    if not isinstance(document, dict) or document.get("format") != PACKET_FORMAT:
        raise PacketError("packet.json has an unknown format")
    for key in ("packet_id", "reviewer_id", "brief_version", "rows"):
        if key not in document:
            raise PacketError(f"packet.json is missing {key!r}")
    rows = document["rows"]
    if not isinstance(rows, list) or not rows:
        raise PacketError("packet.json has no rows")
    for entry in rows:
        if not isinstance(entry, dict) or "row_id" not in entry or "row" not in entry:
            raise PacketError("packet.json has a malformed row entry")
    return document


def parse_verdict_lines(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Read a verdict file: (JSON objects, problems). A torn last line is skipped and reported."""
    verdicts: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"line {number}: not valid JSON")
            continue
        if not isinstance(obj, dict):
            problems.append(f"line {number}: not an object")
            continue
        verdicts.append(obj)
    return verdicts, problems
