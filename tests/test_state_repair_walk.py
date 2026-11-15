"""The queries of the repair step read the same as ever while they build less: a due call's notes are built only
when asked for, and the walk over the rows in repair is shared and kept only until the next event."""

from __future__ import annotations

import random

from rps import ids, repair
from rps.failures import KIND_PROVIDER, KIND_TRUNCATED
from rps.state import State

from helpers import Builder
from repair_helpers import FIX_REASON

MAX_ATTEMPTS, ATTEMPTS = 3, 2


def answers(state: State):
    """Every repair query of a state, as plain values."""
    due = state.repairs_needing_calls(MAX_ATTEMPTS, ATTEMPTS)
    failed = state.repairs_failed(MAX_ATTEMPTS, ATTEMPTS)
    every = state.lines_needing_calls(MAX_ATTEMPTS, ATTEMPTS)
    return (
        [(t.task_id, l.line_id, s) for t, l, s in due],
        [(r.row_id, code, detail) for r, code, detail in failed],
        [(t.task_id, l.line_id, s) for t, l, s in every],
    )


class Stream:
    """A long run of mixed events built by choosing among what the state offers at each step."""

    def __init__(self, seed: int, tasks: int = 4, lines: int = 6) -> None:
        self.rng = random.Random(seed)
        self.builder = Builder()
        self.state = State()
        self.packets = 0
        for number in range(tasks):
            self.add_task(str(number), {f"{n:03d}": f"spec {number}-{n}" for n in range(1, lines + 1)})

    def push(self, count_before: int) -> None:
        for event in self.builder.events[count_before:]:
            self.state.apply(event)

    def add_task(self, batch, lines):
        before = len(self.builder.events)
        self.builder.task(batch=batch, lines=lines)
        self.push(before)

    def step(self) -> bool:
        """Do one thing the state allows. False when nothing is left to do."""
        b, st, rng = self.builder, self.state, self.rng
        before = len(b.events)
        options = []
        for task, line, status in st.lines_needing_calls(MAX_ATTEMPTS, ATTEMPTS):
            options.append(("prepare", (task, line, status)))
        for call in st.calls.values():
            if call.status == "prepared":
                options.append(("submit", call))
            elif call.status == "submitted":
                options.extend([("return", call)] * 2 + [("fail", call)])
        for row in st.rows.values():
            if row.status == "written":
                options.append(("assess", row))
            elif row.status == "passed":
                options.append(("review", row))
            elif row.status == "hand":
                options.append(("request", row))
        for row, code, detail in st.repairs_failed(MAX_ATTEMPTS, ATTEMPTS):
            options.append(("give_up", row))
        if not options:
            return False
        kind, thing = rng.choice(options)
        if kind == "prepare":
            task, line, status = thing
            if status.repair_of is not None:
                b.prepare(task.task_id, line.line_id, status.next_attempt, purpose="repair", repair_of=status.repair_of,
                          retry_of=status.retry_of, feedback=list(status.feedback))
            else:
                b.prepare(task.task_id, line.line_id, status.next_attempt, retry_of=status.retry_of, feedback=list(status.feedback))
        elif kind == "submit":
            b.submit([thing.call_id], handle_id=f"batch-{thing.call_id}")
        elif kind == "return":
            b.returned(thing.call_id, rows=rng.choice([0, 1, 1, 2]), handle_id=thing.handle_id)
        elif kind == "fail":
            b.failed(
                thing.call_id, error=rng.choice(["overloaded", "too long", "no good"]), retryable=rng.random() < 0.7,
                billed=rng.random() < 0.5, handle_id=thing.handle_id,
            )
            if rng.random() < 0.4:  # the kind of failure is its own field
                b.events[-1].payload["kind"] = rng.choice([KIND_TRUNCATED, KIND_PROVIDER])
        elif kind == "assess":
            passed = rng.random() < 0.45
            b.assessed(thing.row_id, thing.call_id, passed=passed, errors=[] if passed else [f"error {rng.randrange(3)}", "shared error"])
            if not passed:
                b.add("row_rejected", {"row_id": thing.row_id, "call_id": thing.call_id, "by": "assessor", "reason": "checks",
                                       "errors": [f"error {rng.randrange(3)}", "shared error"], "retry": False})
            elif rng.random() < 0.2:
                b.add("row_set_aside", {"row_id": thing.row_id, "call_id": thing.call_id, "class": "other", "reason": "set aside"})
        elif kind == "review":
            self.packets += 1
            packet = f"p{self.packets:012x}"
            reviewer, family = f"rev-{rng.randrange(3)}", f"family-{rng.randrange(3)}"
            b.add("packet_published", {"packet_id": packet, "reviewer_id": reviewer, "family": family, "brief_version": "1",
                                       "items": [{"row_id": thing.row_id, "image": None, "round": 1, "audit": False}]})
            b.add("verdict_received", {"packet_id": packet, "row_id": thing.row_id, "reviewer_id": reviewer, "family": family,
                                       "verdict": "fix", "reasons": ["needs_work"], "notes": f"note {rng.randrange(4)}"})
            b.add("row_to_hand", {"row_id": thing.row_id, "call_id": thing.call_id, "reason": FIX_REASON, "verdicts": []})
        elif kind == "request":
            attempt = st.next_attempt(thing.task_id, thing.line)
            b.add("repair_requested", {"row_id": thing.row_id, "call_id": ids.call_id(thing.task_id, thing.line, attempt),
                                       "route": "fake-batch"})
        elif kind == "give_up":
            b.add("row_to_hand", {"row_id": thing.row_id, "call_id": thing.call_id, "reason": repair.FAILED_CHECKER_REASON, "verdicts": []})
        self.push(before)
        return True


