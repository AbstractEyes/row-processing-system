import json

import pytest

from rps.events import Event
from rps.state import State

from helpers import Builder


def lifecycle() -> Builder:
    """Two lines: one written, assessed, reviewed and accepted; one failed after retries."""
    b = Builder()
    task = b.task()
    c1 = b.prepare(task, "001")
    c2 = b.prepare(task, "002")
    b.submit([c1, c2])
    (r1,) = b.returned(c1)
    b.failed(c2, error="overloaded")
    b.add("batch_collected", {"handle_id": "batch-1", "returned": 1, "failed": 1})
    b.assessed(r1, c1)
    b.add(
        "packet_published",
        {
            "packet_id": "p0123456789ab", "reviewer_id": "rev-a", "family": "alpha", "brief_version": "1",
            "items": [{"row_id": r1, "round": 1, "audit": False, "image": None}],
        },
    )
    b.add(
        "verdict_received",
        {"packet_id": "p0123456789ab", "row_id": r1, "reviewer_id": "rev-a", "family": "alpha", "verdict": "keep", "reasons": [], "notes": ""},
    )
    b.add("row_accepted", {"row_id": r1, "call_id": c1, "by": "reviewers"})
    return b


def test_replaying_the_same_events_twice_gives_the_same_state():
    events = lifecycle().events
    once = State.from_events(events)
    twice = State.from_events(events + events)
    assert json.dumps(once.snapshot(), sort_keys=True) == json.dumps(twice.snapshot(), sort_keys=True)
    # Spend must not double count either.
    assert once.spend("fake-batch").actual == pytest.approx(twice.spend("fake-batch").actual)


def test_replay_is_the_same_whether_events_arrive_in_one_go_or_one_by_one():
    events = lifecycle().events
    incremental = State()
    for event in events:
        incremental.apply(event)
        incremental.apply(event)  # every event applied twice in a row
    assert incremental.snapshot() == State.from_events(events).snapshot()


def test_rows_that_pass_assessment_are_numbered_per_writer_provider():
    b = Builder()
    task = b.task(lines={"001": "a", "002": "b", "003": "c", "004": "d", "005": "e"})
    calls = [b.prepare(task, line) for line in ("001", "002", "003", "004", "005")]
    b.submit(calls)
    rows = [b.returned(call)[0] for call in calls[:4]]
    other = b.row(calls[4], 0, provider="other-provider")
    b.add(
        "call_returned",
        {
            "call_id": calls[4], "handle_id": "batch-1", "usage": {}, "usd": 0.0, "price_table_date": "2026-11-06",
            "minutes": 0.1, "rows": [other], "row_flags": {}, "skips": [],
        },
    )
    b.assessed(rows[0], calls[0])
    b.assessed(rows[1], calls[1], passed=False, errors=["bad"])  # a failed row takes no number
    b.assessed(other["row_id"], calls[4])
    b.assessed(rows[2], calls[2])
    b.assessed(rows[3], calls[3])
    state = State.from_events(b.events + b.events[-5:])  # the last assessments arrive twice
    assert [state.rows[row].ordinal for row in rows] == [0, None, 1, 2]
    assert state.rows[other["row_id"]].ordinal == 0  # another provider counts on its own
    assert state.snapshot()["passed_by_provider"] == {"fake": 3, "other-provider": 1}


def test_calls_prepared_but_not_submitted_are_pending():
    b = Builder()
    task = b.task()
    c1 = b.prepare(task, "001")
    c2 = b.prepare(task, "002")
    state = State.from_events(b.events)
    assert [c.call_id for c in state.pending_calls()] == [c1, c2]
    b.submit([c1])
    state = State.from_events(b.events)
    assert [c.call_id for c in state.pending_calls()] == [c2]
    assert state.calls[c1].status == "submitted"
    assert state.calls[c1].handle_id == "batch-1"


