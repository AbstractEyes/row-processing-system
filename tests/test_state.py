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


# ------------------------------------------------------------------ rows reopened and packets withdrawn

NO_REVIEWER = "no reviewer from an unused family is available"


def passed_row(b: Builder):
    """One assessed row that waits for its first review: (row id, call id)."""
    task = b.task(lines={"001": "a"})
    call = b.prepare(task, "001")
    b.submit([call])
    (row,) = b.returned(call)
    b.assessed(row, call)
    return row, call


def publish(b: Builder, row: str, packet: str, reviewer: str = "rev-a", family: str = "alpha", *, round_: int = 1, audit: bool = False) -> None:
    b.add(
        "packet_published",
        {"packet_id": packet, "reviewer_id": reviewer, "family": family, "brief_version": "1",
         "items": [{"row_id": row, "round": round_, "audit": audit, "image": None}]},
    )


def verdict(b: Builder, row: str, packet: str, word: str, reviewer: str = "rev-a", family: str = "alpha") -> None:
    reasons = [] if word == "keep" else ["needs_work"]
    b.add(
        "verdict_received",
        {"packet_id": packet, "row_id": row, "reviewer_id": reviewer, "family": family, "verdict": word, "reasons": reasons, "notes": ""},
    )


def reviewed_once_then_in_question(b: Builder):
    """A row whose first reviewer rejected it and that waits for a further reviewer."""
    row, call = passed_row(b)
    publish(b, row, "p0123456789ab")
    verdict(b, row, "p0123456789ab", "reject")
    b.add("row_escalated", {"row_id": row, "call_id": call, "reason": "in_question", "round": 2, "families_used": ["alpha"]})
    return row, call


def same_state_when_every_event_comes_twice(events):
    once = State.from_events(events)
    doubled = State()
    for event in events:
        doubled.apply(event)
        doubled.apply(event)
    assert doubled.snapshot() == once.snapshot()
    return once


def test_a_reopened_row_that_never_had_a_review_waits_for_its_first_one():
    b = Builder()
    row, call = passed_row(b)
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": "no reviewer from another family than the writer is available", "verdicts": []})
    stuck = State.from_events(b.events)
    assert stuck.rows[row].status == "hand" and [r.row_id for r in stuck.hand_queue()] == [row]
    b.add("row_reopened", {"row_id": row, "call_id": call, "reason": "a reviewer from another family than the writer is now available"})
    state = same_state_when_every_event_comes_twice(b.events)
    assert state.rows[row].status == "passed" and state.rows[row].hand is None
    assert state.hand_queue() == []
    assert [r.row_id for r in state.rows_awaiting_first_review()] == [row]
    assert state.rows[row].ordinal == 0  # it keeps its place in the first-review rotation


def test_a_reopened_row_that_had_a_review_waits_for_a_further_one_and_keeps_its_verdicts():
    b = Builder()
    row, call = reviewed_once_then_in_question(b)
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": NO_REVIEWER, "verdicts": ["rev-a: reject"]})
    assert State.from_events(b.events).rows[row].status == "hand"
    b.add("row_reopened", {"row_id": row, "call_id": call, "reason": "a reviewer from an unused family is now available"})
    state = same_state_when_every_event_comes_twice(b.events)
    reopened = state.rows[row]
    assert reopened.status == "in_question" and reopened.hand is None
    assert [r.row_id for r in state.rows_in_question()] == [row] and state.rows_awaiting_first_review() == []
    assert [v["verdict"] for v in reopened.verdicts] == ["reject"] and reopened.families_used() == {"alpha"}


def test_a_row_that_is_not_in_the_hand_queue_is_not_reopened():
    b = Builder()
    row, call = passed_row(b)
    b.add("row_reopened", {"row_id": row, "call_id": call, "reason": "x"})  # never sent to the owner
    assert State.from_events(b.events).rows[row].status == "passed"
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": NO_REVIEWER, "verdicts": []})
    b.add("hand_resolved", {"row_id": row, "call_id": call, "decision": "accept"})
    b.add("row_reopened", {"row_id": row, "call_id": call, "reason": "x"})  # the owner has answered
    state = State.from_events(b.events)
    assert state.rows[row].status == "accepted" and state.rows[row].accepted_by == "hand"
    assert State.from_events(b.events + [b.events[-1]]).snapshot() == state.snapshot()


def test_a_row_can_go_to_the_hand_queue_again_after_it_was_reopened():
    b = Builder()
    row, call = passed_row(b)
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": NO_REVIEWER, "verdicts": []})
    b.add("row_reopened", {"row_id": row, "call_id": call, "reason": "x"})
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": "3 reviewers without a settlement", "verdicts": []})
    state = State.from_events(b.events)
    assert state.rows[row].status == "hand" and state.rows[row].hand["reason"] == "3 reviewers without a settlement"


