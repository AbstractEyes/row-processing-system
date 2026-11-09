"""The state of the repair step, rebuilt from events: rows sent back to their writer, the calls that write them
again, the rows those calls wrote, and where a spec line stands meanwhile."""

import json

import pytest

from rps import ids, repair
from rps.failures import KIND_PROVIDER, KIND_TRUNCATED
from rps.state import State

from helpers import Builder
from repair_helpers import FIX_REASON

NOTE = "thought 1 states a pose that no record gives"


class Bench:
    """Events for one task of two spec lines whose first call wrote one row for line 001, which a reviewer sent
    back with a fix verdict; `state` is the state those events (and any added later) rebuild."""

    def __init__(self, *, notes=(NOTE,), rows=1, lines=None):
        self.b = Builder()
        self.task = self.b.task(lines=lines or {"001": "a cat", "002": "a dog"})
        self.call = self.b.prepare(self.task, "001")
        self.b.submit([self.call])
        self.rows = self.b.returned(self.call, rows=rows)
        self.row = self.rows[0]
        for row_id in self.rows:
            self.b.assessed(row_id, self.call)
        # one fix verdict for each note, from reviewers of different families (the row waits for the owner after them)
        for number, note in enumerate(notes or ("",)):
            self.send_to_review(self.row, f"rev-{number}", f"family-{number}", "fix", notes=note, packet=f"p{number:012x}")
        self.b.add("row_to_hand", {"row_id": self.row, "call_id": self.call, "reason": FIX_REASON, "verdicts": ["rev-0: fix"]})

    # -- events

    def verdict(self, row_id, reviewer, family, kind, notes, packet="p000000000001"):
        return {
            "packet_id": packet, "row_id": row_id, "reviewer_id": reviewer, "family": family, "verdict": kind,
            "reasons": ["needs_work"], "notes": notes,
        }

    def send_to_review(self, row_id, reviewer, family, kind, *, notes="", packet="p000000000001"):
        self.b.add(
            "packet_published",
            {"packet_id": packet, "reviewer_id": reviewer, "family": family, "brief_version": "1",
             "items": [{"row_id": row_id, "image": None, "round": 1, "audit": False}]},
        )
        self.b.add("verdict_received", self.verdict(row_id, reviewer, family, kind, notes, packet))

    def request(self, row_id=None, attempt=2, route="fake-batch"):
        call_id = ids.call_id(self.task, "001", attempt)
        self.b.add("repair_requested", {"row_id": row_id or self.row, "call_id": call_id, "route": route})
        return call_id

    def prepare_repair(self, attempt=2, *, row_id=None, retry_of=None, feedback=None, est=0.01):
        row_id = row_id or self.row
        return self.b.prepare(
            self.task, "001", attempt, purpose="repair", est=est, repair_of=row_id, retry_of=retry_of,
            feedback=feedback if feedback is not None else list(repair.repair_feedback(self.state.rows[row_id])),
        )

    @property
    def state(self) -> State:
        return State.from_events(self.b.events)


def test_a_request_takes_the_row_out_of_the_hand_queue_and_puts_its_repair_on_record():
    bench = Bench()
    assert [row.row_id for row in bench.state.hand_queue()] == [bench.row]
    call_id = bench.request()
    state = bench.state
    row = state.rows[bench.row]
    assert state.hand_queue() == [] and row.status == "repairing" and row.hand is None
    assert row.repair == {"call_id": call_id, "route": "fake-batch", "calls": [], "rows": []}
    assert row.repair_of is None
    assert [item.row_id for item in state.rows_being_repaired()] == [bench.row]
    assert state.counts()["rows"] == {"repairing": 1} and state.counts()["hand_queue"] == 0
    assert state.unknown_kinds == {} and state.bad_events == []
    # the verdicts it was sent back with stay on the row
    assert [(v["verdict"], v["notes"]) for v in row.verdicts] == [("fix", NOTE)]