def test_after_every_event_the_queries_equal_those_computed_from_scratch_and_the_kept_walk_is_exact():
    seen = {"due": 0, "failed": 0, "retry": 0, "lazy": 0, "moved": 0}
    for seed in (1, 2, 3):
        stream = Stream(seed)
        for _ in range(450):
            if not stream.step():
                break
            kept = answers(stream.state)  # asked of the state that has been asked before, so a kept walk is in play
            again = answers(stream.state)  # and asked again with no event between
            fresh = answers(State.from_events(stream.builder.events))
            assert kept == again == fresh
            due, failed, _ = fresh
            seen["due"] += bool(due)
            seen["failed"] += bool(failed)
            seen["retry"] += any(s.retry_of for _t, _l, s in due)
            seen["moved"] += any(s.next_attempt > 2 for _t, _l, s in due)
            for _t, _l, status in stream.state.repairs_needing_calls(MAX_ATTEMPTS, ATTEMPTS):
                if status.repair_of is not None and "feedback" not in status.__dict__:
                    seen["lazy"] += 1  # a status asked for and not read: its notes are not built
    assert all(count > 20 for count in seen.values()), seen  # the stream reached every kind of case


def test_the_notes_of_a_due_repair_call_are_built_when_first_read_and_not_before():
    builder = Builder()
    task = builder.task(lines={"001": "a cat"})
    call = builder.prepare(task, "001")
    builder.submit([call])
    row = builder.returned(call)[0]
    builder.assessed(row, call)
    builder.add("packet_published", {"packet_id": "p000000000001", "reviewer_id": "rev-a", "family": "alpha", "brief_version": "1",
                                     "items": [{"row_id": row, "image": None, "round": 1, "audit": False}]})
    builder.add("verdict_received", {"packet_id": "p000000000001", "row_id": row, "reviewer_id": "rev-a", "family": "alpha",
                                     "verdict": "fix", "reasons": ["needs_work"], "notes": "thought 1 is wrong"})
    builder.add("row_to_hand", {"row_id": row, "call_id": call, "reason": FIX_REASON, "verdicts": []})
    builder.add("repair_requested", {"row_id": row, "call_id": ids.call_id(task, "001", 2), "route": "fake-batch"})
    state = State.from_events(builder.events)
    calls = [state.lines_needing_calls(3, 2), state.repairs_needing_calls(3, 2), state.lines_needing_calls(3, 2)]
    assert [len(c) for c in calls] == [1, 1, 1]
    status = calls[0][0][2]
    assert status.outcome == "repair" and status.repair_of == row and status.next_attempt == 2
    assert "feedback" not in status.__dict__  # asked about, and the notes are still not built
    assert status.feedback == repair.repair_feedback(state.rows[row])  # built on the first read
    assert "thought 1 is wrong" in status.feedback[0] and status.feedback[-1].startswith("{")
    assert status.feedback is status.feedback and "feedback" in status.__dict__
    assert calls[1][0][2] == status and hash(calls[1][0][2]) == hash(status)
    # a status that was renumbered keeps its notes unbuilt until read
    from rps.state import _renumbered
    unread = State.from_events(builder.events).repairs_needing_calls(3, 2)[0][2]
    again = _renumbered(unread, 5)
    assert again.next_attempt == 5 and unread.next_attempt == 2 and "feedback" not in again.__dict__
    assert again.feedback == status.feedback