def test_a_withdrawn_first_review_puts_the_row_back_as_it_was_before_the_packet():
    b = Builder()
    row, _call = passed_row(b)
    publish(b, row, "p0123456789ab")
    published = State.from_events(b.events)
    assert published.rows[row].status == "in_review" and [p.packet_id for p in published.open_packets()] == ["p0123456789ab"]
    b.add("packet_withdrawn", {"packet_id": "p0123456789ab", "reviewer_id": "rev-a", "row_ids": [row]})
    state = same_state_when_every_event_comes_twice(b.events)
    assert state.rows[row].status == "passed" and state.rows[row].assignments == []
    assert state.rows[row].withdrawals == 1 and state.rows[row].families_used() == set()  # no review took place
    assert state.packets["p0123456789ab"].withdrawn and [p.packet_id for p in state.withdrawn_packets()] == ["p0123456789ab"]
    assert state.open_packets() == [] and state.images_to_burn() == []
    assert [r.row_id for r in state.rows_awaiting_first_review()] == [row]


def test_a_withdrawn_further_review_leaves_the_earlier_review_in_place():
    b = Builder()
    row, call = reviewed_once_then_in_question(b)
    publish(b, row, "p0123456789ac", "rev-b", "beta", round_=2)
    assert State.from_events(b.events).rows[row].status == "in_review"
    b.add("packet_withdrawn", {"packet_id": "p0123456789ac", "reviewer_id": "rev-b", "row_ids": [row]})
    state = same_state_when_every_event_comes_twice(b.events)
    waiting = state.rows[row]
    assert waiting.status == "in_question" and [a.packet_id for a in waiting.assignments] == ["p0123456789ab"]
    assert waiting.families_used() == {"alpha"}  # the family of the withdrawn reviewer is unused again
    assert [r.row_id for r in state.rows_in_question()] == [row]
    assert [v["verdict"] for v in waiting.verdicts] == ["reject"]


def test_a_withdrawn_audit_leaves_an_accepted_row_accepted_and_due_for_an_audit_again():
    b = Builder()
    row, call = passed_row(b)
    publish(b, row, "p0123456789ab")
    verdict(b, row, "p0123456789ab", "keep")
    b.add("row_accepted", {"row_id": row, "call_id": call, "by": "reviewers", "reason": "keep with no assessor flag"})
    publish(b, row, "p0123456789ac", "rev-b", "beta", round_=2, audit=True)
    assert State.from_events(b.events).rows_needing_audit(lambda _id: True) == []
    b.add("packet_withdrawn", {"packet_id": "p0123456789ac", "reviewer_id": "rev-b", "row_ids": [row]})
    state = same_state_when_every_event_comes_twice(b.events)
    assert state.rows[row].status == "accepted"
    assert [a.packet_id for a in state.rows[row].assignments] == ["p0123456789ab"]
    assert [r.row_id for r in state.rows_needing_audit(lambda _id: True)] == [row]


def test_nothing_a_withdrawn_reviewer_sends_afterwards_changes_the_state():
    b = Builder()
    row, _call = passed_row(b)
    publish(b, row, "p0123456789ab")
    b.add("packet_withdrawn", {"packet_id": "p0123456789ab", "reviewer_id": "rev-a", "row_ids": [row]})
    before = State.from_events(b.events).snapshot()
    verdict(b, row, "p0123456789ab", "keep")
    assert State.from_events(b.events).snapshot() == before
    b.add("packet_withdrawn", {"packet_id": "p0123456789ff", "reviewer_id": "rev-a", "row_ids": [row]})  # no such packet
    assert State.from_events(b.events).snapshot() == before


def test_a_packet_the_row_is_withdrawn_from_twice_counts_both_times():
    b = Builder()
    row, _call = passed_row(b)
    publish(b, row, "p0123456789ab")
    b.add("packet_withdrawn", {"packet_id": "p0123456789ab", "reviewer_id": "rev-a", "row_ids": [row]})
    publish(b, row, "p0123456789ac")
    b.add("packet_withdrawn", {"packet_id": "p0123456789ac", "reviewer_id": "rev-a", "row_ids": [row]})
    state = State.from_events(b.events)
    assert state.rows[row].withdrawals == 2 and state.rows[row].status == "passed"


def test_snapshot_is_plain_json():
    snapshot = State.from_events(lifecycle().events).snapshot()
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_counts_summarize_the_state():
    counts = State.from_events(lifecycle().events).counts()
    assert counts["tasks"] == 1
    assert counts["rows"] == {"accepted": 1}
    assert counts["calls"] == {"returned": 1, "failed": 1}
    assert counts["batches_waiting"] == 0


# ------------------------------------------------------------------ the owner sends a row back to be reviewed again