@pytest.mark.parametrize("case", ["owner_accepted", "unknown_row", "malformed_call_id", "not_in_the_queue"])
def test_a_request_for_a_row_that_cannot_be_sent_back_changes_nothing(case):
    bench = Bench()
    if case == "owner_accepted":
        bench.b.add("hand_resolved", {"row_id": bench.row, "call_id": bench.call, "decision": "accept"})
    before = bench.state.snapshot()
    if case == "unknown_row":
        bench.request(row_id="no-such-row")
    elif case == "malformed_call_id":
        bench.b.add("repair_requested", {"row_id": bench.row, "call_id": "not a call id", "route": "fake-batch"})
    elif case == "not_in_the_queue":
        bench.request(row_id=bench.call)  # the id of a call, not of a row
    else:
        bench.request()  # the owner answered it first
    state = bench.state
    assert state.snapshot() == before
    assert state.rows[bench.row].repair is None and state.rows[bench.row].status in ("hand", "accepted")
    assert state.repairs_needing_calls(2) == [] and state.rows_being_repaired() == []
    assert len(state.bad_events) == (1 if case == "malformed_call_id" else 0)


def test_a_row_is_sent_back_once_whatever_is_requested_after():
    bench = Bench()
    first = bench.request(attempt=2)
    bench.request(attempt=3)  # a second request for the same row is not taken
    state = bench.state
    assert state.rows[bench.row].repair["call_id"] == first
    assert state.next_attempt(bench.task, "001") == 3  # only the first call's number is reserved
    again = Bench()
    again.request(attempt=2)
    again.b.add("row_to_hand", {"row_id": again.row, "call_id": again.call, "reason": "x", "verdicts": []})
    again.request(attempt=3)  # back in the queue after its repair, and still not sent back a second time
    assert again.state.rows[again.row].status == "hand" and again.state.rows[again.row].repair["call_id"] == first


def test_the_call_planned_for_the_row_is_due_with_the_notes_and_the_row_as_feedback():
    bench = Bench(notes=("first problem", "second problem"))
    call_id = bench.request()
    state = bench.state
    ((task, line, status),) = state.repairs_needing_calls(2)
    assert (task.task_id, line.line_id) == (bench.task, "001")
    assert (status.outcome, status.next_attempt, status.repair_of) == ("repair", 2, bench.row)
    assert status.retry_of is None and status.boost is False
    assert ids.call_id(task.task_id, line.line_id, status.next_attempt) == call_id
    assert status.feedback[:-1] == ("first problem", "second problem")
    assert json.loads(status.feedback[-1]) == {"id": "001-0", "thoughts": ["thought"]}
    assert "rev-" not in " ".join(status.feedback)
    # it is among the calls due, ahead of the lines that have no call yet, and the line itself is not due
    due = state.lines_needing_calls(2)
    assert [(item[1].line_id, item[2].outcome) for item in due] == [("001", "repair"), ("002", "new")]
    assert state.line_status(bench.task, "001", 2).outcome == "active"


def test_a_call_of_the_repair_step_is_not_an_attempt_of_its_line_and_is_charged_to_repair():
    bench = Bench()
    bench.request()
    before = bench.state.line_status(bench.task, "001", 2)
    repair_call = bench.prepare_repair(est=0.02)
    bench.b.submit([repair_call], handle_id="batch-2")
    state = bench.state
    call = state.calls[repair_call]
    assert call.repair_of == bench.row and call.purpose == "repair" and call.attempt == 2 and call.retry_of is None
    assert state.line_status(bench.task, "001", 2) == before  # the line's own attempts are unchanged
    assert state.spend("fake-batch", "repair").reserved == pytest.approx(0.02)
    assert state.spend("fake-batch", "rebound").total == 0
    assert state.rows[bench.row].repair["calls"] == [repair_call]
    assert state.repairs_needing_calls(2) == [] and state.unfinished_calls("fake-batch") == 1
    assert state.next_attempt(bench.task, "001") == 3  # one more than the repair call, which is not a line attempt
    assert state.recompute_spend()[("fake-batch", "repair")].reserved == pytest.approx(0.02)


def test_the_rows_a_repair_wrote_are_linked_to_the_row_and_it_is_repaired_when_one_passes_the_checker():
    bench = Bench()
    bench.request()
    call = bench.prepare_repair()
    bench.b.submit([call], handle_id="batch-2")
    (new_row,) = bench.b.returned(call, handle_id="batch-2")
    state = bench.state
    assert new_row == ids.row_id(call, 0) != bench.row
    assert state.rows[new_row].repair_of == bench.row and state.rows[bench.row].repair_of is None
    assert state.rows[bench.row].repair["rows"] == [new_row] and state.calls[call].row_ids == [new_row]
    assert state.rows[bench.row].status == "repairing"  # not yet known to be good enough
    assert state.repairs_failed(2) == []  # a row not yet checked is not a failure
    bench.b.assessed(new_row, call)
    state = bench.state
    assert state.rows[bench.row].status == "repaired" and state.rows_being_repaired() == []
    assert state.rows[new_row].status == "passed" and state.rows[new_row].ordinal == 1
    assert state.rows[new_row].record["row_id"] == new_row and "repair_of" not in state.rows[new_row].record


