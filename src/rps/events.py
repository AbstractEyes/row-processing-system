"""An append-only event log: one JSON object per line.

Every change to the run's state is an event. Replaying the events rebuilds the
state exactly (see `rps.state`), which is what lets a run stop at any moment and
continue later. Each controller run writes its own files, so no file ever has two
writers. Long runs roll to a new part file when a file grows past a size limit.

Line format: {"ts": ..., "actor": ..., "kind": ..., "payload": {...}}

Event kinds and their payloads:

    controller_started   run, version, config digest
    controller_stopped   run, reason, counts
    task_created         task_id, source, batch, variant, route, purpose, effort,
                         pool_version, lines [{line_id, spec, item_id, image, image_class, license}]
    call_prepared        call_id, task_id, line, attempt, route, purpose, est_usd,
                         est_input_tokens, max_output_tokens, image_size, feedback, retry_of,
                         rules_version and rules_profile (the version and the prompt profile of the
                         writing rules the call is built from, when rules are set), repair_of (only on
                         a call of the repair step: the row it writes again; its purpose is `repair`,
                         its feedback holds the reviewers' notes and then the row as it was written,
                         and its route is the route the row was sent back to, which is not always the
                         route of the row's task: see `repair.routes`)
    budget_refused       call_id, route, purpose, est_usd, reason, and the spend and caps
    line_screened        task_id, line, item_id, route, class (other), reason (a spec line the rule
                         set's screen set aside before its first call: no call was made for it, and
                         it is counted apart from `row_set_aside`, which is a writer's own skip or the
                         assessor's set-aside of a written row)
    batch_submitted      handle_id, handle, route, call_ids (recorded before any wait)
    call_returned        call_id, handle_id, usage, usd, price_table_date, minutes, rows, skips,
                         tier (the price tier the call was billed at, when the route has tiers),
                         repair_of (only on a call of the repair step: the row it wrote again; each
                         row the call returns is linked to that row in the state)
    call_failed          call_id, handle_id, error, retryable, billed, usage, usd, minutes,
                         kind (why it failed: see rps.failures), tier, output (the start of an
                         unusable answer), unconverted_rows (the rows of a writer's wrapper that
                         could not be turned into stored rows, as written)
    batch_collected      handle_id, returned, failed
    route_halted         route, kind, reason, call_id (a rejected request or key stops the route
                         for the rest of the run)
    cache_created        route, key, name, expires, model, tokens, created or extended_from (a
                         provider-side prompt cache a writer made or kept alive, remembered so a
                         restart reuses it)
    cache_storage        route, purpose, key, name, model, tokens, hours, rate, usd, from, to,
                         price_table_date (the storage fee for the hours a cache was kept, a
                         ledger line of its own)
    row_assessed         row_id, call_id, passed, flags, errors
    row_rejected         row_id, call_id, by (assessor or reviewers), reason, errors, retry
    row_set_aside        call_id, row_id (optional), class, reason
    packet_published     packet_id, reviewer_id, family, brief_version,
                         items [{row_id, image, round, audit, rules_version, rules_profile}] (the
                         rules each row was written with, from its writer label; packet.json itself
                         stays blind to them)
    packet_withdrawn     packet_id, reviewer_id, row_ids (a packet addressed to a reviewer who is now
                         switched off, or no longer in the configuration, and who has pushed no verdict
                         file: its files are deleted from the state store and each of its rows
                         waits for a review again, to go to a reviewer that is on; a verdict file that
                         turns up for it later is ignored)
    verdict_received     packet_id, row_id, reviewer_id, family, verdict, reasons, notes
    row_escalated        row_id, call_id, reason (in_question or audit_reject), round, families_used
    row_to_hand          row_id, call_id, reason, verdicts (the owner's hand queue)
    row_reopened         row_id, call_id, reason (a row that went to the hand queue only for want of a
                         reviewer waits for a review again, when the review policy as it stands now
                         still asks for one and the current configuration has a reviewer for it: a
                         first review when the row never had one, else a further one; a row the owner
                         has resolved, a row the current policy settles, and a row sent to the hand
                         queue for any other reason are never reopened)
    repair_requested     row_id, call_id, route (a row in the hand queue for a fix verdict is sent back,
                         once, to the route that is to write it again: the route that wrote it, or the
                         route `repair.routes` names for that route, and `route` is that repairing route.
                         The row leaves the hand queue and is `repairing`, and `call_id` names the call
                         that will write it again, which is prepared on `route` with `repair_of` set to
                         the row; the rows that call writes are written by `route` and linked to the row
                         by their `repair_of` in the state, never in a stored or shipped row. A row whose
                         repair comes to nothing, or whose repairs were switched off, goes back to the
                         hand queue with a `row_to_hand` that says why)
    row_accepted         row_id, call_id, by, reason
    hand_resolved        row_id, call_id, decision (accept, discard, replace), row (for replace)
    packet_burned        packet_id, row_ids, deleted (pictures deleted from the shared store)
    shipped              task_id, route, row_ids, path, count
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .layout import Actor, events_path, parse_events_path, sort_event_paths
from .store.base import NotFound, Store

EVENT_KINDS: tuple[str, ...] = (
    "controller_started",
    "controller_stopped",
    "task_created",
    "call_prepared",
    "budget_refused",
    "line_screened",
    "batch_submitted",
    "call_returned",
    "call_failed",
    "batch_collected",
    "route_halted",
    "cache_created",
    "cache_storage",
    "row_assessed",
    "row_rejected",
    "row_set_aside",
    "packet_published",
    "packet_withdrawn",
    "verdict_received",
    "row_escalated",
    "row_to_hand",
    "row_reopened",
    "repair_requested",
    "row_accepted",
    "hand_resolved",
    "packet_burned",
    "shipped",
)

# Events that must be durable in the store before the run does anything else.
# A submitted batch id is recorded before any wait, so a restart polls the batch
# instead of submitting the same calls again.
DURABLE_KINDS: frozenset[str] = frozenset({"batch_submitted"})


class EventError(ValueError):
    """An event that cannot be written or read."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Event:
    ts: str
    actor: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_obj(self) -> dict[str, Any]:
        return {"ts": self.ts, "actor": self.actor, "kind": self.kind, "payload": self.payload}

    def to_line(self) -> str:
        """One line of JSON, with no line break inside."""
        try:
            return json.dumps(self.to_obj(), ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise EventError(f"event {self.kind!r} payload is not valid JSON data: {error}") from None

    @classmethod
    def from_obj(cls, obj: Any) -> "Event":
        if not isinstance(obj, dict):
            raise EventError("event is not an object")
        for key, kind in (("ts", str), ("actor", str), ("kind", str), ("payload", dict)):
            if not isinstance(obj.get(key), kind):
                raise EventError(f"event field {key!r} is missing or not a {kind.__name__}")
        return cls(ts=obj["ts"], actor=obj["actor"], kind=obj["kind"], payload=obj["payload"])

    @classmethod
    def from_line(cls, line: str) -> "Event":
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as error:
            raise EventError(f"not valid JSON: {error.msg}") from None
        return cls.from_obj(obj)


@dataclass(frozen=True)
class ReplayProblem:
    """A line that replay skipped.

    `torn` is True for an unreadable final line in a file that does not end with a
    newline, which is what an interrupted append leaves behind.
    """

    path: str
    line_number: int
    reason: str
    torn: bool
    excerpt: str


@dataclass
class Replay:
    """The events read from the log, and anything that was skipped."""

    events: list[Event] = field(default_factory=list)
    problems: list[ReplayProblem] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    @property
    def torn(self) -> list[ReplayProblem]:
        return [problem for problem in self.problems if problem.torn]

    def report(self) -> str:
        if not self.problems:
            return f"{len(self.events)} events from {len(self.paths)} file(s), nothing skipped"
        lines = [f"{len(self.events)} events from {len(self.paths)} file(s); skipped {len(self.problems)} line(s):"]
        for problem in self.problems:
            kind = "torn last line" if problem.torn else "unreadable line"
            lines.append(f"  {problem.path}:{problem.line_number}: {kind} ({problem.reason})")
        return "\n".join(lines)


def parse_event_text(text: str, path: str = "") -> tuple[list[Event], list[ReplayProblem]]:
    """Parse the text of one event file, skipping lines that cannot be read."""
    events: list[Event] = []
    problems: list[ReplayProblem] = []
    lines = text.split("\n")
    ends_with_newline = text.endswith("\n")
    last_index = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        try:
            events.append(Event.from_line(line))
        except EventError as error:
            problems.append(
                ReplayProblem(
                    path=path,
                    line_number=index + 1,
                    reason=str(error),
                    torn=(index == last_index and not ends_with_newline),
                    excerpt=line[:80],
                )
            )
    return events, problems


def read_event_file(store: Store, path: str) -> tuple[list[Event], list[ReplayProblem]]:
    try:
        text = store.read_text(path)
    except NotFound:
        return [], []
    return parse_event_text(text, path)


def replay(store: Store, prefix: str = "events/") -> Replay:
    """Read every controller event file in replay order.

    A torn or unreadable line is skipped and listed in `Replay.problems`.
    """
    result = Replay()
    for path in sort_event_paths(store.list(prefix)):
        events, problems = read_event_file(store, path)
        result.events.extend(events)
        result.problems.extend(problems)
        result.paths.append(path)
    return result


class EventLog:
    """Appends events to this run's own files in a store.

    `actor` is the controller run that owns the files. `on_append` is called after
    each event is written (the controller uses it to keep its in-memory state in
    step; tests use it to stop a run at an exact point).
    """

    def __init__(
        self,
        store: Store,
        actor: Actor,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        now: Callable[[], datetime] = utc_now,
        on_append: Callable[[Event], None] | None = None,
    ) -> None:
        if actor.kind != "controller":
            raise EventError("only a controller writes the event log")
        self._store = store
        self.actor = actor
        self._max_bytes = max_bytes
        self._now = now
        self._on_append = on_append
        self._part, self._size = self._find_tail()

    def _find_tail(self) -> tuple[int, int]:
        prefix = f"events/controller-{self.actor.name}."
        parts = []
        for path in self._store.list(prefix):
            parsed = parse_events_path(path)
            if parsed is not None and parsed[0] == self.actor.name:
                parts.append(parsed[1])
        if not parts:
            return 0, 0
        last = max(parts)
        return last, len(self._store.read_bytes(events_path(self.actor.name, last)))

    @property
    def path(self) -> str:
        """The part file currently being written."""
        return events_path(self.actor.name, self._part)

    def append(self, kind: str, payload: dict[str, Any] | None = None, *, durable: bool | None = None) -> Event:
        """Write one event and return it. Durable kinds are flushed to the store at once.

        `durable` says it for this event: True flushes, False does not even for a durable kind, and None
        (the default) leaves it to the kind.
        """
        if kind not in EVENT_KINDS:
            raise EventError(f"unknown event kind: {kind!r}")
        event = Event(ts=format_ts(self._now()), actor=self.actor.label, kind=kind, payload=payload or {})
        line = event.to_line()
        size = len(line.encode("utf-8")) + 1
        if self._size > 0 and self._size + size > self._max_bytes:
            self._part += 1
            self._size = 0
        self._store.append_lines(self.path, [line])
        self._size += size
        if durable if durable is not None else kind in DURABLE_KINDS:
            self._store.flush(force=True)
        if self._on_append is not None:
            self._on_append(event)
        return event


def events_by_kind(events: Iterable[Event], kind: str) -> list[Event]:
    return [event for event in events if event.kind == kind]
