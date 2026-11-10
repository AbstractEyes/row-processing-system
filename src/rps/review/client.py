"""The local reviewer client.

A reviewer works through packets addressed to it:

    pull      copy the next packet (packet.json and its pictures) into a local folder
    verdict   record one verdict in the reviewer's own local verdict file
    check     validate a verdict file
    push      upload the verdict file, then delete the packet's local pictures (the burn)

Everything the reviewer writes goes to one file per packet, `review/<reviewer>/<packet>.jsonl`;
the store wrapper refuses any other write. The local copy of that file is appended one
verdict at a time as the reviewer works, so a reviewer that stops and starts again
carries on from its own verdict file. The pictures in the shared store belong to the
controller, which deletes them after it has read the verdicts; the client only burns
its local copies, and only after the push succeeded.

Two ways to work, both built on the same calls:

    attended   a person or an agent session runs the CLI (python -m rps.review ...)
    scripted   `run_packet` calls a function (or a command line) once per row
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import layout
from ..layout import reviewer_actor
from ..store.base import ActorStore, NotFound, Store
from ..store.local import LocalStore
from .packets import (
    PacketError,
    normalize_verdict,
    parse_packet,
    parse_verdict_lines,
    validate_verdict,
)


class ReviewError(Exception):
    """A reviewer client operation that cannot be done."""


@dataclass(frozen=True)
class ReviewItem:
    """One row handed to a review function."""

    packet_id: str
    row_id: str
    row: dict[str, Any]
    task: dict[str, Any] | None
    image_path: Path | None
    brief_version: str
    verdict_schema: dict[str, Any]
    # What the packet's reviewer may correct with a fix verdict (empty for a reviewer who may not).
    may_amend: tuple[str, ...] = ()
    amend_values: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def to_json(self) -> str:
        data: dict[str, Any] = {
            "packet_id": self.packet_id,
            "row_id": self.row_id,
            "row": self.row,
            "task": self.task,
            "image_path": str(self.image_path) if self.image_path else None,
            "brief_version": self.brief_version,
            "verdict_schema": self.verdict_schema,
        }
        if self.may_amend:
            data["may_amend"] = list(self.may_amend)
            data["amend_values"] = {key: list(values) for key, values in self.amend_values.items()}
        return json.dumps(data, ensure_ascii=True)


ReviewFn = Callable[[ReviewItem], Mapping[str, Any]]


class CommandReviewer:
    """A review function that runs a command line once per row.

    The command receives the item as JSON on standard input (the picture is named by
    `image_path`) and prints a verdict as JSON on standard output. The command is run
    directly, never through a shell.
    """

    def __init__(self, command: Sequence[str] | str, *, timeout: float = 600.0) -> None:
        if isinstance(command, str):
            command = shlex.split(command, posix=(os.name != "nt"))
        if not command:
            raise ReviewError("the review command is empty")
        self.command = list(command)
        self.timeout = timeout

    def __call__(self, item: ReviewItem) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                self.command, input=item.to_json(), capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReviewError(f"the review command failed to run: {type(error).__name__}") from None
        if completed.returncode != 0:
            raise ReviewError(f"the review command exited with {completed.returncode}: {completed.stderr.strip()[:200]}")
        text = completed.stdout.strip()
        try:
            verdict = json.loads(text.splitlines()[-1]) if text else None
        except json.JSONDecodeError:
            verdict = None
        if not isinstance(verdict, dict):
            raise ReviewError("the review command did not print a verdict as JSON")
        return verdict


def as_review_fn(review: ReviewFn | Sequence[str] | str) -> ReviewFn:
    """Accept a function or a command line."""
    return review if callable(review) else CommandReviewer(review)


@dataclass
class LocalPacket:
    """A packet copied to the local folder."""

    packet_id: str
    reviewer_id: str
    folder: Path
    document: dict[str, Any]

    @property
    def row_ids(self) -> list[str]:
        return [entry["row_id"] for entry in self.document["rows"]]

    @property
    def verdicts_file(self) -> Path:
        return self.folder / "verdicts.jsonl"

    @property
    def may_amend(self) -> tuple[str, ...]:
        """The records the packet lets its reviewer correct (empty: none)."""
        listed = self.document.get("may_amend")
        return tuple(str(key) for key in listed) if isinstance(listed, list) else ()

    @property
    def amend_values(self) -> dict[str, list[str]]:
        values = self.document.get("amend_values")
        return {str(key): list(vals) for key, vals in values.items()} if isinstance(values, dict) and self.may_amend else {}

    def verdict_problems(self, verdict: Any) -> list[str]:
        """The problems with a verdict for a row of this packet, under the packet's own permission to amend."""
        return validate_verdict(verdict, allowed_row_ids=self.row_ids, may_amend=self.may_amend, amend_values=self.amend_values)

    def entry(self, row_id: str) -> dict[str, Any]:
        for entry in self.document["rows"]:
            if entry["row_id"] == row_id:
                return entry
        raise ReviewError(f"row {row_id!r} is not in packet {self.packet_id}")

    def image_path(self, entry: Mapping[str, Any]) -> Path | None:
        name = entry.get("image")
        if not name:
            return None
        path = self.folder / name
        return path if path.exists() else None