def test_a_line_stands_where_the_rows_that_repaired_its_row_stand():
    bench = Bench(lines={"001": "a cat"})
    bench.request()
    call = bench.prepare_repair()
    bench.b.submit([call], handle_id="batch-2")
    (new_row,) = bench.b.returned(call, handle_id="batch-2")
    bench.b.assessed(new_row, call)
    assert bench.state.line_status(bench.task, "001", 2).outcome == "active"  # the new row is on its way to review
    bench.send_to_review(new_row, "rev-b", "beta", "keep", packet="p000000000002")
    bench.b.add("row_accepted", {"row_id": new_row, "call_id": call, "by": "reviewers", "reason": "keep"})
    state = bench.state
    assert state.rows[bench.row].status == "repaired" and state.rows[new_row].status == "accepted"
    assert state.line_status(bench.task, "001", 2).outcome == "accepted"
    assert state.job_progress(2)["lines_accepted"] == 1 and state.job_progress(2)["lines_done"] == 1
    assert [row.row_id for row in state.accepted_rows_by_file()[(bench.task, "fake-batch")]] == [new_row]  # the repaired row never ships


def test_a_line_whose_repaired_row_was_discarded_is_rejected_and_one_whose_row_is_still_being_repaired_is_active():
    bench = Bench(lines={"001": "a cat"})
    bench.request()
    assert bench.state.line_status(bench.task, "001", 2).outcome == "active"
    call = bench.prepare_repair()
    bench.b.submit([call], handle_id="batch-2")
    (new_row,) = bench.b.returned(call, handle_id="batch-2")
    bench.b.assessed(new_row, call)
    bench.b.add("row_rejected", {"row_id": new_row, "call_id": call, "by": "reviewers", "reason": "2 reject verdicts", "errors": [], "retry": False})
    assert bench.state.line_status(bench.task, "001", 2).outcome == "rejected"


def test_a_later_attempt_of_the_line_never_takes_the_number_of_a_repair_call():
    # one call wrote two rows: the first was sent back and repaired, the second failed the checker and may be written
    # again (a retry), after the first row's repair was rejected: the retry is attempt 3 because 2 was the repair
    b = Builder()
    task = b.task(lines={"001": "a series"})
    first = b.prepare(task, "001")
    b.submit([first])
    ids_written = b.returned(first, rows=2)
    top, other = ids_written
    b.assessed(top, first)
    b.add("row_assessed", {"row_id": other, "call_id": first, "passed": False, "flags": [], "errors": ["too long"]})
    b.add("row_rejected", {"row_id": other, "call_id": first, "by": "assessor", "reason": "failed assessment", "errors": ["too long"], "retry": True})
    b.add("packet_published", {"packet_id": "p000000000001", "reviewer_id": "rev-a", "family": "alpha", "brief_version": "1", "items": [{"row_id": top, "image": None, "round": 1, "audit": False}]})
    b.add("verdict_received", {"packet_id": "p000000000001", "row_id": top, "reviewer_id": "rev-a", "family": "alpha", "verdict": "fix", "reasons": ["x"], "notes": NOTE})
    b.add("row_to_hand", {"row_id": top, "call_id": first, "reason": FIX_REASON, "verdicts": ["rev-a: fix"]})
    b.add("repair_requested", {"row_id": top, "call_id": ids.call_id(task, "001", 2), "route": "fake-batch"})
    assert State.from_events(b.events).line_status(task, "001", 3).outcome == "active"  # the top row is being repaired
    repair_call = b.prepare(task, "001", 2, purpose="repair", repair_of=top, feedback=["n", "{}"])
    b.submit([repair_call], handle_id="batch-2")
    (new_row,) = b.returned(repair_call, handle_id="batch-2")
    b.assessed(new_row, repair_call)
    b.add("row_rejected", {"row_id": new_row, "call_id": repair_call, "by": "reviewers", "reason": "2 reject verdicts", "errors": [], "retry": False})
    state = State.from_events(b.events)
    status = state.line_status(task, "001", 3)
    assert (status.outcome, status.next_attempt, status.retry_of) == ("retry", 3, first)
    assert status.feedback == ("too long",)
    assert status.repair_of is None  # a line's own retry is not a repair
    assert state.next_attempt(task, "001") == 3