def test_a_submitted_batch_is_polled_until_every_call_has_an_outcome():
    b = Builder()
    task = b.task()
    c1, c2 = b.prepare(task, "001"), b.prepare(task, "002")
    b.submit([c1, c2], handle_id="B1")
    assert [x.handle_id for x in State.from_events(b.events).batches_to_poll()] == ["B1"]
    b.returned(c1, handle_id="B1")
    assert [x.handle_id for x in State.from_events(b.events).batches_to_poll()] == ["B1"]
    b.failed(c2, handle_id="B1")
    assert State.from_events(b.events).batches_to_poll() == []


def test_a_collected_batch_is_not_polled_again():
    b = Builder()
    task = b.task()
    c1 = b.prepare(task, "001")
    b.submit([c1], handle_id="B1")
    b.add("batch_collected", {"handle_id": "B1", "returned": 0, "failed": 0})
    assert State.from_events(b.events).batches_to_poll() == []


def test_first_outcome_of_a_call_wins():
    b = Builder()
    task = b.task()
    c1 = b.prepare(task, "001")
    b.submit([c1])
    b.returned(c1, usd=0.01)
    b.returned(c1, usd=99.0)
    b.failed(c1)
    state = State.from_events(b.events)
    assert state.calls[c1].status == "returned"
    assert state.calls[c1].usd == pytest.approx(0.01)
    assert len(state.rows) == 1


def test_spend_holds_the_estimate_until_the_call_returns_then_uses_the_actual():
    b = Builder()
    task = b.task()
    c1 = b.prepare(task, "001", est=0.04)
    c2 = b.prepare(task, "002", est=0.04)
    state = State.from_events(b.events)
    assert state.spend("fake-batch").reserved == pytest.approx(0.08)
    assert state.spend("fake-batch").actual == 0
    b.submit([c1, c2])
    b.returned(c1, usd=0.01)
    b.failed(c2, billed=True, usd=0.02)
    state = State.from_events(b.events)
    spend = state.spend("fake-batch", "production")
    assert spend.reserved == pytest.approx(0)
    assert spend.actual == pytest.approx(0.03)
    assert spend.calls == 2 and spend.failed == 1
    recomputed = state.recompute_spend()[("fake-batch", "production")]
    assert recomputed.actual == pytest.approx(spend.actual)
    assert recomputed.reserved == pytest.approx(spend.reserved)


def test_unknown_and_malformed_events_are_counted_not_fatal():
    b = Builder()
    b.task()
    b.add("something_new", {"x": 1})
    b.add("call_prepared", {"call_id": "missing-the-rest"})
    b.add("call_returned", {"nonsense": True})
    state = State.from_events(b.events)
    assert len(state.tasks) == 1
    assert state.unknown_kinds == {"something_new": 1}
    kinds = [kind for kind, _ in state.bad_events]
    assert "call_prepared" in kinds and "call_returned" in kinds


