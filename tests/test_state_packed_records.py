"""A finished row holds its record packed (compressed JSON text) and reads it back unchanged.

The state of a long job is mostly the records of rows that are finished (accepted, rejected, set aside or repaired).
Such a row keeps its record packed; `row.record` decodes it on each read into a dictionary equal to the one written.
Nothing a run records, writes or decides may differ from a state that keeps every record as objects.
"""

import copy
import dataclasses
import functools
import json
import math
import pickle
import sys
from pathlib import Path

import rps.controller
from rps.events import Event
from rps.provenance import rows_to_lines
from rps.state import TERMINAL_ROW_STATUSES, RowState, State

from harness import World, task_spec
from helpers import Builder
from test_state import lifecycle, passed_row, publish, verdict


def both(events):
    """The state with finished records packed (the default) and with every record kept as objects."""
    return State.from_events(events), State.from_events(events, pack_finished=False)


def assert_same(packed: State, plain: State) -> None:
    assert packed.snapshot() == plain.snapshot()
    assert packed.counts() == plain.counts()
    assert packed.job_progress(2) == plain.job_progress(2)
    for row_id, row in plain.rows.items():
        assert packed.rows[row_id].record == row.record
        assert packed.rows[row_id] == row


def test_an_accepted_row_holds_its_record_packed_and_reads_it_back_equal():
    events = lifecycle().events
    packed, plain = both(events)
    (row_id,) = [row_id for row_id, row in packed.rows.items() if row.status == "accepted"]
    row = packed.rows[row_id]
    assert row.record_packed and type(row.__dict__["_record"]) is bytes
    assert not plain.rows[row_id].record_packed
    assert row.record == plain.rows[row_id].record
    assert list(row.record) == list(plain.rows[row_id].record)  # the keys keep their order
    assert_same(packed, plain)


def test_the_accepted_rows_file_is_the_same_text_whether_the_records_are_packed_or_not():
    packed, plain = both(lifecycle().events)
    for state in (packed, plain):
        assert state.files_to_ship() == plain.files_to_ship()
    texts = [
        {key: rows_to_lines([row.record for row in rows]) for key, rows in state.accepted_rows_by_file().items()}
        for state in (packed, plain)
    ]
    assert texts[0] == texts[1] and texts[0]


def test_each_read_of_a_packed_record_is_a_new_dictionary_and_the_packed_record_does_not_change():
    packed, _plain = both(lifecycle().events)
    row = next(row for row in packed.rows.values() if row.record_packed)
    first = row.record
    first["thoughts"] = ["changed by a caller"]
    assert row.record != first and row.record is not row.record


def test_a_row_still_in_review_or_in_the_hand_queue_keeps_its_record_as_objects():
    b = Builder()
    row_id, call = passed_row(b)
    state = State.from_events(b.events)
    assert state.rows[row_id].status == "passed" and not state.rows[row_id].record_packed
    publish(b, row_id, "p0123456789ab")
    state = State.from_events(b.events)
    assert state.rows[row_id].status == "in_review" and not state.rows[row_id].record_packed
    b.add("row_to_hand", {"row_id": row_id, "call_id": call, "reason": "three reviewers without a settlement", "verdicts": []})
    state = State.from_events(b.events)
    assert state.rows[row_id].status == "hand" and not state.rows[row_id].record_packed


def test_every_way_a_row_finishes_packs_its_record():
    b = Builder()
    task = b.task(lines={"001": "a", "002": "b", "003": "c", "004": "d", "005": "e", "006": "f"})
    calls = [b.prepare(task, line) for line in ("001", "002", "003", "004", "005", "006")]
    b.submit(calls)
    rows = [b.returned(call)[0] for call in calls]
    # rejected by the checker
    b.assessed(rows[0], calls[0], passed=False, errors=["bad"])
    b.add("row_rejected", {"row_id": rows[0], "call_id": calls[0], "by": "assessor", "reason": "failed", "errors": ["bad"], "retry": True})
    # set aside
    b.add("row_set_aside", {"row_id": rows[1], "call_id": calls[1], "reason": "not a fit"})
    # accepted, discarded and replaced by the owner from the hand queue
    for row_id, call, decision in ((rows[2], calls[2], "accept"), (rows[3], calls[3], "discard"), (rows[4], calls[4], "replace")):
        b.assessed(row_id, call)
        b.add("row_to_hand", {"row_id": row_id, "call_id": call, "reason": "for the owner", "verdicts": []})
        payload = {"row_id": row_id, "call_id": call, "decision": decision}
        if decision == "replace":
            payload["row"] = {**b.row(call), "row_id": f"{row_id}-h1", "thoughts": ["written by the owner"]}
        b.add("hand_resolved", payload)
    # accepted by the reviewers
    b.assessed(rows[5], calls[5])
    publish(b, rows[5], "p0123456789ab")
    verdict(b, rows[5], "p0123456789ab", "keep")
    b.add("row_accepted", {"row_id": rows[5], "call_id": calls[5], "by": "reviewers"})
    packed, plain = both(b.events)
    statuses = {row.status for row in packed.rows.values()}
    assert {"accepted", "rejected", "set_aside"} <= statuses
    assert f"{rows[4]}-h1" in packed.rows
    for row in packed.rows.values():
        assert row.record_packed == (row.status in TERMINAL_ROW_STATUSES), (row.row_id, row.status)
    assert_same(packed, plain)