# ------------------------------------------------------------------ a call that fails, and a repair that comes to nothing


def failed_call(bench, call_id, *, retryable=True, billed=False, kind=KIND_PROVIDER, error="overloaded"):
    bench.b.add(
        "call_failed",
        {"call_id": call_id, "handle_id": "batch-2", "error": error, "retryable": retryable, "billed": billed, "kind": kind,
         "usage": {}, "usd": 0.0, "price_table_date": "2026-11-06", "minutes": 0.1},
    )


def in_flight(bench, attempt=2, **kw):
    bench.request()
    call = bench.prepare_repair(attempt, **kw)
    bench.b.submit([call], handle_id=f"batch-{attempt}")
    return call


def test_a_repair_call_that_failed_is_made_again_while_the_row_has_tries_left_with_the_same_notes_and_row():
    bench = Bench()
    first = in_flight(bench)
    feedback = tuple(bench.state.calls[first].feedback)
    failed_call(bench, first)
    state = bench.state
    assert state.repairs_failed(2) == []
    ((_task, _line, status),) = state.repairs_needing_calls(2)
    assert (status.next_attempt, status.retry_of, status.repair_of, status.boost) == (3, first, bench.row, False)
    assert status.feedback == feedback  # the same notes and the same row
    # it goes out as the next call, and when that fails too the repair has come to nothing
    second = bench.prepare_repair(3, retry_of=first)
    bench.b.submit([second], handle_id="batch-3")
    assert bench.state.repairs_needing_calls(2) == []
    failed_call(bench, second)
    state = bench.state
    assert state.repairs_needing_calls(2) == []
    assert [(row.row_id, code, detail) for row, code, detail in state.repairs_failed(2)] == [(bench.row, "call_failed", KIND_PROVIDER)]
    assert state.rows[bench.row].repair["calls"] == [first, second]


def test_a_cut_off_repair_is_made_again_with_a_larger_limit_and_a_call_that_cannot_be_mended_is_not_made_again():
    bench = Bench()
    first = in_flight(bench)
    failed_call(bench, first, kind=KIND_TRUNCATED, billed=True, error="cut off")
    ((_t, _l, status),) = bench.state.repairs_needing_calls(2)
    assert status.boost is True
    other = Bench()
    call = in_flight(other)
    failed_call(other, call, retryable=False, kind="refused", error="the model declined")
    state = other.state
    assert state.repairs_needing_calls(5) == []
    assert [(code, detail) for _row, code, detail in state.repairs_failed(5)] == [("call_failed", "refused")]


@pytest.mark.parametrize("max_attempts, tries", [(1, 1), (2, 2), (3, 3)])
def test_a_repair_is_tried_as_many_times_as_the_attempts_of_a_line_allow(max_attempts, tries):
    bench = Bench()
    call = in_flight(bench)
    made = 1
    while True:
        failed_call(bench, call)
        due = bench.state.repairs_needing_calls(max_attempts)
        if not due:
            break
        made += 1
        call = bench.prepare_repair(made + 1, retry_of=call)
        bench.b.submit([call], handle_id=f"batch-{made + 1}")
    assert made == tries
    assert [code for _row, code, _detail in bench.state.repairs_failed(max_attempts)] == ["call_failed"]


def test_a_repair_whose_writer_answered_with_no_row_has_come_to_nothing():
    bench = Bench()
    call = in_flight(bench)
    bench.b.returned(call, rows=0, skips=[{"skip": True, "class": "other", "reason": "cannot be done"}], handle_id="batch-2")
    state = bench.state
    assert [(code, detail) for _row, code, detail in state.repairs_failed(2)] == [("no_row", "")]
    assert state.repairs_needing_calls(2) == []