def settle_live(row, solo=None, **policy):
    """What the review policy makes of the verdicts of the row that still count."""
    from rps.config import ReviewPolicy
    from rps.review.policy import settle

    live = row.live_verdicts()
    return settle(
        ReviewPolicy(fix_to_hand=True, **policy),
        flagged=row.flagged,
        verdicts=[v["verdict"] for v in live],
        solo=[(solo or {}).get(v["reviewer_id"], True) for v in live],
    )


def fixed_row_in_hand(b: Builder):
    """A row whose one reviewer (rev-a, family alpha) sent it back with a fix, now in the owner's hand queue."""
    row, call = passed_row(b)
    publish(b, row, "p0123456789ab")
    verdict(b, row, "p0123456789ab", "fix")
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": "fix verdict", "verdicts": ["rev-a: fix"]})
    return row, call


def review_again(b: Builder, row: str, call: str, *set_aside: str) -> None:
    b.add("hand_resolved", {"row_id": row, "call_id": call, "decision": "review", "set_aside": list(set_aside)})


def test_a_row_reviewed_again_by_the_owner_waits_for_a_review_and_its_set_aside_verdict_no_longer_counts():
    b = Builder()
    row, call = fixed_row_in_hand(b)
    before = State.from_events(b.events).rows[row]
    assert before.status == "hand" and settle_live(before).outcome == "hand"
    review_again(b, row, call, "rev-a")
    state = same_state_when_every_event_comes_twice(b.events)
    after = state.rows[row]
    assert after.status == "in_question" and after.hand is None and state.hand_queue() == []
    assert [r.row_id for r in state.rows_in_question()] == [row]
    assert [(v["reviewer_id"], v["verdict"], v.get("set_aside")) for v in after.verdicts] == [("rev-a", "fix", True)]
    assert after.live_verdicts() == [] and len(after.main_verdicts()) == 1  # still in the record, no longer counted
    assert after.families_used() == {"alpha"} and after.reviewers_used() == {"rev-a"}
    assert settle_live(after).outcome == "need_first"  # a review is asked for, the owner is not
    assert after.hand_reviews[0]["set_aside"] == ["rev-a"] and after.hand_reviews[0]["decision"] == "review"


def test_a_row_reviewed_again_counts_a_keep_that_was_not_set_aside_and_is_accepted_by_a_further_keep():
    b = Builder()
    row, call = passed_row(b)
    publish(b, row, "p0123456789ab")
    verdict(b, row, "p0123456789ab", "fix")
    publish(b, row, "p0123456789ac", "rev-b", "beta", round_=2)
    verdict(b, row, "p0123456789ac", "keep", "rev-b", "beta")
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": "fix verdict", "verdicts": ["rev-a: fix", "rev-b: keep"]})
    review_again(b, row, call, "rev-a")
    state = State.from_events(b.events)
    waiting = state.rows[row]
    assert waiting.status == "in_question"
    assert [v["reviewer_id"] for v in waiting.live_verdicts()] == ["rev-b"]
    solo = {"rev-b": False}  # rev-b may not keep alone
    assert settle_live(waiting, solo).outcome == "further"
    publish(b, row, "p0123456789ad", "rev-c", "gamma", round_=3)
    verdict(b, row, "p0123456789ad", "keep", "rev-c", "gamma")
    done = State.from_events(b.events).rows[row]
    assert done.families_used() == {"alpha", "beta", "gamma"}
    assert settle_live(done, solo).outcome == "accept"
    b.add("row_accepted", {"row_id": row, "call_id": call, "by": "reviewers"})
    assert State.from_events(b.events).rows[row].status == "accepted"


def test_a_review_decision_for_a_row_not_in_the_hand_queue_changes_nothing():
    b = Builder()
    row, call = passed_row(b)
    review_again(b, row, call, "rev-a")  # never sent to the owner
    assert State.from_events(b.events).rows[row].status == "passed"
    assert State.from_events(b.events).rows[row].hand_reviews == []
    b2 = Builder()
    row2, call2 = fixed_row_in_hand(b2)
    review_again(b2, row2, call2, "rev-a")
    once = State.from_events(b2.events)
    review_again(b2, row2, call2, "rev-a")  # the row has left the queue
    assert State.from_events(b2.events).snapshot() == once.snapshot()


def test_a_row_with_a_failed_repair_can_still_be_reviewed_again_and_an_extra_key_is_ignored():
    b = Builder()
    row, call = fixed_row_in_hand(b)
    b.add("hand_resolved", {"row_id": row, "call_id": call, "decision": "review", "set_aside": ["rev-a"], "future_key": 1})
    state = State.from_events(b.events)
    assert state.rows[row].status == "in_question" and state.rows[row].repair is None
    # an old-style event with no set_aside list sets nothing aside
    b2 = Builder()
    row2, call2 = fixed_row_in_hand(b2)
    b2.add("hand_resolved", {"row_id": row2, "call_id": call2, "decision": "review"})
    assert State.from_events(b2.events).rows[row2].live_verdicts()[0]["verdict"] == "fix"