def test_a_row_written_again_is_packed_when_its_repair_passes_the_checker():
    b = Builder()
    task = b.task(lines={"001": "a"})
    call = b.prepare(task, "001")
    b.submit([call])
    (row_id,) = b.returned(call)
    b.assessed(row_id, call)
    b.add("row_to_hand", {"row_id": row_id, "call_id": call, "reason": "a reviewer asked for a fix", "verdicts": []})
    repair_call = b.prepare(task, "001", attempt=2, repair_of=row_id)
    b.add("repair_requested", {"row_id": row_id, "call_id": repair_call, "route": "fake-batch"})
    b.submit([repair_call], handle_id="batch-2")
    (new_row,) = b.returned(repair_call, handle_id="batch-2")
    assert not State.from_events(b.events).rows[row_id].record_packed  # repairing: not finished
    b.assessed(new_row, repair_call)
    packed, plain = both(b.events)
    assert packed.rows[row_id].status == "repaired" and packed.rows[row_id].record_packed
    assert not packed.rows[new_row].record_packed
    assert_same(packed, plain)


def test_an_accepted_row_an_audit_rejects_holds_its_record_as_objects_again():
    b = lifecycle()
    (row_id,) = [r for r, row in State.from_events(b.events).rows.items() if row.status == "accepted"]
    call = State.from_events(b.events).rows[row_id].call_id
    publish(b, row_id, "p0123456789ac", reviewer="rev-b", family="beta", round_=2, audit=True)
    verdict(b, row_id, "p0123456789ac", "reject", reviewer="rev-b", family="beta")
    b.add("row_escalated", {"row_id": row_id, "call_id": call, "reason": "audit_reject", "round": 2, "families_used": ["alpha", "beta"]})
    packed, plain = both(b.events)
    assert packed.rows[row_id].status == "in_question" and not packed.rows[row_id].record_packed
    assert_same(packed, plain)


def test_a_record_that_would_not_read_back_equal_is_left_as_it_is():
    for record in ({"row_id": "r", "values": (1, 2)}, {"row_id": "r", 1: "a"}, {"row_id": "r", "x": float("nan")}, {"row_id": "r", "x": object()}):
        row = RowState(row_id="r", call_id="c", task_id="t", line="l", route="x", record=record)
        assert row.pack_record() is False and not row.record_packed
        assert row.record is record


def test_a_finished_call_holds_its_feedback_packed_and_reads_it_back_equal():
    b = Builder()
    task = b.task(lines={"001": "a", "002": "b"})
    notes = ["a note", "the row as it was written: {\"thoughts\": [\"x\"]}"]
    done = b.prepare(task, "001", feedback=notes)
    waiting = b.prepare(task, "002", feedback=notes)
    b.submit([done, waiting])
    b.failed(done, error="overloaded")
    packed, plain = both(b.events)
    assert type(packed.calls[done].__dict__["_feedback"]) is bytes
    assert type(packed.calls[waiting].__dict__["_feedback"]) is list  # still in flight: kept as it is
    assert packed.calls[done].feedback == notes == plain.calls[done].feedback
    assert type(plain.calls[done].__dict__["_feedback"]) is list
    assert_same(packed, plain)
    assert packed.calls == plain.calls


def test_texts_that_recur_are_shared_and_read_the_same():
    b = lifecycle()
    packed, plain = both(b.events)
    row = next(row for row in packed.rows.values() if row.verdicts)
    verdict_ = row.verdicts[0]
    assert verdict_["reviewer_id"] is sys.intern("rev-a") and verdict_["verdict"] is sys.intern("keep")
    call = packed.calls[row.call_id]
    assert all(key is sys.intern(key) for key in call.usage)
    assert_same(packed, plain)