def test_a_repair_whose_rows_were_all_rejected_or_set_aside_by_the_checker_has_come_to_nothing():
    bench = Bench()
    call = in_flight(bench)
    (new_row,) = bench.b.returned(call, handle_id="batch-2")
    assert bench.state.repairs_failed(2) == []  # not checked yet
    bench.b.add("row_assessed", {"row_id": new_row, "call_id": call, "passed": False, "flags": [], "errors": ["too long"]})
    assert bench.state.repairs_failed(2) == []  # failed, and the rejection is not recorded yet
    bench.b.add("row_rejected", {"row_id": new_row, "call_id": call, "by": "assessor", "reason": "failed assessment", "errors": ["too long"], "retry": False})
    state = bench.state
    assert [(row.row_id, code) for row, code, _detail in state.repairs_failed(2)] == [(bench.row, "checker")]
    assert state.repairs_needing_calls(2) == []  # a repair that failed the checker is not written a third time
    # a row the checker set aside is the same
    other = Bench()
    call = in_flight(other)
    (set_aside,) = other.b.returned(call, handle_id="batch-2")
    other.b.add("row_set_aside", {"row_id": set_aside, "call_id": call, "class": "other", "reason": "nothing to say"})
    assert [code for _row, code, _detail in other.state.repairs_failed(2)] == ["checker"]


def test_a_row_that_goes_back_to_the_hand_queue_after_its_repair_keeps_the_record_and_is_never_a_candidate_again():
    bench = Bench()
    call = in_flight(bench)
    (new_row,) = bench.b.returned(call, handle_id="batch-2")
    bench.b.add("row_assessed", {"row_id": new_row, "call_id": call, "passed": False, "flags": [], "errors": ["too long"]})
    bench.b.add("row_rejected", {"row_id": new_row, "call_id": call, "by": "assessor", "reason": "failed assessment", "errors": ["too long"], "retry": False})
    reason = repair.failure_reason("checker")
    bench.b.add("row_to_hand", {"row_id": bench.row, "call_id": bench.call, "reason": reason, "verdicts": ["rev-a: fix"]})
    state = bench.state
    row = state.rows[bench.row]
    assert row.status == "hand" and row.hand["reason"] == reason and [r.row_id for r in state.hand_queue()] == [bench.row]
    assert row.repair["failed"] == reason and row.repair["calls"] == [call] and row.repair["rows"] == [new_row]
    assert not repair.is_candidate(row) and repair.candidates(state.hand_queue()) == []
    assert state.rows_being_repaired() == [] and state.repairs_failed(2) == []
    # the owner may answer it like any row in the queue
    bench.b.add("hand_resolved", {"row_id": bench.row, "call_id": bench.call, "decision": "discard"})
    assert bench.state.rows[bench.row].status == "rejected"


# ------------------------------------------------------------------ rebuilding


def test_replaying_the_same_events_twice_gives_the_same_state():
    bench = Bench(notes=("one", "two"))
    call = in_flight(bench)
    (new_row,) = bench.b.returned(call, handle_id="batch-2")
    bench.b.assessed(new_row, call)
    once = State.from_events(bench.b.events)
    twice = State()
    for event in bench.b.events:
        twice.apply(event)
    for event in bench.b.events:
        twice.apply(event)
    assert once.snapshot() == twice.snapshot() == State.from_events(bench.b.events).snapshot()
    assert once.rows[bench.row].status == "repaired" and once.bad_events == [] and twice.bad_events == []
    assert twice.next_attempt(bench.task, "001") == once.next_attempt(bench.task, "001") == 3


def test_the_counts_and_the_task_ledger_see_rows_that_were_sent_back():
    from helpers import make_config
    from rps.ledger import Ledger, PriceTable

    bench = Bench()
    call = in_flight(bench)
    state = bench.state
    assert state.counts()["rows"] == {"repairing": 1} and state.counts()["calls"] == {"returned": 1, "submitted": 1}
    (new_row,) = bench.b.returned(call, handle_id="batch-2")
    bench.b.assessed(new_row, call)
    state = bench.state
    assert state.counts()["rows"] == {"repaired": 1, "passed": 1}
    (line,) = Ledger(make_config(), PriceTable.default()).task_lines(state)
    assert line["rows_out"] == 2 and line["rows_passed"] == 2 and line["rows_accepted"] == 0
    assert line["usd_by_purpose"] == {"production": pytest.approx(0.005), "repair": pytest.approx(0.005)}
