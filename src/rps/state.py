"""Run state, rebuilt by replaying events.

`State.apply(event)` folds one event into the state. Applying the same events twice
gives the same state: every handler ignores an event whose effect is already
recorded (a task or call that exists, an outcome that is set, a status that has
already moved on). Events that are malformed or of an unknown kind are counted and
skipped, so replay never stops on one bad line.

Row life cycle (statuses):

    written -> passed -> in_review -> accepted
                 |           |    \\-> in_question -> in_review ...
                 |           |    \\-> hand -> accepted / rejected / replaced
                 |           |            \\-> passed or in_question again (row_reopened, see below)
                 |           |            \\-> repairing -> repaired (repair_requested, see below)
                 |           \\-> rejected (discarded by reviewers)
                 \\-> rejected (assessor failure, may be retried) / set_aside

Two events return a row to waiting for a review. `row_reopened` takes a row out of the hand queue
(one sent there only for want of a reviewer): it becomes `passed` when it never had a review and
`in_question` when it did. `packet_withdrawn` takes back a packet its reviewer never answered: each
row of it is as it was before the packet was published to it, `passed` for a first review and
`in_question` for a further one, and the withdrawn reviewer no longer counts among those that
reviewed the row.

The repair step. `repair_requested` takes a row out of the hand queue (one sent there for a fix verdict) and
sends it back, once, to the route that is to write it again (the route that wrote it, or the one `repair.routes`
names for that route; the event records which): it is `repairing`. The call that writes it again is an
ordinary call of the same spec line, prepared with `repair_of` (the row) on that route, but it is kept out of
the line's own attempts: a line's retries are about the line's own calls, and a line can have several rows
being repaired at once. The rows that call writes carry `repair_of`, are checked and reviewed like any row,
and the row becomes `repaired` (it never ships) as soon as one of them passes the checker. A row whose repair
comes to nothing (the call failed, the writer wrote no row, or every row it wrote failed the checker) goes
back to the hand queue with a `row_to_hand` that says why, and is never sent back again; where a line stands
is then read from the rows that repaired it, as if they were the row.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from . import ids
from .events import Event
from .failures import KIND_TRUNCATED
from .ids import canonical_json
from .repair import repair_feedback

# "repaired" is the end of a row that was written again: it never ships, and what stands for it is the rows its repair wrote.
TERMINAL_ROW_STATUSES = frozenset({"accepted", "rejected", "set_aside", "repaired"})
WAITING_ROW_STATUSES = frozenset({"written", "failed", "passed", "in_review", "in_question", "hand", "repairing"})
TERMINAL_LINE_OUTCOMES = frozenset({"accepted", "set_aside", "rejected", "exhausted", "screened"})


@dataclass
class LineState:
    """One spec line of a task. `ordinal` is its position in the task (the runner's
    per-call choices rotate on it); `image_class` is the class of its picture's origin."""

    line_id: str
    spec: Any
    item_id: str | None = None
    image: str | None = None
    license: str | None = None
    image_class: str | None = None
    ordinal: int = 0


@dataclass
class TaskState:
    task_id: str
    source: str
    batch: str
    variant: str
    route: str
    purpose: str
    effort: str | None
    pool_version: str
    created: str
    dataset: str = ""
    lines: dict[str, LineState] = field(default_factory=dict)


@dataclass
class CallState:
    call_id: str
    task_id: str
    line: str
    attempt: int
    route: str
    purpose: str
    est_usd: float = 0.0
    est_input_tokens: int = 0
    max_output_tokens: int = 0
    image_size: int | None = None
    feedback: list[str] = field(default_factory=list)
    retry_of: str | None = None
    # The row a call of the repair step writes again (None on every other call). Such a call is not one of its
    # line's own attempts: it is kept out of `State._lines` and found through the row's `repair` record.
    repair_of: str | None = None
    status: str = "prepared"  # prepared, submitted, returned, failed
    handle_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    usd: float = 0.0
    price_table_date: str | None = None
    minutes: float = 0.0
    error: str | None = None
    retryable: bool = True
    billed: bool = False
    kind: str | None = None
    tier: str | None = None
    row_ids: list[str] = field(default_factory=list)
    skips: list[dict[str, Any]] = field(default_factory=list)
    skips_recorded: bool = False
    prepared_ts: str = ""
    finished_ts: str = ""


@dataclass
class Assignment:
    """A row sent to one reviewer. `open` until that reviewer's verdict arrives."""

    packet_id: str
    reviewer_id: str
    family: str
    round: int
    audit: bool = False
    open: bool = True


@dataclass
class RowState:
    row_id: str
    call_id: str
    task_id: str
    line: str
    route: str
    record: dict[str, Any]
    status: str = "written"
    flags: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rejection: dict[str, Any] | None = None
    assignments: list[Assignment] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    reopened: bool = False
    escalations: int = 0
    hand: dict[str, Any] | None = None
    replaced_by: str | None = None
    accepted_by: str | None = None
    # How many packets this row was taken back from (see the `packet_withdrawn` event). It goes into the
    # id of the row's next packets, so a packet sent after a withdrawal never has the withdrawn one's id.
    withdrawals: int = 0
    # The repair step. On a row that was sent back to be written again (status `repairing`, then `repaired`, or `hand`
    # again): {"call_id" (the call planned for it), "route" (the route that writes it again: its own, or the one
    # `repair.routes` names for it), "calls" (the repair calls prepared for it, in order), "rows" (the rows its
    # repair wrote), "failed" (why it is in the hand queue again, when it is)}.
    repair: dict[str, Any] | None = None
    # On a row a repair call wrote: the id of the row that was sent back to be written again. This link lives in
    # the events and the state; it is never a column of a stored or shipped row.
    repair_of: str | None = None
    # How many rows from the same writer provider passed assessment before this one.
    # Consecutive rows get consecutive first reviewers, and the number comes from the
    # event history alone, so a restart picks the same reviewer for the same row.
    ordinal: int | None = None

    @property
    def flagged(self) -> bool:
        """True when the assessor flagged the row (a flag puts a row in question)."""
        return bool(self.flags)

    def open_assignments(self, *, audit: bool | None = None) -> list[Assignment]:
        return [a for a in self.assignments if a.open and (audit is None or a.audit == audit)]

    def main_verdicts(self) -> list[dict[str, Any]]:
        return [v for v in self.verdicts if not v.get("audit")]

    def families_used(self) -> set[str]:
        """Families of every reviewer the row has been sent to (open or finished)."""
        return {a.family for a in self.assignments}

    def reviewers_used(self) -> set[str]:
        return {a.reviewer_id for a in self.assignments}