def through_the_log(events):
    """The events as a replay reads them back from the log's text."""
    return [Event.from_line(event.to_line()) for event in events]


def test_a_lone_surrogate_in_a_record_or_a_feedback_note_changes_nothing():
    b = Builder()
    task = b.task(lines={"001": "a"})
    call = b.prepare(task, "001", feedback=["note \ud83d", "plain"])
    b.submit([call])
    record = {**b.row(call), "thoughts": ["half a pair \ud83d here"]}
    b.add("call_returned", {"call_id": call, "handle_id": "batch-1", "usage": {"input_tokens": 1, "output_tokens": 1},
                            "usd": 0.001, "price_table_date": "2026-11-06", "minutes": 0.1, "rows": [record], "row_flags": {}, "skips": []})
    b.assessed(record["row_id"], call)
    publish(b, record["row_id"], "p0123456789ab")
    verdict(b, record["row_id"], "p0123456789ab", "keep")
    b.add("row_accepted", {"row_id": record["row_id"], "call_id": call, "by": "reviewers"})
    for events in (b.events, through_the_log(b.events)):
        packed, plain = both(events)
        assert packed.bad_events == [] == plain.bad_events
        row = packed.rows[record["row_id"]]
        assert row.status == "accepted" and row.record == record and row.record["thoughts"] == ["half a pair \ud83d here"]
        assert row.record_packed  # ASCII escapes keep a lone surrogate exactly
        assert packed.calls[call].feedback == ["note \ud83d", "plain"]
        assert type(packed.calls[call]._feedback) is bytes
        assert_same(packed, plain)
        assert packed.calls == plain.calls


def test_non_ascii_text_infinity_and_negative_zero_read_back_exactly_from_a_packed_record():
    record = {"row_id": "r", "thoughts": ["café 東京 \U0001f600"], "big": float("inf"), "small": float("-inf"), "zero": -0.0, "n": 10**30}
    row = RowState(row_id="r", call_id="c", task_id="t", line="l", route="x", record=dict(record))
    assert row.pack_record() and row.record_packed
    back = row.record
    assert back == record and list(back) == list(record)
    assert math.copysign(1.0, back["zero"]) == -1.0 and back["big"] == math.inf and type(back["n"]) is int


def test_a_feedback_of_odd_texts_is_packed_and_reads_back_exactly():
    notes = ["", "line\nbreak", "tab\there", 'quote " and back\\slash', "nul \u0000", "\U0001f600", "\ud83d", "\udc00 alone"]
    b = Builder()
    task = b.task(lines={"001": "a"})
    call = b.prepare(task, "001", feedback=notes)
    b.submit([call])
    b.failed(call, error="overloaded")
    for events in (b.events, through_the_log(b.events)):
        packed, plain = both(events)
        assert type(packed.calls[call]._feedback) is bytes
        assert packed.calls[call].feedback == notes == plain.calls[call].feedback
        assert packed.bad_events == []
        assert_same(packed, plain)


def test_a_row_accepted_again_after_an_audit_rejected_it_is_packed_again():
    b = lifecycle()
    first = State.from_events(b.events)
    (row_id,) = [r for r, row in first.rows.items() if row.status == "accepted"]
    call = first.rows[row_id].call_id
    publish(b, row_id, "p0123456789ac", reviewer="rev-b", family="beta", round_=2, audit=True)
    verdict(b, row_id, "p0123456789ac", "reject", reviewer="rev-b", family="beta")
    b.add("row_escalated", {"row_id": row_id, "call_id": call, "reason": "audit_reject", "round": 2, "families_used": ["alpha", "beta"]})
    middle = State.from_events(b.events)
    assert middle.rows[row_id].status == "in_question" and not middle.rows[row_id].record_packed
    publish(b, row_id, "p0123456789ad", reviewer="rev-c", family="gamma", round_=3)
    verdict(b, row_id, "p0123456789ad", "keep", reviewer="rev-c", family="gamma")
    b.add("row_accepted", {"row_id": row_id, "call_id": call, "by": "reviewers"})
    packed, plain = both(b.events)
    assert packed.rows[row_id].status == "accepted" and packed.rows[row_id].record_packed
    assert_same(packed, plain)