def read_local_packet(root: str | Path, packet_id: str) -> LocalPacket | None:
    """The packet copied under `root`, or None if it is not there."""
    folder = Path(root) / packet_id
    path = folder / "packet.json"
    if not path.is_file():
        return None
    try:
        document = parse_packet(path.read_text(encoding="utf-8"))
    except PacketError as error:
        raise ReviewError(f"the local copy of packet {packet_id} is damaged: {error}") from None
    return LocalPacket(packet_id, document["reviewer_id"], folder, document)


@dataclass
class PacketStatus:
    packet_id: str
    total: int
    judged: int
    pushed: bool
    images_left: int

    @property
    def left(self) -> int:
        return self.total - self.judged


@dataclass
class CheckReport:
    packet_id: str
    judged: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every line is valid (rows may still be missing)."""
        return not self.problems

    @property
    def complete(self) -> bool:
        return self.ok and not self.missing


@dataclass
class PushResult:
    packet_id: str
    pushed: bool
    burned: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    packet_id: str
    judged: int = 0
    errors: list[str] = field(default_factory=list)
    pushed: bool = False
    burned: int = 0


class ReviewerClient:
    """Works one reviewer's packets: pull, record verdicts, check, push and burn."""

    def __init__(self, reviewer_id: str, remote: Store | None, root: str | Path, *, fsync: bool = True) -> None:
        self.reviewer_id = layout.check_name(reviewer_id, "reviewer id")
        self.remote: Store | None = ActorStore(remote, reviewer_actor(reviewer_id)) if remote is not None else None
        self.root = Path(root)
        self._local = LocalStore(self.root, fsync=fsync)

    # ------------------------------------------------------------ the shared store

    def _store(self) -> Store:
        if self.remote is None:
            raise ReviewError("no store is configured for this reviewer client")
        return self.remote

    def remote_packet_ids(self) -> list[str]:
        """Packets the controller has published for this reviewer."""
        found = []
        for path in self._store().list(layout.packets_prefix(self.reviewer_id)):
            parsed = layout.parse_packet_json_path(path)
            if parsed is not None and parsed[0] == self.reviewer_id:
                found.append(parsed[1])
        return sorted(found)

    def pushed_packet_ids(self) -> set[str]:
        """Packets this reviewer has already sent verdicts for."""
        pushed = set()
        prefix = layout.review_prefix(self.reviewer_id)
        for path in self._store().list(prefix):
            name = path[len(prefix):]
            if name.endswith(".jsonl") and "/" not in name:
                pushed.add(name[: -len(".jsonl")])
        return pushed

    def pending_packet_ids(self) -> list[str]:
        """Published packets with no verdicts pushed yet."""
        pushed = self.pushed_packet_ids()
        return [pid for pid in self.remote_packet_ids() if pid not in pushed]

    # ------------------------------------------------------------ local packets

    def local_packet(self, packet_id: str) -> LocalPacket | None:
        return read_local_packet(self.root, packet_id)

    def local_packets(self) -> list[LocalPacket]:
        found = []
        if not self.root.is_dir():
            return found
        for child in sorted(self.root.iterdir()):
            if child.is_dir() and (child / "packet.json").exists():
                packet = self.local_packet(child.name)
                if packet is not None:
                    found.append(packet)
        return found

    def require_packet(self, packet_id: str) -> LocalPacket:
        packet = self.local_packet(packet_id)
        if packet is None:
            raise ReviewError(f"packet {packet_id} is not in {self.root}; run pull first")
        if packet.reviewer_id != self.reviewer_id:
            raise ReviewError(f"packet {packet_id} is addressed to {packet.reviewer_id}, not {self.reviewer_id}")
        return packet

    def download(self, packet_id: str) -> tuple[LocalPacket, list[str]]:
        """Copy one packet from the shared store. Returns the packet and any missing picture names."""
        store = self._store()
        text = store.read_text(layout.packet_json_path(self.reviewer_id, packet_id))
        try:
            document = parse_packet(text)
        except PacketError as error:
            raise ReviewError(f"packet {packet_id} is malformed: {error}") from None
        if document["reviewer_id"] != self.reviewer_id or document["packet_id"] != packet_id:
            raise ReviewError(f"packet {packet_id} does not belong to {self.reviewer_id}")
        missing = []
        for entry in document["rows"]:
            image = entry.get("image")
            if not image:
                continue
            remote_path = f"{layout.packet_dir(self.reviewer_id, packet_id)}/{image}"
            try:
                self._local.write_bytes(f"{packet_id}/{image}", store.read_bytes(remote_path))
            except NotFound:
                missing.append(image)
        self._local.write_text(f"{packet_id}/packet.json", text)
        packet = LocalPacket(packet_id, self.reviewer_id, self.root / packet_id, document)
        return packet, missing

    def pull(self) -> tuple[LocalPacket | None, list[str]]:
        """The next packet to work on, copied locally if it is not already.

        A local packet with rows still to judge (or complete but not pushed) comes
        first, so interrupted work is resumed before new work starts. Returns
        (None, []) when nothing is waiting.
        """
        pushed = self.pushed_packet_ids()
        for packet in self.local_packets():
            if packet.reviewer_id == self.reviewer_id and packet.packet_id not in pushed:
                return packet, []
        local_ids = {packet.packet_id for packet in self.local_packets()}
        for packet_id in self.remote_packet_ids():
            if packet_id not in pushed and packet_id not in local_ids:
                return self.download(packet_id)
        return None, []

    # ------------------------------------------------------------ verdicts

    def _read_local_verdicts(self, packet: LocalPacket, path: Path | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        target = path or packet.verdicts_file
        if not target.exists():
            return [], []
        return parse_verdict_lines(target.read_text(encoding="utf-8"))

    def judged_rows(self, packet: LocalPacket) -> dict[str, dict[str, Any]]:
        """Valid verdicts recorded so far, by row id (the first one for a row counts)."""
        verdicts, _ = self._read_local_verdicts(packet)
        judged: dict[str, dict[str, Any]] = {}
        for verdict in verdicts:
            if packet.verdict_problems(verdict) == [] and verdict["row_id"] not in judged:
                judged[verdict["row_id"]] = normalize_verdict(
                    verdict, may_amend=packet.may_amend, amend_values=packet.amend_values
                )
        return judged

    def record_verdict(
        self,
        packet_id: str,
        row_id: str,
        verdict: str,
        reasons: Sequence[str] = (),
        notes: str = "",
        *,
        replace: bool = False,
        amend: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Validate a verdict and append it to the reviewer's own verdict file.

        `amend` ({"shot light": value}) is the correction a reviewer whose packet says `may_amend` may give
        with a fix verdict; it is refused for any other reviewer, and for any other verdict.
        """
        packet = self.require_packet(packet_id)
        record: dict[str, Any] = {"row_id": row_id, "verdict": verdict, "reasons": list(reasons), "notes": notes}
        if amend is not None:
            record["amend"] = dict(amend)
        problems = packet.verdict_problems(record)
        if problems:
            raise ReviewError("; ".join(problems))
        record = normalize_verdict(record, may_amend=packet.may_amend, amend_values=packet.amend_values)
        path = f"{packet_id}/verdicts.jsonl"
        if row_id in self.judged_rows(packet):
            if not replace:
                raise ReviewError(f"row {row_id} already has a verdict; use replace to change it")
            kept = [
                line
                for line in packet.verdicts_file.read_text(encoding="utf-8").split("\n")
                if line.strip() and not _is_row_line(line, row_id)
            ]
            self._local.write_text(path, "".join(line + "\n" for line in kept))
        self._local.append_lines(path, [json.dumps(record, ensure_ascii=True, separators=(",", ":"))])
        return record

    def check(self, packet_id: str, path: str | Path | None = None) -> CheckReport:
        """Validate a verdict file (the reviewer's own, or a hand-written one at `path`)."""
        packet = self.require_packet(packet_id)
        report = CheckReport(packet_id)
        target = Path(path) if path else packet.verdicts_file
        if not target.exists():
            report.missing = packet.row_ids
            report.problems.append(f"no verdict file at {target}")
            return report
        verdicts, line_problems = parse_verdict_lines(target.read_text(encoding="utf-8"))
        report.problems.extend(line_problems)
        seen: set[str] = set()
        for number, verdict in enumerate(verdicts, start=1):
            problems = packet.verdict_problems(verdict)
            if problems:
                report.problems.extend(f"verdict {number}: {problem}" for problem in problems)
                continue
            if verdict["row_id"] in seen:
                report.duplicates.append(verdict["row_id"])
                report.problems.append(f"verdict {number}: row {verdict['row_id']} has more than one verdict")
                continue
            seen.add(verdict["row_id"])
            report.judged.append(verdict["row_id"])
        report.missing = [row_id for row_id in packet.row_ids if row_id not in seen]
        return report

    def status(self) -> list[PacketStatus]:
        """Where each local packet stands."""
        pushed = self.pushed_packet_ids() if self.remote is not None else set()
        result = []
        for packet in self.local_packets():
            images = packet.folder / "images"
            left_images = len([p for p in images.iterdir() if p.is_file()]) if images.is_dir() else 0
            result.append(
                PacketStatus(
                    packet_id=packet.packet_id,
                    total=len(packet.row_ids),
                    judged=len(self.judged_rows(packet)),
                    pushed=packet.packet_id in pushed,
                    images_left=left_images,
                )
            )
        return result

    # ------------------------------------------------------------ push and burn

    def push(self, packet_id: str) -> PushResult:
        """Upload the verdict file, then burn the local pictures.

        Refuses unless every row has a valid verdict. The pictures are deleted only
        after the upload has been flushed, so a failed push leaves them in place.
        """
        packet = self.require_packet(packet_id)
        report = self.check(packet_id)
        if not report.complete:
            reasons = list(report.problems) + [f"row {row_id} has no verdict yet" for row_id in report.missing]
            return PushResult(packet_id, pushed=False, reasons=reasons)
        judged = self.judged_rows(packet)
        text = "".join(
            json.dumps(judged[row_id], ensure_ascii=True, separators=(",", ":")) + "\n" for row_id in packet.row_ids
        )
        store = self._store()
        store.write_text(layout.verdicts_path(self.reviewer_id, packet_id), text)
        store.flush(force=True)
        return PushResult(packet_id, pushed=True, burned=self.burn(packet_id))

    def burn(self, packet_id: str) -> int:
        """Delete the packet's local pictures. Returns how many files were deleted."""
        prefix = f"{packet_id}/images/"
        paths = self._local.list(prefix)
        for path in paths:
            self._local.delete(path)
        images_dir = self.root / packet_id / "images"
        try:
            images_dir.rmdir()
        except OSError:
            pass
        return len(paths)

    # ------------------------------------------------------------ scripted review

    def run_packet(self, packet_id: str, review: ReviewFn | Sequence[str] | str) -> RunResult:
        """Review every unjudged row with a function (or command), then push and burn.

        Each verdict is written as soon as it is made, so a run that stops partway
        resumes from the verdict file without asking again about rows already judged.
        A row whose review fails or gives an invalid verdict is left for the next run.
        """
        review_fn = as_review_fn(review)
        packet = self.require_packet(packet_id)
        result = RunResult(packet_id)
        done = self.judged_rows(packet)
        for row_id in packet.row_ids:
            if row_id in done:
                continue
            entry = packet.entry(row_id)
            item = ReviewItem(
                packet_id=packet_id,
                row_id=row_id,
                row=entry["row"],
                task=entry.get("task"),
                image_path=packet.image_path(entry),
                brief_version=str(packet.document["brief_version"]),
                verdict_schema=packet.document.get("verdict_schema", {}),
                may_amend=packet.may_amend,
                amend_values=packet.amend_values,
            )
            try:
                answer = dict(review_fn(item))
                if answer.setdefault("row_id", row_id) != row_id:
                    raise ReviewError(f"the reviewer answered for {answer['row_id']!r}, not {row_id!r}")
                self.record_verdict(
                    packet_id,
                    row_id,
                    answer.get("verdict", ""),
                    answer.get("reasons", []),
                    answer.get("notes", ""),
                    amend=answer.get("amend"),
                )
                result.judged += 1
            except (ReviewError, PacketError) as error:
                result.errors.append(f"{row_id}: {error}")
        if not result.errors:
            pushed = self.push(packet_id)
            result.pushed, result.burned = pushed.pushed, pushed.burned
        return result

    def run(self, review: ReviewFn | Sequence[str] | str, max_packets: int | None = None) -> list[RunResult]:
        """Work packets one after another until none is waiting (or `max_packets` are done)."""
        results: list[RunResult] = []
        while max_packets is None or len(results) < max_packets:
            packet, _ = self.pull()
            if packet is None:
                break
            result = self.run_packet(packet.packet_id, review)
            results.append(result)
            if not result.pushed:
                break
        return results


def _is_row_line(line: str, row_id: str) -> bool:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("row_id") == row_id