def test_line_outcomes_follow_the_calls_and_rows():
    b = Builder()
    task = b.task(lines={"001": "a", "002": "b", "003": "c", "004": "d", "005": "e"})
    state = State.from_events(b.events)
    assert state.line_status(task, "001", 2).outcome == "new"

    c1 = b.prepare(task, "001")
    assert State.from_events(b.events).line_status(task, "001", 2).outcome == "in_flight"
    b.submit([c1])
    (r1,) = b.returned(c1)
    assert State.from_events(b.events).line_status(task, "001", 2).outcome == "active"
    b.assessed(r1, c1)
    b.add("packet_published", {"packet_id": "p0123456789ab", "reviewer_id": "rev-a", "family": "alpha", "brief_version": "1", "items": [{"row_id": r1, "round": 1, "audit": False, "image": None}]})
    b.add("verdict_received", {"packet_id": "p0123456789ab", "row_id": r1, "reviewer_id": "rev-a", "family": "alpha", "verdict": "keep"})
    b.add("row_accepted", {"row_id": r1, "call_id": c1, "by": "reviewers"})
    assert State.from_events(b.events).line_status(task, "001", 2).outcome == "accepted"

    # A failed call is retried once, then the line is exhausted.
    c2 = b.prepare(task, "002")
    b.submit([c2], handle_id="B2")
    b.failed(c2, handle_id="B2")
    status = State.from_events(b.events).line_status(task, "002", 2)
    assert (status.outcome, status.next_attempt, status.retry_of) == ("retry", 2, c2)
    c2b = b.prepare(task, "002", attempt=2, purpose="rebound")
    b.submit([c2b], handle_id="B3")
    b.failed(c2b, handle_id="B3")
    assert State.from_events(b.events).line_status(task, "002", 2).outcome == "exhausted"

    # A failure that is not retryable is not retried.
    c3 = b.prepare(task, "003")
    b.submit([c3], handle_id="B4")
    b.failed(c3, retryable=False, handle_id="B4")
    assert State.from_events(b.events).line_status(task, "003", 2).outcome == "exhausted"

    # A skip object leaves no rows: the line is set aside.
    c4 = b.prepare(task, "004")
    b.submit([c4], handle_id="B5")
    b.add("call_returned", {"call_id": c4, "handle_id": "B5", "usage": {}, "usd": 0.0, "rows": [], "skips": [{"skip": True, "class": "person", "reason": "a person"}]})
    state = State.from_events(b.events)
    assert state.line_status(task, "004", 2).outcome == "set_aside"
    assert [c.call_id for c in state.calls_with_unrecorded_skips()] == [c4]
    b.add("row_set_aside", {"call_id": c4, "class": "person", "reason": "a person"})
    assert State.from_events(b.events).calls_with_unrecorded_skips() == []

    state = State.from_events(b.events)
    progress = state.job_progress(2)
    assert progress["lines_total"] == 5
    assert progress["lines_new"] == 1
    assert progress["lines_accepted"] == 1
    assert progress["lines_exhausted"] == 2
    assert progress["lines_set_aside"] == 1
    assert progress["lines_done"] == 4


def test_an_assessor_rejection_with_a_retry_makes_the_line_due_again_with_feedback():
    b = Builder()
    task = b.task(lines={"001": "a"})
    c1 = b.prepare(task, "001")
    b.submit([c1])
    (r1,) = b.returned(c1)
    b.assessed(r1, c1, passed=False, errors=["thought defines a word"])
    state = State.from_events(b.events)
    assert [r.row_id for r in state.rows_failed_unrejected()] == [r1]
    b.add("row_rejected", {"row_id": r1, "call_id": c1, "by": "assessor", "reason": "failed assessment", "errors": ["thought defines a word"], "retry": True})
    state = State.from_events(b.events)
    status = state.line_status(task, "001", 2)
    assert status.outcome == "retry"
    assert status.next_attempt == 2
    assert status.feedback == ("thought defines a word",)
    due = state.lines_needing_calls(2)
    assert [(t.task_id, l.line_id, s.outcome) for t, l, s in due] == [(task, "001", "retry")]
    # With only one attempt allowed the same rejection ends the line.
    assert state.line_status(task, "001", 1).outcome == "exhausted"


def test_files_to_ship_notices_new_and_withdrawn_rows():
    events = lifecycle().events
    state = State.from_events(events)
    task_id = next(iter(state.tasks))
    assert state.files_to_ship() == [(task_id, "fake-batch")]
    accepted = [r.row_id for r in state.accepted_rows_by_file()[(task_id, "fake-batch")]]
    shipped = Event("2026-11-07T10:00:00.000000Z", "controller-test", "shipped",
                    {"task_id": task_id, "route": "fake-batch", "row_ids": accepted, "path": "p", "count": 1})
    state = State.from_events(events + [shipped])
    assert state.files_to_ship() == []
    assert state.rows_to_ship() == []


def test_snapshot_is_plain_json():
    snapshot = State.from_events(lifecycle().events).snapshot()
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_counts_summarize_the_state():
    counts = State.from_events(lifecycle().events).counts()
    assert counts["tasks"] == 1
    assert counts["rows"] == {"accepted": 1}
    assert counts["calls"] == {"returned": 1, "failed": 1}
    assert counts["batches_waiting"] == 0