@dataclass
class BatchState:
    handle_id: str
    route: str
    handle: dict[str, Any]
    call_ids: list[str]
    submitted_ts: str
    collected: bool = False
    recovered: bool = False


@dataclass
class PacketState:
    packet_id: str
    reviewer_id: str
    family: str
    brief_version: str
    created: str
    items: list[dict[str, Any]] = field(default_factory=list)
    burned: set[str] = field(default_factory=set)
    withdrawn: bool = False  # taken back before its reviewer answered; its verdicts are never read


@dataclass(frozen=True)
class LineStatus:
    """Where a spec line stands. `outcome` is one of:

    new, retry (a new call is due), in_flight, active (rows still moving),
    accepted, set_aside, rejected, exhausted, screened (set aside by the rule set's screen before
    any call; `set_aside` is a line whose writer or assessor set it aside after a call).

    A call of the repair step that is due is described the same way, with outcome `repair` and `repair_of`
    set to the row it writes again; `feedback` then holds the reviewers' notes and the row as it was written,
    and `route` the route the call goes to. These items come from `State.repairs_needing_calls`, not from
    `State.line_status`.
    """

    outcome: str
    next_attempt: int = 1
    retry_of: str | None = None
    feedback: tuple[str, ...] = ()
    boost: bool = False  # the retry follows a truncated answer and gets a larger token limit
    repair_of: str | None = None  # the call is a repair: it writes this row again
    # The route a call of the repair step goes to: the route the row was sent back to (`repair_requested` names it,
    # and it is not always the route of the row's task: see `repair.routes`). None for every other call, which goes
    # to the route of its task.
    route: str | None = None


@dataclass(frozen=True)
class Spend:
    actual: float = 0.0
    reserved: float = 0.0
    calls: int = 0
    failed: int = 0

    @property
    def total(self) -> float:
        """Money spent plus money held for calls still in flight."""
        return self.actual + self.reserved