def test_copy_deepcopy_and_pickle_keep_a_packed_row_and_a_state_as_they_were():
    b = Builder()
    row_id, _call = passed_row(b)  # stays open: its record is plain
    events = lifecycle().events + b.events
    packed, plain = both(events)
    held = [row for row in packed.rows.values() if row.record_packed]
    assert held and any(not row.record_packed for row in packed.rows.values())
    for row in held:
        for made in (copy.copy(row), copy.deepcopy(row), pickle.loads(pickle.dumps(row))):
            assert made == row and made.record == row.record and made.record_packed
    for made in (copy.deepcopy(packed), pickle.loads(pickle.dumps(packed))):
        assert made.snapshot() == plain.snapshot()
        assert {r: row.record_packed for r, row in made.rows.items()} == {r: row.record_packed for r, row in packed.rows.items()}
        assert made.rows[row_id].record == plain.rows[row_id].record


def surrogate_rows(payload):
    """A writer whose rows carry a lone surrogate in their text (sent escaped, as JSON allows)."""
    from rps.providers import FakeOutcome

    row = {"id": f"{payload['line']}-{payload['attempt']}", "thoughts": ["a thought with half a pair \ud83d in it"]}
    return FakeOutcome(output=json.dumps(row))


def test_a_job_whose_rows_carry_a_lone_surrogate_runs_the_same_whether_records_are_packed_or_not(tmp_path, monkeypatch):
    packed_ctl, packed_events, packed_files = run_world(tmp_path / "packed", monkeypatch, True, behavior=surrogate_rows)
    plain_ctl, plain_events, plain_files = run_world(tmp_path / "plain", monkeypatch, False, behavior=surrogate_rows)
    assert json.dumps(packed_events, sort_keys=True) == json.dumps(plain_events, sort_keys=True)
    assert packed_files == plain_files and any(path.startswith("accepted/") for path in packed_files)
    assert packed_ctl.format_status() == plain_ctl.format_status()
    assert packed_ctl.state.bad_events == [] == plain_ctl.state.bad_events
    accepted = [row for row in packed_ctl.state.rows.values() if row.status == "accepted"]
    assert accepted and all(row.record_packed and "\ud83d" in row.record["thoughts"][0] for row in accepted)


def test_the_dataclass_reads_and_writes_the_record_as_before():
    row = RowState(row_id="r", call_id="c", task_id="t", line="l", route="x", record={"row_id": "r", "a": [1, 2.5, None, True]})
    plain = dataclasses.replace(row)
    assert row.pack_record() and row.record_packed
    assert dataclasses.asdict(row) == dataclasses.asdict(plain)
    assert row == plain and repr(row) == repr(plain)
    copy = dataclasses.replace(row)
    assert copy.record == plain.record and not copy.record_packed
    row.record = {"row_id": "r", "b": 1}  # an assignment replaces the packed record
    assert not row.record_packed and row.record == {"row_id": "r", "b": 1}
    row.pack_record()
    row.unpack_record()
    assert not row.record_packed and row.record == {"row_id": "r", "b": 1}


def run_world(tmp_path: Path, monkeypatch, pack: bool, behavior=None):
    monkeypatch.setattr(rps.controller, "State", functools.partial(State, pack_finished=pack))
    world = World(tmp_path, behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec())
    world.drive(controller)
    assert controller.state.pack_finished is pack
    events = [(event.kind, event.payload) for event in world.events()]
    files = {}
    for store in (world.state_store, world.ship_store):
        for path in store.list(""):
            if not path.startswith("events/"):
                files[path] = store.read_bytes(path)
    return controller, events, files


def test_a_run_writes_the_same_events_and_files_whether_finished_records_are_packed_or_not(tmp_path, monkeypatch):
    packed_ctl, packed_events, packed_files = run_world(tmp_path / "packed", monkeypatch, True)
    plain_ctl, plain_events, plain_files = run_world(tmp_path / "plain", monkeypatch, False)
    assert json.dumps(packed_events, sort_keys=True) == json.dumps(plain_events, sort_keys=True)
    assert packed_files == plain_files and any(path.startswith("accepted/") for path in packed_files)
    assert packed_ctl.format_status() == plain_ctl.format_status()
    assert any(row.record_packed for row in packed_ctl.state.rows.values())
    assert not any(row.record_packed for row in plain_ctl.state.rows.values())


def test_a_restart_on_the_same_log_reads_packed_records_back(tmp_path):
    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec())
    world.drive(first)
    second = world.controller()
    second.start()
    assert second.state.rows and all(row.record_packed for row in second.state.rows.values() if row.status == "accepted")
    assert sorted(row.record["row_id"] for row in second.state.rows.values() if row.status == "accepted") == sorted(
        row["row_id"] for row in world.shipped_rows()
    )