def _unique(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def _feedback_from(rows: Iterable[RowState]) -> tuple[str, ...]:
    lines: list[str] = []
    for row in rows:
        for text in (row.rejection or {}).get("errors", []) or []:
            lines.append(str(text)[:300])
    return tuple(_unique(lines)[:20])


class State:
    """Everything the run knows, rebuilt from events."""

    def __init__(self) -> None:
        self.tasks: dict[str, TaskState] = {}
        self.calls: dict[str, CallState] = {}
        self.rows: dict[str, RowState] = {}
        self.batches: dict[str, BatchState] = {}
        self.packets: dict[str, PacketState] = {}
        self.refusals: dict[str, dict[str, Any]] = {}
        self.halts: list[dict[str, Any]] = []
        self.caches: dict[tuple[str, str], dict[str, Any]] = {}
        self.storage: list[dict[str, Any]] = []  # cache storage charges, one line each
        self._storage_seen: set[tuple[str, str, str]] = set()
        self.runs: list[dict[str, Any]] = []
        self._passed_by_provider: dict[str, int] = {}
        self.shipped_files: dict[tuple[str, str], tuple[str, ...]] = {}
        self.unknown_kinds: dict[str, int] = {}
        self.bad_events: list[tuple[str, str]] = []
        self.events_applied = 0
        self._spend: dict[tuple[str, str], list[float]] = {}
        self._lines: dict[tuple[str, str], list[str]] = {}
        # The attempt numbers of the calls of the repair step that were planned or prepared for a line: they are
        # not among the line's own attempts (`_lines`), but no later call of the line may take their numbers.
        self._repair_attempts: dict[tuple[str, str], set[int]] = {}
        self._screened: dict[tuple[str, str], dict[str, Any]] = {}  # (task id, line id) -> class, reason, route

    # ------------------------------------------------------------------ building

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> "State":
        state = cls()
        for event in events:
            state.apply(event)
        return state

    def apply(self, event: Event) -> None:
        """Fold one event into the state. Safe to apply the same event again."""
        handler = self._HANDLERS.get(event.kind)
        self.events_applied += 1
        if handler is None:
            self.unknown_kinds[event.kind] = self.unknown_kinds.get(event.kind, 0) + 1
            return
        try:
            handler(self, event)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            self.bad_events.append((event.kind, f"{type(error).__name__}: {error}"))

    # ------------------------------------------------------------------ handlers

    def _on_controller_started(self, event: Event) -> None:
        self.runs.append({"ts": event.ts, "started": True, **event.payload})

    def _on_controller_stopped(self, event: Event) -> None:
        self.runs.append({"ts": event.ts, "stopped": True, **event.payload})

    def _on_task_created(self, event: Event) -> None:
        p = event.payload
        if p["task_id"] in self.tasks:
            return
        lines = {}
        for ordinal, item in enumerate(p["lines"]):
            lines[item["line_id"]] = LineState(
                line_id=item["line_id"],
                spec=item.get("spec"),
                item_id=item.get("item_id"),
                image=item.get("image"),
                license=item.get("license"),
                image_class=item.get("image_class"),
                ordinal=ordinal,
            )
        self.tasks[p["task_id"]] = TaskState(
            task_id=p["task_id"],
            source=p["source"],
            batch=p["batch"],
            variant=p.get("variant", ""),
            route=p["route"],
            purpose=p["purpose"],
            effort=p.get("effort"),
            pool_version=p["pool_version"],
            created=event.ts,
            dataset=p.get("dataset") or p["source"],
            lines=lines,
        )

    def _spend_cell(self, route: str, purpose: str) -> list[float]:
        return self._spend.setdefault((route, purpose), [0.0, 0.0, 0, 0])

    def _on_call_prepared(self, event: Event) -> None:
        p = event.payload
        call_id = p["call_id"]
        if call_id in self.calls:
            return
        call = CallState(
            call_id=call_id,
            task_id=p["task_id"],
            line=p["line"],
            attempt=int(p["attempt"]),
            route=p["route"],
            purpose=p["purpose"],
            est_usd=float(p.get("est_usd", 0.0)),
            est_input_tokens=int(p.get("est_input_tokens", 0)),
            max_output_tokens=int(p.get("max_output_tokens", 0)),
            image_size=p.get("image_size"),
            feedback=list(p.get("feedback", [])),
            retry_of=p.get("retry_of"),
            repair_of=p.get("repair_of"),
            prepared_ts=event.ts,
        )
        self.calls[call_id] = call
        if call.repair_of is None:
            attempts = self._lines.setdefault((call.task_id, call.line), [])
            attempts.append(call_id)
            attempts.sort(key=lambda cid: self.calls[cid].attempt)
        else:
            # A repair call is one of the row's own, not an attempt of the line.
            self._repair_attempts.setdefault((call.task_id, call.line), set()).add(call.attempt)
            original = self.rows.get(call.repair_of)
            if original is not None and original.repair is not None and call_id not in original.repair["calls"]:
                original.repair["calls"].append(call_id)
        self._spend_cell(call.route, call.purpose)[1] += call.est_usd
        self.refusals.pop(call_id, None)

    def _on_budget_refused(self, event: Event) -> None:
        p = event.payload
        if p["call_id"] in self.calls:
            return
        self.refusals[p["call_id"]] = dict(p)

    def _on_line_screened(self, event: Event) -> None:
        p = event.payload
        key = (p["task_id"], p["line"])
        task = self.tasks.get(key[0])
        if key in self._screened or task is None or key[1] not in task.lines or self._lines.get(key):
            return  # already recorded, not a line of the state, or a line that has a call (the call decides where it stands)
        self._screened[key] = {
            "route": str(p.get("route") or task.route),
            "class": str(p.get("class", "other")),
            "reason": str(p.get("reason", "")),
        }

    def _on_batch_submitted(self, event: Event) -> None:
        p = event.payload
        if p["handle_id"] in self.batches:
            return
        batch = BatchState(
            handle_id=p["handle_id"],
            route=p["route"],
            handle=dict(p.get("handle", {})),
            call_ids=list(p["call_ids"]),
            submitted_ts=event.ts,
            recovered=bool(p.get("recovered", False)),
        )
        self.batches[batch.handle_id] = batch
        for call_id in batch.call_ids:
            call = self.calls.get(call_id)
            if call is not None and call.status == "prepared":
                call.status = "submitted"
                call.handle_id = batch.handle_id

    def _finish_call(self, call: CallState, p: dict[str, Any], status: str, ts: str) -> None:
        call.status = status
        call.handle_id = p.get("handle_id", call.handle_id)
        call.usage = dict(p.get("usage", {}))
        call.usd = float(p.get("usd", 0.0))
        call.price_table_date = p.get("price_table_date")
        call.minutes = float(p.get("minutes", 0.0))
        call.tier = p.get("tier")
        call.finished_ts = ts
        cell = self._spend_cell(call.route, call.purpose)
        cell[1] = max(0.0, cell[1] - call.est_usd)
        cell[0] += call.usd
        cell[2] += 1

    def _on_call_returned(self, event: Event) -> None:
        p = event.payload
        call = self.calls.get(p["call_id"])
        if call is None or call.status in ("returned", "failed"):
            return
        self._finish_call(call, p, "returned", event.ts)
        call.skips = [dict(item) for item in p.get("skips", [])]
        flags_by_row = p.get("row_flags", {})
        for record in p.get("rows", []):
            row_id = record["row_id"]
            if row_id in self.rows:
                continue
            self.rows[row_id] = RowState(
                row_id=row_id,
                call_id=call.call_id,
                task_id=call.task_id,
                line=call.line,
                route=call.route,
                record=record,
                flags=list(flags_by_row.get(row_id, [])),
                repair_of=call.repair_of,
            )
            call.row_ids.append(row_id)
        if call.repair_of is not None:
            original = self.rows.get(call.repair_of)
            if original is not None and original.repair is not None:
                original.repair["rows"] = list(call.row_ids)

    def _on_call_failed(self, event: Event) -> None:
        p = event.payload
        call = self.calls.get(p["call_id"])
        if call is None or call.status in ("returned", "failed"):
            return
        self._finish_call(call, p, "failed", event.ts)
        call.error = str(p.get("error", ""))
        call.retryable = bool(p.get("retryable", True))
        call.billed = bool(p.get("billed", False))
        call.kind = p.get("kind")
        self._spend_cell(call.route, call.purpose)[3] += 1

    def _on_batch_collected(self, event: Event) -> None:
        batch = self.batches.get(event.payload["handle_id"])
        if batch is not None:
            batch.collected = True

    def _on_route_halted(self, event: Event) -> None:
        p = event.payload
        route = p["route"]
        if any(h.get("call_id") == p.get("call_id") and h.get("route") == route for h in self.halts):
            return
        self.halts.append({"ts": event.ts, **p})

    def _on_cache_created(self, event: Event) -> None:
        p = event.payload
        self.caches[(p["route"], p["key"])] = dict(p)

    def _on_cache_storage(self, event: Event) -> None:
        """A storage fee for the hours a provider-side cache was kept: spent money, with a line of its own."""
        p = event.payload
        route, purpose, usd = p["route"], p["purpose"], float(p["usd"])  # read everything before changing anything
        marker = (route, p["name"], p["to"])  # one charge per stretch of a cache's life
        if marker in self._storage_seen:
            return
        self._storage_seen.add(marker)
        self.storage.append({**p, "ts": event.ts})
        self._spend_cell(route, purpose)[0] += usd

    def _on_row_assessed(self, event: Event) -> None:
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None or row.status != "written":
            return
        row.flags = _unique(list(row.flags) + [str(flag) for flag in p.get("flags", [])])
        row.errors = [str(error) for error in p.get("errors", [])]
        row.status = "passed" if p["passed"] else "failed"
        if p["passed"]:
            provider = str(row.record.get("writer", {}).get("provider", ""))
            row.ordinal = self._passed_by_provider.get(provider, 0)
            self._passed_by_provider[provider] = row.ordinal + 1
            if row.repair_of is not None:
                # The row it was written again from is done with: its repair has a row that passed the checker.
                original = self.rows.get(row.repair_of)
                if original is not None and original.status == "repairing":
                    original.status = "repaired"

    def _on_row_rejected(self, event: Event) -> None:
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None or row.status in TERMINAL_ROW_STATUSES:
            return
        row.status = "rejected"
        row.rejection = {
            "by": p.get("by", ""),
            "reason": p.get("reason", ""),
            "errors": [str(error) for error in p.get("errors", [])],
            "retry": bool(p.get("retry", False)),
        }

    def _on_row_set_aside(self, event: Event) -> None:
        p = event.payload
        row_id = p.get("row_id")
        if row_id:
            row = self.rows.get(row_id)
            if row is not None and row.status not in TERMINAL_ROW_STATUSES:
                row.status = "set_aside"
                row.rejection = {"by": "assessor", "reason": str(p.get("reason", "")), "errors": [], "retry": False}
            return
        call = self.calls.get(p["call_id"])
        if call is not None:
            call.skips_recorded = True

    def _on_packet_published(self, event: Event) -> None:
        p = event.payload
        if p["packet_id"] in self.packets:
            return
        packet = PacketState(
            packet_id=p["packet_id"],
            reviewer_id=p["reviewer_id"],
            family=p["family"],
            brief_version=str(p.get("brief_version", "")),
            created=event.ts,
            items=[dict(item) for item in p["items"]],
        )
        self.packets[packet.packet_id] = packet
        for item in packet.items:
            row = self.rows.get(item["row_id"])
            if row is None:
                continue
            row.assignments.append(
                Assignment(
                    packet_id=packet.packet_id,
                    reviewer_id=packet.reviewer_id,
                    family=packet.family,
                    round=int(item.get("round", 1)),
                    audit=bool(item.get("audit", False)),
                )
            )
            if not item.get("audit") and row.status in ("passed", "in_question"):
                row.status = "in_review"

    def _on_packet_withdrawn(self, event: Event) -> None:
        """A packet its reviewer never answered was taken back.

        Each row of the packet that was still waiting on it is put back as it was before the packet
        was published to it: the assignment is dropped (the reviewer, and so its family, no longer
        counts among those that reviewed the row), a row in review for the first time is `passed`
        again and one in review for a further reviewer is `in_question` again, and an audit that was
        out with the reviewer is simply due again. A packet is withdrawn once; an assignment that was
        answered stays, with its verdict.
        """
        packet = self.packets.get(event.payload["packet_id"])
        if packet is None or packet.withdrawn:
            return
        packet.withdrawn = True
        for item in packet.items:
            row = self.rows.get(item["row_id"])
            if row is None:
                continue
            kept = [a for a in row.assignments if not (a.open and a.packet_id == packet.packet_id)]
            if len(kept) == len(row.assignments):
                continue
            row.assignments = kept
            row.withdrawals += 1
            if row.status == "in_review" and not row.open_assignments(audit=False):
                row.status = "in_question" if any(not a.audit for a in kept) else "passed"

    def _on_verdict_received(self, event: Event) -> None:
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None:
            return
        assignment = next(
            (a for a in row.assignments if a.open and a.packet_id == p["packet_id"] and a.reviewer_id == p["reviewer_id"]),
            None,
        )
        if assignment is None:
            return
        assignment.open = False
        row.verdicts.append(
            {
                "reviewer_id": p["reviewer_id"],
                "family": p["family"],
                "verdict": p["verdict"],
                "reasons": list(p.get("reasons", [])),
                "notes": str(p.get("notes", "")),
                "packet_id": p["packet_id"],
                "audit": assignment.audit,
                "round": assignment.round,
            }
        )

    def _on_row_escalated(self, event: Event) -> None:
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None or row.status not in ("in_review", "accepted"):
            return
        if row.status == "accepted" and p.get("reason") != "audit_reject":
            return
        row.status = "in_question"
        row.escalations += 1
        if p.get("reason") == "audit_reject":
            row.reopened = True

    def _on_row_to_hand(self, event: Event) -> None:
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None or row.status not in ("passed", "in_review", "in_question", "repairing"):
            return
        if row.status == "repairing" and row.repair is not None:
            # A row sent back to its writer whose repair came to nothing: it keeps the record of what was tried.
            row.repair = {**row.repair, "failed": str(p.get("reason", ""))}
        row.status = "hand"
        row.hand = {"reason": str(p.get("reason", "")), "ts": event.ts}

    def _on_repair_requested(self, event: Event) -> None:
        """A row in the hand queue is sent back, once, to the route that wrote it, to be written again.

        The row leaves the hand queue (`repairing`) and its repair is on record from now on: it is never sent
        back a second time, whatever happens to the repair. The call planned for it is named by the event; the
        number it carries is reserved for the row's line from here on.
        """
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None or row.status != "hand" or row.repair is not None:
            return
        call_id, route = str(p["call_id"]), str(p["route"])
        attempt = ids.parse_call_id(call_id)[2]  # read first: a call id that cannot be read changes nothing
        self._repair_attempts.setdefault((row.task_id, row.line), set()).add(attempt)
        row.status = "repairing"
        row.hand = None
        row.repair = {"call_id": call_id, "route": route, "calls": [], "rows": []}

    def _on_row_reopened(self, event: Event) -> None:
        """A row in the hand queue (and not resolved by the owner) is waiting for a review again.

        It is `passed` when it never had a review, so the first review picks it up, and `in_question`
        when it did, so a further reviewer does. Nothing else about the row changes: its verdicts,
        flags and assignments stay. (`RowState.reopened` is another thing: an accepted row an audit
        reviewer rejected.)
        """
        row = self.rows.get(event.payload["row_id"])
        if row is None or row.status != "hand":
            return
        row.status = "in_question" if any(not a.audit for a in row.assignments) else "passed"
        row.hand = None

    def _on_row_accepted(self, event: Event) -> None:
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None or row.status not in ("in_review", "in_question"):
            return
        row.status = "accepted"
        row.accepted_by = str(p.get("by", "reviewers"))

    def _on_hand_resolved(self, event: Event) -> None:
        p = event.payload
        row = self.rows.get(p["row_id"])
        if row is None or row.status != "hand":
            return
        decision = p["decision"]
        row.hand = {**(row.hand or {}), "decision": decision, "resolved_ts": event.ts}
        if decision == "accept":
            row.status = "accepted"
            row.accepted_by = "hand"
        elif decision == "discard":
            row.status = "rejected"
            row.rejection = {"by": "hand", "reason": "discarded by the owner", "errors": [], "retry": False}
        elif decision == "replace":
            new_record = p["row"]
            new_id = new_record["row_id"]
            row.status = "rejected"
            row.rejection = {"by": "hand", "reason": "rewritten by the owner", "errors": [], "retry": False}
            row.replaced_by = new_id
            if new_id not in self.rows:
                call = self.calls.get(row.call_id)
                self.rows[new_id] = RowState(
                    row_id=new_id,
                    call_id=row.call_id,
                    task_id=row.task_id,
                    line=row.line,
                    route=row.route,
                    record=new_record,
                    status="accepted",
                    accepted_by="hand",
                )
                if call is not None:
                    call.row_ids.append(new_id)
        else:
            raise ValueError(f"unknown hand decision: {decision!r}")

    def _on_packet_burned(self, event: Event) -> None:
        packet = self.packets.get(event.payload["packet_id"])
        if packet is not None:
            packet.burned.update(event.payload.get("row_ids", []))

    def _on_shipped(self, event: Event) -> None:
        p = event.payload
        self.shipped_files[(p["task_id"], p["route"])] = tuple(sorted(p["row_ids"]))

    _HANDLERS: dict[str, Callable[["State", Event], None]] = {
        "controller_started": _on_controller_started,
        "controller_stopped": _on_controller_stopped,
        "task_created": _on_task_created,
        "call_prepared": _on_call_prepared,
        "budget_refused": _on_budget_refused,
        "line_screened": _on_line_screened,
        "batch_submitted": _on_batch_submitted,
        "call_returned": _on_call_returned,
        "call_failed": _on_call_failed,
        "batch_collected": _on_batch_collected,
        "route_halted": _on_route_halted,
        "cache_created": _on_cache_created,
        "cache_storage": _on_cache_storage,
        "row_assessed": _on_row_assessed,
        "row_rejected": _on_row_rejected,
        "row_set_aside": _on_row_set_aside,
        "packet_published": _on_packet_published,
        "packet_withdrawn": _on_packet_withdrawn,
        "verdict_received": _on_verdict_received,
        "row_escalated": _on_row_escalated,
        "row_to_hand": _on_row_to_hand,
        "row_reopened": _on_row_reopened,
        "repair_requested": _on_repair_requested,
        "row_accepted": _on_row_accepted,
        "hand_resolved": _on_hand_resolved,
        "packet_burned": _on_packet_burned,
        "shipped": _on_shipped,
    }

    # ------------------------------------------------------------------ spend

    def spend(self, route: str, purpose: str | None = None) -> Spend:
        """Money spent (and held for calls in flight) on a route, optionally for one purpose."""
        actual = reserved = 0.0
        calls = failed = 0
        for (cell_route, cell_purpose), cell in self._spend.items():
            if cell_route == route and (purpose is None or cell_purpose == purpose):
                actual += cell[0]
                reserved += cell[1]
                calls += int(cell[2])
                failed += int(cell[3])
        return Spend(actual=actual, reserved=reserved, calls=calls, failed=failed)

    def storage_usd(self, route: str, purpose: str | None = None) -> float:
        """Money spent keeping provider-side caches on a route, optionally for one purpose."""
        return sum(
            float(line["usd"]) for line in self.storage if line["route"] == route and (purpose is None or line["purpose"] == purpose)
        )

    def recompute_spend(self) -> dict[tuple[str, str], Spend]:
        """Spend rebuilt from the call and storage records alone (used to check the running totals)."""
        cells: dict[tuple[str, str], list[float]] = {}
        for call in self.calls.values():
            cell = cells.setdefault((call.route, call.purpose), [0.0, 0.0, 0, 0])
            if call.status in ("returned", "failed"):
                cell[0] += call.usd
                cell[2] += 1
                if call.status == "failed":
                    cell[3] += 1
            else:
                cell[1] += call.est_usd
        for line in self.storage:
            cells.setdefault((line["route"], line["purpose"]), [0.0, 0.0, 0, 0])[0] += float(line["usd"])
        return {key: Spend(actual=c[0], reserved=c[1], calls=int(c[2]), failed=int(c[3])) for key, c in cells.items()}

    # ------------------------------------------------------------------ queries: calls and batches

    def pending_calls(self, route: str | None = None) -> list[CallState]:
        """Calls prepared but not yet submitted (after a restart these are submitted again)."""
        return [c for c in self.calls.values() if c.status == "prepared" and (route is None or c.route == route)]

    def submitted_calls(self) -> list[CallState]:
        return [c for c in self.calls.values() if c.status == "submitted"]

    def unfinished_calls(self, route: str) -> int:
        """How many calls on a route are prepared or submitted and not yet answered."""
        return sum(1 for c in self.calls.values() if c.route == route and c.status in ("prepared", "submitted"))

    def batches_to_poll(self, route: str | None = None) -> list[BatchState]:
        """Submitted batches with at least one call still waiting for its result."""
        waiting = []
        for batch in self.batches.values():
            if batch.collected or (route is not None and batch.route != route):
                continue
            if any(self.calls[cid].status == "submitted" for cid in batch.call_ids if cid in self.calls):
                waiting.append(batch)
        return waiting

    def calls_with_unrecorded_skips(self) -> list[CallState]:
        return [c for c in self.calls.values() if c.status == "returned" and c.skips and not c.skips_recorded]

    # ------------------------------------------------------------------ queries: lines

    def next_attempt(self, task_id: str, line: str) -> int:
        """The attempt number of the next call for a spec line: one more than any call made, or planned, for it.

        The calls of the repair step count here too, so that no call of the line ever takes the number of
        another. Without any repair this is one more than the line's latest attempt.
        """
        used = [self.calls[call_id].attempt for call_id in self._lines.get((task_id, line), [])]
        used.extend(self._repair_attempts.get((task_id, line), ()))
        return max(used, default=0) + 1

    def line_status(self, task_id: str, line: str, max_attempts: int) -> LineStatus:
        attempts = self._lines.get((task_id, line), [])
        if not attempts:
            if (task_id, line) in self._screened:
                return LineStatus("screened")
            return LineStatus("new", next_attempt=1)
        latest = self.calls[attempts[-1]]
        if latest.status in ("prepared", "submitted"):
            return LineStatus("in_flight")
        if latest.status == "failed":
            if latest.retryable and latest.attempt < max_attempts:
                truncated = latest.kind == KIND_TRUNCATED
                # A cut-off answer needs room, not advice; any other billed failure tells the writer what went wrong.
                feedback = (latest.error,) if latest.billed and latest.error and not truncated else ()
                return LineStatus("retry", self.next_attempt(task_id, line), latest.call_id, feedback, boost=truncated)
            return LineStatus("exhausted")
        rows = [self.rows[row_id] for row_id in latest.row_ids if row_id in self.rows]
        rows = self._through_repairs(rows)
        if not rows:
            return LineStatus("set_aside")
        if any(row.status not in TERMINAL_ROW_STATUSES for row in rows):
            return LineStatus("active")
        if any(row.status == "accepted" for row in rows):
            return LineStatus("accepted")
        retry_rows = [row for row in rows if row.status == "rejected" and (row.rejection or {}).get("retry")]
        if retry_rows and latest.attempt < max_attempts:
            return LineStatus(
                "retry", self.next_attempt(task_id, line), latest.call_id, _feedback_from(retry_rows)
            )
        if retry_rows:
            return LineStatus("exhausted")
        if any(row.status == "rejected" for row in rows):
            return LineStatus("rejected")
        return LineStatus("set_aside")

    def _through_repairs(self, rows: list[RowState]) -> list[RowState]:
        """The rows of a call, with each row that was written again replaced by the rows its repair wrote.

        A row that is `repaired` is done with and never ships; where its line stands depends on the rows that
        took its place. Any other row, a row being repaired included, stands for itself.
        """
        result: list[RowState] = []
        for row in rows:
            written = (
                [self.rows[row_id] for row_id in (row.repair or {}).get("rows", []) if row_id in self.rows]
                if row.status == "repaired"
                else []
            )
            result.extend(written or [row])
        return result

    def is_screened(self, task_id: str, line: str) -> bool:
        """True when the rule set's screen set the line aside before any call."""
        return (task_id, line) in self._screened

    def screened_lines(self) -> dict[tuple[str, str], dict[str, Any]]:
        """The lines the screen set aside before any call, by (task id, line id): their route, class and reason."""
        return {key: dict(value) for key, value in self._screened.items()}

    def lines_needing_calls(self, max_attempts: int) -> list[tuple[TaskState, LineState, LineStatus]]:
        """Spec lines that are due a call: never called, or a retry is due.

        The calls of the repair step that are due come first (`repairs_needing_calls`): a row waiting to be
        written again is further along than a line not yet written, and a one-call route takes calls in order.
        """
        due = self.repairs_needing_calls(max_attempts)
        for task in self.tasks.values():
            for line in task.lines.values():
                status = self.line_status(task.task_id, line.line_id, max_attempts)
                if status.outcome in ("new", "retry"):
                    due.append((task, line, status))
        return due

    # ------------------------------------------------------------------ queries: the repair step

    def rows_being_repaired(self) -> list[RowState]:
        """Rows sent back to their writer whose repair is not over yet, in row-id order."""
        return sorted(self.rows_with_status("repairing"), key=lambda row: row.row_id)

    def _repair_progress(self, row: RowState, max_attempts: int) -> tuple[str, Any]:
        """Where the repair of a row stands: ("due", LineStatus) when a call is due, ("failed", (code, detail))
        when it has come to nothing, else ("waiting", None).

        The first call is the one the row's `repair_requested` planned. A repair call that failed in a way a
        retry may mend (the same ones a line's own calls are retried for) is made again, as a new attempt
        that carries the same notes and row, while the row has had fewer than `max_attempts` calls. A call that
        came back counts only through its rows: the repair is over when none of them passed the checker.
        """
        record = row.repair or {}
        route = record.get("route")  # the route the row was sent back to, as the event recorded it
        calls = [self.calls[call_id] for call_id in record.get("calls", []) if call_id in self.calls]
        if not calls:
            attempt = ids.parse_call_id(record["call_id"])[2]
            status = LineStatus("repair", next_attempt=attempt, feedback=repair_feedback(row), repair_of=row.row_id, route=route)
            return "due", status
        last = calls[-1]
        if last.status in ("prepared", "submitted"):
            return "waiting", None
        if last.status == "failed":
            if last.retryable and len(calls) < max_attempts:
                status = LineStatus(
                    "repair",
                    next_attempt=self.next_attempt(row.task_id, row.line),
                    retry_of=last.call_id,
                    feedback=tuple(last.feedback),
                    boost=last.kind == KIND_TRUNCATED,
                    repair_of=row.row_id,
                    route=route,
                )
                return "due", status
            return "failed", ("call_failed", last.kind or last.error or "")
        written = [self.rows[row_id] for row_id in last.row_ids if row_id in self.rows]
        if not written:
            return "failed", ("no_row", "")
        if all(item.status in ("rejected", "set_aside") for item in written):
            return "failed", ("checker", "")
        return "waiting", None

    def repairs_needing_calls(self, max_attempts: int) -> list[tuple[TaskState, LineState, LineStatus]]:
        """The calls of the repair step that are due: a row sent back to its writer whose call has not been
        made yet, or has to be made again. Each comes as (task, line, status) like a spec line that is due,
        with `status.repair_of` set to the row, in row-id order."""
        due = []
        for row in self.rows_being_repaired():
            outcome, status = self._repair_progress(row, max_attempts)
            task = self.tasks.get(row.task_id)
            line = task.lines.get(row.line) if task is not None else None
            if outcome == "due" and task is not None and line is not None:
                due.append((task, line, status))
        return due

    def repairs_failed(self, max_attempts: int) -> list[tuple[RowState, str, str]]:
        """The rows whose repair has come to nothing, as (row, code, detail), in row-id order.

        `code` is `call_failed` (the repair call failed and may not be tried again; `detail` is the kind of
        failure), `no_row` (the writer answered with no row) or `checker` (every row it wrote was rejected or
        set aside by the checker). Such a row goes back to the hand queue; a repair is never tried twice.
        """
        failed = []
        for row in self.rows_being_repaired():
            outcome, detail = self._repair_progress(row, max_attempts)
            if outcome == "failed":
                failed.append((row, detail[0], detail[1]))
        return failed

    def job_progress(self, max_attempts: int) -> dict[str, int]:
        """Counts of lines by outcome, plus totals, for progress reports."""
        counts: dict[str, int] = {}
        total = 0
        for task in self.tasks.values():
            for line in task.lines.values():
                total += 1
                outcome = self.line_status(task.task_id, line.line_id, max_attempts).outcome
                counts[outcome] = counts.get(outcome, 0) + 1
        done = sum(counts.get(name, 0) for name in TERMINAL_LINE_OUTCOMES)
        return {"lines_total": total, "lines_done": done, **{f"lines_{k}": v for k, v in sorted(counts.items())}}

    # ------------------------------------------------------------------ queries: rows

    def rows_with_status(self, *statuses: str) -> list[RowState]:
        return [row for row in self.rows.values() if row.status in statuses]

    def rows_awaiting_assessment(self) -> list[RowState]:
        return self.rows_with_status("written")

    def rows_failed_unrejected(self) -> list[RowState]:
        """Rows the assessor failed but whose rejection is not yet recorded."""
        return self.rows_with_status("failed")

    def rows_awaiting_first_review(self) -> list[RowState]:
        """Assessed rows that have not been sent to any reviewer."""
        return [row for row in self.rows.values() if row.status == "passed" and not row.assignments]

    def rows_in_question(self) -> list[RowState]:
        """Rows that need a further reviewer and have none assigned."""
        return [row for row in self.rows.values() if row.status == "in_question" and not row.open_assignments(audit=False)]

    def rows_awaiting_review(self) -> list[RowState]:
        """Every row that has passed assessment and is not yet settled by reviewers."""
        return self.rows_with_status("passed", "in_review", "in_question")

    def rows_awaiting_decision(self) -> list[RowState]:
        """Rows whose assigned reviewers have all answered, ready to be settled."""
        return [
            row
            for row in self.rows.values()
            if row.status == "in_review" and not row.open_assignments(audit=False) and row.main_verdicts()
        ]

    def rows_with_audit_reject(self) -> list[RowState]:
        """Accepted rows an audit reviewer rejected (they go back in question)."""
        return [
            row
            for row in self.rows.values()
            if row.status == "accepted"
            and not row.reopened
            and any(v["audit"] and v["verdict"] == "reject" for v in row.verdicts)
        ]

    def rows_needing_audit(self, selected: Callable[[str], bool]) -> list[RowState]:
        """Accepted rows chosen for audit that have not been sent to an audit reviewer."""
        return [
            row
            for row in self.rows.values()
            if row.status == "accepted"
            and not any(a.audit for a in row.assignments)
            and row.accepted_by == "reviewers"
            and selected(row.row_id)
        ]

    def rows_awaiting_acceptance(self) -> list[RowState]:
        """Alias for rows whose verdicts are in and need a decision."""
        return self.rows_awaiting_decision()

    def hand_queue(self) -> list[RowState]:
        return self.rows_with_status("hand")

    def accepted_rows_by_file(self) -> dict[tuple[str, str], list[RowState]]:
        """Accepted rows grouped by (task id, route), in row-id order."""
        groups: dict[tuple[str, str], list[RowState]] = {}
        for row in self.rows.values():
            if row.status == "accepted":
                groups.setdefault((row.task_id, row.route), []).append(row)
        for rows in groups.values():
            rows.sort(key=lambda row: row.row_id)
        return groups

    def files_to_ship(
        self, staged: Mapping[tuple[str, str], tuple[str, ...]] | None = None
    ) -> list[tuple[str, str]]:
        """(task id, route) pairs whose accepted rows differ from what was last shipped.

        `staged` holds the row ids of files already handed to the ship store and not yet sent. While a
        file waits there it is compared with those ids instead: a file waiting for the store's next
        commit is not handed over again, and one whose waiting content is out of date is.
        """
        staged = staged or {}
        wanted = {key: tuple(row.row_id for row in rows) for key, rows in self.accepted_rows_by_file().items()}
        held = {**self.shipped_files, **staged}
        keys = set(wanted) | {key for key, ids in held.items() if ids} | set(staged)
        return sorted(key for key in keys if wanted.get(key, ()) != held.get(key, ()))

    def rows_to_ship(self) -> list[RowState]:
        """Accepted rows not in the file last shipped for their task."""
        shipped = {row_id for ids in self.shipped_files.values() for row_id in ids}
        return [row for row in self.rows.values() if row.status == "accepted" and row.row_id not in shipped]

    # ------------------------------------------------------------------ queries: packets

    def open_packets(self) -> list[PacketState]:
        """Packets with at least one verdict still outstanding."""
        open_ids = {a.packet_id for row in self.rows.values() for a in row.assignments if a.open}
        return [self.packets[pid] for pid in self.packets if pid in open_ids]

    def withdrawn_packets(self) -> list[PacketState]:
        """Packets taken back before their reviewer answered (their rows were sent on)."""
        return [packet for packet in self.packets.values() if packet.withdrawn]

    def images_to_burn(self) -> list[tuple[PacketState, list[str]]]:
        """Packets whose rows have been answered but whose pictures are still in the store."""
        result = []
        for packet in self.packets.values():
            answered = []
            for item in packet.items:
                row = self.rows.get(item["row_id"])
                if row is None or item["row_id"] in packet.burned or not item.get("image"):
                    continue
                if any(a.packet_id == packet.packet_id and not a.open for a in row.assignments):
                    answered.append(item["row_id"])
            if answered:
                result.append((packet, answered))
        return result

    # ------------------------------------------------------------------ reporting

    def counts(self) -> dict[str, Any]:
        """Counts for status displays."""
        row_counts: dict[str, int] = {}
        for row in self.rows.values():
            row_counts[row.status] = row_counts.get(row.status, 0) + 1
        call_counts: dict[str, int] = {}
        for call in self.calls.values():
            call_counts[call.status] = call_counts.get(call.status, 0) + 1
        return {
            "tasks": len(self.tasks),
            "calls": call_counts,
            "rows": row_counts,
            "batches_waiting": len(self.batches_to_poll()),
            "packets_open": len(self.open_packets()),
            "hand_queue": len(self.hand_queue()),
            "refused_calls": len(self.refusals),
            "screened_lines": len(self._screened),
        }

    def snapshot(self) -> dict[str, Any]:
        """A plain, deterministic description of the state, for comparing two states."""
        return json.loads(
            canonical_json(
                {
                    "tasks": {tid: asdict(task) for tid, task in self.tasks.items()},
                    "calls": {cid: asdict(call) for cid, call in self.calls.items()},
                    "rows": {rid: asdict(row) for rid, row in self.rows.items()},
                    "batches": {bid: asdict(batch) for bid, batch in self.batches.items()},
                    "packets": {
                        pid: {**asdict(packet), "burned": sorted(packet.burned)} for pid, packet in self.packets.items()
                    },
                    "refusals": self.refusals,
                    "screened": {f"{task_id}|{line}": value for (task_id, line), value in self._screened.items()},
                    "halts": self.halts,
                    "caches": {f"{route}|{key}": value for (route, key), value in self.caches.items()},
                    "storage": self.storage,
                    "passed_by_provider": self._passed_by_provider,
                    "shipped_files": {f"{t}|{r}": list(ids) for (t, r), ids in self.shipped_files.items()},
                    "spend": {f"{r}|{p}": [round(v, 9) for v in cell] for (r, p), cell in self._spend.items()},
                }
            )
        )
