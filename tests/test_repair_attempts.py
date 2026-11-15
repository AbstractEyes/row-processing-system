"""Repair attempts and repair rounds (0.6.3): `repair.attempts` calls for one row, `repair.rounds` rounds along the chain
of rows that repair one another. The defaults (1 and 1) are the repair step as it was."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from rps import ids, repair
from rps.assess import Assessment
from rps.config import Config, ConfigError, RepairConfig
from rps.events import replay
from rps.prompting import REPAIR_CHECK_HEAD, REPAIR_NOTES_HEAD, REPAIR_ROW_HEAD, repair_text
from rps.providers import FakeOutcome, SimulatedCrash
from rps.repair import CHECK_LINE_START, FAILED_CHECKER_REASON, NO_ROW_REASON
from rps.review.policy import FIX_TO_HAND_REASON
from rps.state import RowState, State

from harness import World, task_spec
from repair_helpers import FIX_REASON, repair_config, sends_back, writes_and_rewrites
from review_helpers import crash_after, outcome
from test_config_repair import make

LINES = ("001", "002")
ERROR = "thought 1 is too long"
CALL_FAILED = "the repair call failed (provider_error): the row waits for the owner"


# ------------------------------------------------------------------ the fake writer and the checkers


def numbered(payload):
    """A writer whose repaired row says which attempt wrote it (the attempt number of the call)."""
    if payload.get("repair_of"):
        row = {"id": f"{payload['line']}-r", "thoughts": [f"repaired{payload['attempt']}: a better thought about {payload['spec']}"]}
    else:
        row = {"id": f"{payload['line']}-{payload['attempt']}", "thoughts": [f"a thought about {payload['spec']}"]}
    return FakeOutcome(output=json.dumps(row))


def repaired_attempt(row):
    """The attempt number a repaired row names (None for a row no repair wrote)."""
    text = row["thoughts"][0]
    return int(text.split(":")[0][len("repaired"):]) if text.startswith("repaired") else None


def fails_until(attempt):
    """A checker that refuses a repaired row written by a call before `attempt`."""

    def assess(row, context):
        written = repaired_attempt(row)
        if written is not None and written < attempt:
            return Assessment(passed=False, errors=(ERROR,))
        return Assessment(passed=True)

    return assess


def drive(tmp_path, *, config, behavior=numbered, assess=None, verdict=None, fix=("001",), lines=LINES):
    world = World(tmp_path, config=config, behavior=behavior)
    controller = world.controller(assess=assess)
    controller.add_task(task_spec(lines))
    world.drive(controller, verdict or sends_back(*fix))
    return world, controller


def first_rows(controller):
    return {row.line: row for row in controller.state.rows.values() if row.repair_of is None}


def repair_calls(world, **match):
    return [e.payload for e in world.events_of("call_prepared") if e.payload["purpose"] == "repair" and all(e.payload.get(k) == v for k, v in match.items())]


def reasons(world):
    return [e.payload["reason"] for e in world.events_of("row_to_hand")]


# ------------------------------------------------------------------ the configuration


def test_attempts_and_rounds_default_to_one_and_are_left_out_of_the_written_section():
    config = Config.from_dict(make())
    assert (config.repair.attempts, config.repair.rounds) == (1, 1)
    assert config.to_dict()["repair"] == {"enabled": False, "min_rows": 20}
    assert Config.from_dict(make(repair={"attempts": 1, "rounds": 1})).to_dict()["repair"] == {"enabled": False, "min_rows": 20}
    assert Config.from_dict(make(repair={"attempts": 1})).digest() == Config.from_dict(make()).digest()


def test_attempts_and_rounds_are_read_and_written_back():
    config = Config.from_dict(make(repair={"enabled": True, "attempts": 3, "rounds": 2}))
    assert (config.repair.attempts, config.repair.rounds) == (3, 2)
    assert config.to_dict()["repair"] == {"enabled": True, "min_rows": 20, "attempts": 3, "rounds": 2}
    assert Config.from_dict(config.to_dict()).repair == config.repair


@pytest.mark.parametrize("key", ["attempts", "rounds"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "2", True])
def test_attempts_and_rounds_are_whole_numbers_of_at_least_one(key, value):
    with pytest.raises(ConfigError, match=f"repair.{key}"):
        Config.from_dict(make(repair={key: value}))


# ------------------------------------------------------------------ the candidate rule


def hand_row(row_id="r1", *, reason=FIX_REASON, repair_record=None, repair_of=None, status="hand", decision=False):
    hand = {"reason": reason, "ts": "2026-11-07T09:00:00.000000Z", **({"decision": "accept"} if decision else {})}
    return RowState(
        row_id=row_id, call_id="c", task_id="t", line="001", route="fake-batch", status=status, hand=hand, record={},
        repair=repair_record, repair_of=repair_of,
    )


class Call:
    def __init__(self, retry_of=None):
        self.retry_of = retry_of


def failed_record(reason, calls=("c2",)):
    return {"call_id": calls[0], "route": "fake-batch", "calls": list(calls), "rows": [], "failed": reason}


def test_a_row_a_repair_wrote_is_a_candidate_only_while_the_rounds_allow_it():
    first = hand_row("r1")
    again = hand_row("r2", repair_of="r1")
    third = hand_row("r3", repair_of="r2")
    rows = {row.row_id: row for row in (first, again, third)}
    assert [repair.repair_round(row, rows) for row in (first, again, third)] == [0, 1, 2]
    assert [repair.is_candidate(row, rows=rows) for row in (first, again, third)] == [True, False, False]  # today's rule
    assert [repair.is_candidate(row, rows=rows, rounds=2) for row in (first, again, third)] == [True, True, False]
    assert [repair.is_candidate(row, rows=rows, rounds=3) for row in (first, again, third)] == [True, True, True]
    assert repair.is_candidate(again, rounds=2) and not repair.is_candidate(again)  # without the rows the chain is one link


def test_a_row_sent_back_before_is_not_a_candidate_for_its_fix_whatever_the_rounds():
    assert not repair.is_candidate(hand_row(repair_record={"call_id": "c2", "route": "x", "calls": [], "rows": []}), rounds=2)


@pytest.mark.parametrize("reason", [FAILED_CHECKER_REASON, CALL_FAILED, "the repair call failed: the row waits for the owner"])
def test_a_row_whose_repair_failed_is_a_candidate_while_it_has_attempts_left(reason):
    row = hand_row(reason=reason, repair_record=failed_record(reason))
    calls = {"c2": Call()}
    assert not repair.is_candidate(row, calls=calls)  # one attempt used, one allowed: as before
    assert not repair.is_candidate(row, calls=calls, attempts=1)
    assert repair.is_candidate(row, calls=calls, attempts=2)
    assert repair.is_candidate(row, calls=calls, attempts=2, rounds=2)


def test_a_retry_of_a_failed_call_is_not_an_attempt_of_its_own():
    reason = CALL_FAILED
    row = hand_row(reason=reason, repair_record=failed_record(reason, ("c2", "c3")))
    calls = {"c2": Call(), "c3": Call(retry_of="c2")}
    assert repair.attempts_used(row, calls) == 1
    assert repair.is_candidate(row, calls=calls, attempts=2) and not repair.is_candidate(row, calls=calls, attempts=1)
    calls = {"c2": Call(), "c3": Call()}
    assert repair.attempts_used(row, calls) == 2
    assert not repair.is_candidate(row, calls=calls, attempts=2) and repair.is_candidate(row, calls=calls, attempts=3)


@pytest.mark.parametrize(
    "reason",
    [NO_ROW_REASON, "the repair was not made, the step or the route's repair cap is switched off: the row waits for the owner"],
)
def test_a_declined_or_unmade_repair_stays_in_the_queue(reason):
    row = hand_row(reason=reason, repair_record=failed_record(reason))
    assert not repair.is_candidate(row, calls={"c2": Call()}, attempts=5, rounds=5)


def test_the_owner_has_the_last_word_on_a_row_waiting_after_a_failed_repair():
    row = hand_row(reason=FAILED_CHECKER_REASON, repair_record=failed_record(FAILED_CHECKER_REASON), decision=True)
    assert not repair.is_candidate(row, calls={"c2": Call()}, attempts=3)
    other = hand_row(reason="3 reviewers without a settlement")
    assert not repair.is_candidate(other, attempts=3, rounds=3)


def test_candidates_come_in_row_id_order_with_the_settings():
    rows = [
        hand_row("r3"),
        hand_row("r2", reason=FAILED_CHECKER_REASON, repair_record=failed_record(FAILED_CHECKER_REASON)),
        hand_row("r1"),
    ]
    calls = {"c2": Call()}
    assert [row.row_id for row in repair.candidates(rows, calls=calls)] == ["r1", "r3"]
    assert [row.row_id for row in repair.candidates(rows, calls=calls, attempts=2)] == ["r1", "r2", "r3"]


# ------------------------------------------------------------------ what a later attempt is told


def test_a_later_attempt_is_told_the_checkers_errors_between_the_notes_and_the_row():
    feedback = ("the thought names a pose", CHECK_LINE_START + "thought 1 is too long", CHECK_LINE_START + "caption repeats", '{"id": 1}')
    text = repair_text(feedback)
    assert text.index(REPAIR_NOTES_HEAD) < text.index(REPAIR_CHECK_HEAD) < text.index(REPAIR_ROW_HEAD)
    assert "- thought 1 is too long\n- caption repeats\n\n" + REPAIR_ROW_HEAD in text
    assert CHECK_LINE_START not in text and text.count("the thought names a pose") == 1
    assert REPAIR_CHECK_HEAD not in repair_text(("the thought names a pose", '{"id": 1}'))  # a first attempt: as before


# ------------------------------------------------------------------ (a) two refusals, then a pass


def test_a_repair_that_fails_the_checker_twice_passes_at_the_third_attempt(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(attempts=3), assess=fails_until(4))
    state, first = controller.state, first_rows(controller)
    row = first["001"]
    calls = repair_calls(world, repair_of=row.row_id)
    assert [(c["attempt"], c["retry_of"]) for c in calls] == [(2, None), (3, None), (4, None)]
    assert len(world.events_of("repair_requested")) == 1  # one request; the attempts after it are calls of the same repair
    assert row.status == "repaired" and state.hand_queue() == []
    written = [r for r in state.rows.values() if r.repair_of == row.row_id]
    assert [r.status for r in sorted(written, key=lambda r: r.row_id)] == ["rejected", "rejected", "accepted"]
    assert row.repair["calls"] == [c["call_id"] for c in calls] and row.repair["rows"] == [written[-1].row_id]
    # attempt 1 of the repair carries the notes and the row only; the next two add the checker's errors
    assert not any(CHECK_LINE_START in line for line in calls[0]["feedback"])
    for later in calls[1:]:
        assert later["feedback"].count(CHECK_LINE_START + ERROR) == 1
        assert later["feedback"][-1] == calls[0]["feedback"][-1]  # the row as it was written, the same
        assert later["feedback"][0] == calls[0]["feedback"][0]  # the reviewers' notes, the same
    assert state.spend("fake-batch", "repair").calls == 3 and state.spend("fake-batch", "rebound").calls == 0
    assert reasons(world) == [FIX_REASON]  # it never went back to the queue
    shipped = {r["row_id"] for r in world.shipped_rows()}
    assert shipped == {first["002"].row_id, max(r.row_id for r in written if r.status == "accepted")}
    assert controller.is_idle()


# ------------------------------------------------------------------ (b) the attempts are used up


def test_when_the_attempts_are_used_up_the_row_goes_to_the_queue_with_todays_reason(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(attempts=2), assess=fails_until(99))
    row = first_rows(controller)["001"]
    assert len(repair_calls(world)) == 2 and len(world.events_of("repair_requested")) == 1
    assert row.status == "hand" and row.hand["reason"] == FAILED_CHECKER_REASON == "the repair failed the checker: the row waits for the owner"
    assert reasons(world) == [FIX_REASON, FAILED_CHECKER_REASON] and len(row.repair["calls"]) == 2
    for _ in range(3):
        assert controller.step() == 0  # it is not tried again
    assert controller.state.spend("fake-batch", "repair").calls == 2


def test_a_repair_call_that_fails_is_made_again_until_the_attempts_are_used_and_the_row_then_waits(tmp_path):
    def behavior(payload):
        return FakeOutcome(error="declined", retryable=False, kind="refused") if payload["repair_of"] else numbered(payload)

    world, controller = drive(tmp_path, config=repair_config(attempts=3), behavior=behavior)
    row = first_rows(controller)["001"]
    calls = repair_calls(world)
    assert [(c["attempt"], c["retry_of"]) for c in calls] == [(2, None), (3, None), (4, None)]
    assert calls[0]["feedback"] == calls[1]["feedback"] == calls[2]["feedback"]  # a failed call leaves no checker error to tell
    assert row.hand["reason"] == "the repair call failed (refused): the row waits for the owner"
    assert reasons(world)[-1] == row.hand["reason"] and len(world.events_of("repair_requested")) == 1


def test_a_call_that_fails_for_a_passing_reason_is_retried_inside_its_attempt(tmp_path):
    def behavior(payload):
        if payload["repair_of"] and payload["attempt"] == 2:
            return FakeOutcome(error="overloaded", retryable=True)
        return numbered(payload)

    world, controller = drive(tmp_path, config=repair_config(attempts=2), behavior=behavior)
    first, second = repair_calls(world)
    assert (first["attempt"], first["retry_of"], second["attempt"], second["retry_of"]) == (2, None, 3, first["call_id"])
    assert first_rows(controller)["001"].status == "repaired" and len(repair_calls(world)) == 2


# ------------------------------------------------------------------ (c) a declined repair is not tried again


def test_a_repair_the_writer_declined_is_not_tried_again(tmp_path):
    declined = FakeOutcome(output=json.dumps({"skip": True, "class": "other", "reason": "cannot be written again", "rows": []}))
    behavior = lambda payload: declined if payload["repair_of"] else numbered(payload)
    world, controller = drive(tmp_path, config=repair_config(attempts=3, rounds=2), behavior=behavior)
    row = first_rows(controller)["001"]
    assert len(repair_calls(world)) == 1 and row.status == "hand" and row.hand["reason"] == NO_ROW_REASON
    for _ in range(3):
        assert controller.step() == 0
    assert len(world.events_of("repair_requested")) == 1 and len(repair_calls(world)) == 1


# ------------------------------------------------------------------ (d) a row waiting from an earlier run


def test_a_row_waiting_after_a_failed_repair_is_picked_up_again_when_the_configuration_allows_more_attempts(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(), assess=fails_until(3))  # one attempt: it fails and waits
    row = first_rows(controller)["001"]
    assert row.status == "hand" and row.hand["reason"] == FAILED_CHECKER_REASON and len(repair_calls(world)) == 1
    world.config = repair_config(attempts=2)
    later = world.controller(assess=fails_until(3))
    world.drive(later, sends_back("001"))
    row = later.state.rows[row.row_id]
    assert row.status == "repaired" and len(world.events_of("repair_requested")) == 2
    again = repair_calls(world, repair_of=row.row_id)
    assert [c["attempt"] for c in again] == [2, 3]
    assert again[1]["feedback"].count(CHECK_LINE_START + ERROR) == 1 and "failed" not in row.repair
    assert reasons(world) == [FIX_REASON, FAILED_CHECKER_REASON]
    assert later.state.hand_queue() == [] and len(world.shipped_rows()) == 2


def test_a_row_waiting_after_a_failed_repair_stays_when_the_attempts_are_the_same_or_the_owner_answered(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(), assess=fails_until(99))
    row = first_rows(controller)["001"]
    again = world.controller(assess=fails_until(99))
    for _ in range(3):
        assert again.step() == 0
    assert len(world.events_of("repair_requested")) == 1 and again.state.rows[row.row_id].status == "hand"
    from review_helpers import write_hand_decisions

    write_hand_decisions(world, [{"row_id": row.row_id, "decision": "accept"}])
    world.config = repair_config(attempts=3)
    last = world.controller(assess=fails_until(99))
    last.step()
    last.step()
    assert last.state.rows[row.row_id].status == "accepted" and len(world.events_of("repair_requested")) == 1


def test_a_row_waiting_for_a_declined_repair_is_not_picked_up_when_the_attempts_grow(tmp_path):
    declined = FakeOutcome(output=json.dumps({"skip": True, "class": "other", "reason": "no", "rows": []}))
    world, controller = drive(tmp_path, config=repair_config(), behavior=lambda p: declined if p["repair_of"] else numbered(p))
    world.config = repair_config(attempts=4, rounds=2)
    later = world.controller()
    for _ in range(3):
        assert later.step() == 0
    assert len(world.events_of("repair_requested")) == 1


# ------------------------------------------------------------------ (e) a second round


def test_a_row_a_repair_wrote_is_sent_back_once_more_with_two_rounds_and_a_fix_on_the_second_goes_to_the_owner(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(rounds=2), verdict=sends_back("001", repaired="fix"))
    state, first = controller.state, first_rows(controller)
    original = first["001"]
    round_one = next(r for r in state.rows.values() if r.repair_of == original.row_id)
    round_two = next(r for r in state.rows.values() if r.repair_of == round_one.row_id)
    assert [repair.repair_round(r, state.rows) for r in (original, round_one, round_two)] == [0, 1, 2]
    assert original.status == "repaired" and round_one.status == "repaired"
    assert round_two.status == "hand" and round_two.hand["reason"] == FIX_TO_HAND_REASON
    assert [e.payload["row_id"] for e in world.events_of("repair_requested")] == [original.row_id, round_one.row_id]
    # the second round's call carries the notes of the fix verdicts on the repair row, and the repair row as the earlier row
    (first_call,) = repair_calls(world, repair_of=original.row_id)
    (second_call,) = repair_calls(world, repair_of=round_one.row_id)
    assert second_call["feedback"][-1] == repair.written_row_text(round_one.record) != first_call["feedback"][-1]
    assert second_call["feedback"][:-1] == [f"line 001: thought 1 states a pose that no record gives"] * len(round_one.verdicts)
    assert second_call["feedback"][-1] != repair.written_row_text(original.record)
    assert state.spend("fake-batch", "repair").calls == 2
    # a fix on the second-round row is not sent back again
    for _ in range(4):
        assert controller.step() == 0
    assert len(world.events_of("repair_requested")) == 2
    assert [r.row_id for r in state.hand_queue()] == [round_two.row_id]
    assert {r["row_id"] for r in world.shipped_rows()} == {first["002"].row_id}
    assert state.line_status(original.task_id, "001", 3).outcome == "active"


def test_the_second_rounds_row_is_reviewed_and_ships_like_a_first_rounds(tmp_path):
    # the original and the first repair get a fix, the second repair a keep: the line is accepted through the whole chain
    def verdict(reviewer, item):
        if item.task["line_id"] == "001" and repaired_attempt(item.row) in (None, 2):
            return {"verdict": "fix", "reasons": ["needs_work"], "notes": "thought 1 is not right"}
        return {"verdict": "keep"}

    world, controller = drive(tmp_path, config=repair_config(rounds=2), verdict=verdict)
    state, first = controller.state, first_rows(controller)
    chain = sorted((r for r in state.rows.values() if r.line == "001"), key=lambda r: ids.parse_call_id(r.call_id)[2])
    assert [r.status for r in chain] == ["repaired", "repaired", "accepted"]
    assert [r.repair_of for r in chain] == [None, chain[0].row_id, chain[1].row_id]
    assert state.line_status(chain[0].task_id, "001", 3).outcome == "accepted" and state.hand_queue() == []
    assert {r["row_id"] for r in world.shipped_rows()} == {chain[2].row_id, first["002"].row_id}
    assert state.spend("fake-batch", "repair").calls == 2


def test_with_one_round_a_fix_on_a_repair_row_goes_to_the_owner_as_before(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(rounds=1), verdict=sends_back("001", repaired="fix"))
    assert len(world.events_of("repair_requested")) == 1
    assert [r.hand["reason"] for r in controller.state.hand_queue()] == [FIX_TO_HAND_REASON]


def test_rows_waiting_for_a_fix_on_a_first_repair_become_candidates_when_the_rounds_grow(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(), verdict=sends_back("001", repaired="fix"))
    (waiting,) = controller.state.hand_queue()
    assert waiting.repair_of is not None and len(world.events_of("repair_requested")) == 1
    world.config = repair_config(rounds=2)
    later = world.controller()
    world.drive(later, sends_back("001", repaired="keep"))
    assert [e.payload["row_id"] for e in world.events_of("repair_requested")][-1] == waiting.row_id
    assert later.state.rows[waiting.row_id].status == "repaired" and later.state.hand_queue() == []
    assert len(world.shipped_rows()) == 2


def test_attempts_apply_within_each_round(tmp_path):
    # calls 2 and 3 are the attempts of round one, calls 4 and 5 those of round two: the checker refuses the first
    # attempt of each round; the first round's row gets a fix, the second round's row is kept
    def assess(row, context):
        return Assessment(passed=False, errors=(ERROR,)) if repaired_attempt(row) in (2, 4) else Assessment(passed=True)

    def verdict(reviewer, item):
        if item.task["line_id"] == "001" and repaired_attempt(item.row) in (None, 3):
            return {"verdict": "fix", "reasons": ["needs_work"], "notes": "thought 1 is not right"}
        return {"verdict": "keep"}

    world, controller = drive(tmp_path, config=repair_config(attempts=2, rounds=2), assess=assess, verdict=verdict)
    state = controller.state
    chain = sorted((r for r in state.rows.values() if r.line == "001"), key=lambda r: ids.parse_call_id(r.call_id)[2])
    assert [ids.parse_call_id(r.call_id)[2] for r in chain] == [1, 2, 3, 4, 5]
    assert [r.status for r in chain] == ["repaired", "rejected", "repaired", "rejected", "accepted"]
    assert [r.repair_of for r in chain] == [None, chain[0].row_id, chain[0].row_id, chain[2].row_id, chain[2].row_id]
    assert len(world.events_of("repair_requested")) == 2 and state.hand_queue() == []
    assert len(chain[0].repair["calls"]) == 2 and len(chain[2].repair["calls"]) == 2
    (round_two_second,) = repair_calls(world, attempt=5)
    assert round_two_second["feedback"].count(CHECK_LINE_START + ERROR) == 1 and reasons(world) == [FIX_REASON, FIX_REASON]
    assert state.line_status(chain[0].task_id, "001", 3).outcome == "accepted"
    assert state.spend("fake-batch", "repair").calls == 4


# ------------------------------------------------------------------ (f) replay


def test_the_same_events_read_again_give_the_same_state_and_no_call_is_prepared_twice(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(attempts=3, rounds=2), assess=fails_until(4), verdict=sends_back("001", repaired="fix"))
    events = replay(world.state_store).events
    once, twice = State.from_events(events), State.from_events(list(events) + list(events))
    assert once.snapshot() == controller.state.snapshot() == twice.snapshot()
    assert once.repairs_needing_calls(3, 3) == [] and once.repairs_failed(3, 3) == []
    made = Counter(e.payload["call_id"] for e in world.events_of("call_prepared"))
    assert set(made.values()) == {1}
    fresh = world.controller(assess=fails_until(4))
    for _ in range(3):
        assert fresh.step() == 0
    assert Counter(e.payload["call_id"] for e in world.events_of("call_prepared")) == made


def run_with_a_stop(tmp_path, *, crash_at=None, config, assess, verdict, behavior=numbered, lines=LINES):
    world = World(tmp_path, config=config, behavior=behavior)
    controller = world.controller(on_event=crash_after(crash_at) if crash_at else None, assess=assess)
    if crash_at:
        with pytest.raises(SimulatedCrash):
            controller.add_task(task_spec(lines))
            world.drive(controller, verdict)
        controller = world.controller(assess=assess)
    controller.add_task(task_spec(lines))
    world.drive(controller, verdict)
    return world, controller


def test_a_run_stopped_after_any_event_of_a_repair_with_attempts_and_rounds_ends_where_an_uninterrupted_one_does(tmp_path):
    options = dict(config=repair_config(attempts=2, rounds=2), assess=fails_until(3), verdict=sends_back("001", repaired="fix"))
    reference, _ = run_with_a_stop(tmp_path / "ref", **options)
    expected, total = outcome(reference), len(reference.events())
    assert total > 25  # the request, two attempts, the reviews, the second round
    for count in range(1, total):
        world, controller = run_with_a_stop(tmp_path / f"stop-{count}", crash_at=count, **options)
        where = f"a run stopped after event {count}"
        assert outcome(world) == expected, where
        assert set(Counter(e.payload["call_id"] for e in world.events_of("call_prepared")).values()) == {1}, where
        assert set(Counter(e.payload["row_id"] for e in world.events_of("repair_requested")).values()) == {1}, where
        assert set(Counter(world.provider.submitted_calls).values()) == {1}, where
        state = State.from_events(replay(world.state_store).events)
        assert controller.state.snapshot() == state.snapshot() and state.bad_events == [], where


# ------------------------------------------------------------------ (g) the defaults


def kinds(world):
    return [(e.kind, e.payload) for e in world.events() if e.kind != "controller_started"]


@pytest.mark.parametrize("scenario", ["fails_checker", "fix_on_repair", "plain"])
def test_with_one_attempt_and_one_round_the_events_are_the_ones_the_step_always_wrote(tmp_path, scenario):
    options = {
        "fails_checker": dict(assess=fails_until(99), verdict=sends_back("001", "002")),
        "fix_on_repair": dict(verdict=sends_back("001", repaired="fix")),
        "plain": dict(verdict=sends_back("001")),
    }[scenario]
    plain, _ = drive(tmp_path / "plain", config=repair_config(), **options)
    named, _ = drive(tmp_path / "named", config=repair_config(attempts=1, rounds=1), **options)
    assert kinds(plain) == kinds(named)
    wanted = {"fails_checker": 2, "fix_on_repair": 1, "plain": 1}[scenario]
    assert len(plain.events_of("repair_requested")) == wanted and len(repair_calls(plain)) == wanted
    assert Counter(e.kind for e in plain.events())["row_to_hand"] == {"fails_checker": 4, "fix_on_repair": 2, "plain": 1}[scenario]


# ------------------------------------------------------------------ two rows of one line in repair at once


def two_rows(payload):
    """A writer whose first call writes two rows for the line (a and b); a repair writes one row naming its attempt."""
    if payload.get("repair_of"):
        return numbered(payload)
    rows = [
        {"id": f"{payload['line']}-a", "thoughts": [f"thought a about {payload['spec']}"]},
        {"id": f"{payload['line']}-b", "thoughts": [f"thought b about {payload['spec']}"]},
    ]
    return FakeOutcome(output=json.dumps(rows))


def fix_both(reviewer, item):
    if item.row["thoughts"][0].startswith("repaired"):
        return {"verdict": "keep"}
    return {"verdict": "fix", "reasons": ["needs_work"], "notes": f"notes for {item.row['id']}"}


def check_each_call_is_its_own_rows(world, state, per_row):
    """Every call id is prepared once, and each repair call is built from, and recorded under, its own row."""
    prepared = [e.payload for e in world.events_of("call_prepared")]
    assert len({p["call_id"] for p in prepared}) == len(prepared)
    originals = [r for r in state.rows.values() if r.repair_of is None]
    assert len(originals) == 2
    for row in originals:
        mine = [p for p in prepared if p.get("repair_of") == row.row_id]
        assert len(mine) == per_row and row.repair["calls"] == [p["call_id"] for p in mine]
        marker = "thought a" if row.record["thoughts"][0].startswith("thought a") else "thought b"
        for call in mine:
            assert marker in call["feedback"][-1] and f"notes for {row.record['id']}" in call["feedback"][0]
    attempts = [p["attempt"] for p in prepared if p.get("repair_of")]
    assert len(set(attempts)) == len(attempts)
    return originals


def test_two_rows_of_one_line_that_both_need_a_later_attempt_get_calls_of_their_own(tmp_path):
    world = World(tmp_path, config=repair_config(attempts=3), behavior=two_rows)
    controller = world.controller(assess=fails_until(99))
    controller.add_task(task_spec(("001",)))
    world.drive(controller, fix_both)
    originals = check_each_call_is_its_own_rows(world, controller.state, 3)
    assert {r.status for r in originals} == {"hand"} and reasons(world).count(FAILED_CHECKER_REASON) == 2
    assert controller.state.spend("fake-batch", "repair").calls == 6


def test_two_rows_of_one_line_whose_repair_calls_fail_get_calls_of_their_own(tmp_path):
    def behavior(payload):
        return FakeOutcome(error="declined", retryable=False, kind="refused") if payload.get("repair_of") else two_rows(payload)

    world = World(tmp_path, config=repair_config(attempts=3), behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller, fix_both)
    prepared = [e.payload for e in world.events_of("call_prepared")]
    assert len({p["call_id"] for p in prepared}) == len(prepared) == 7
    for row in [r for r in controller.state.rows.values() if r.repair_of is None]:
        assert len(row.repair["calls"]) == 3 and row.status == "hand"


def test_two_transport_retries_on_one_line_get_calls_of_their_own(tmp_path):
    def behavior(payload):
        return FakeOutcome(error="overloaded", retryable=True) if payload.get("repair_of") else two_rows(payload)

    world = World(tmp_path, config=repair_config(max_attempts=3), behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller, fix_both)
    prepared = [e.payload for e in world.events_of("call_prepared")]
    assert len({p["call_id"] for p in prepared}) == len(prepared) == 7  # the line's call, then three for each row
    by_id = {p["call_id"]: p for p in prepared}
    for p in prepared:
        if p.get("retry_of"):
            assert by_id[p["retry_of"]]["repair_of"] == p["repair_of"]


def test_two_rows_of_one_line_in_repair_at_once_end_the_same_after_a_stop_at_any_event(tmp_path):
    options = dict(config=repair_config(attempts=2), assess=fails_until(99), verdict=fix_both, behavior=two_rows, lines=("001",))
    reference, _ = run_with_a_stop(tmp_path / "ref", **options)
    expected, total = outcome(reference), len(reference.events())
    for count in range(1, total):
        world, controller = run_with_a_stop(tmp_path / f"stop-{count}", crash_at=count, **options)
        where = f"a run stopped after event {count}"
        assert outcome(world) == expected, where
        made = Counter(e.payload["call_id"] for e in world.events_of("call_prepared"))
        assert set(made.values()) == {1}, where
        check_each_call_is_its_own_rows(world, controller.state, 2)
        assert set(Counter(world.provider.submitted_calls).values()) == {1}, where


# ------------------------------------------------------------------ what no further attempt can change


def test_a_repair_the_checker_set_aside_goes_to_the_owner_at_once_and_is_not_tried_again(tmp_path):
    def assess(row, context):
        if repaired_attempt(row) is not None:
            return Assessment(passed=True, set_aside={"class": "person", "reason": "a person in the seed"})
        return Assessment(passed=True)

    world, controller = drive(tmp_path, config=repair_config(attempts=3), assess=assess)
    row = first_rows(controller)["001"]
    assert len(repair_calls(world)) == 1 and row.status == "hand" and row.hand["reason"] == FAILED_CHECKER_REASON
    world.config = repair_config(attempts=5)
    later = world.controller(assess=assess)
    for _ in range(3):
        assert later.step() == 0
    assert len(world.events_of("repair_requested")) == 1 and len(repair_calls(world)) == 1


def test_a_repair_call_that_could_not_be_built_is_not_tried_again(tmp_path):
    from rps.images import ImageError

    world = World(tmp_path, config=repair_config(attempts=3), behavior=numbered)
    loads = {"count": 0}

    def loader(source, line):
        loads["count"] += 1
        if loads["count"] > 2:
            raise ImageError("the picture is gone")
        return b"picture", "jpg", 64

    controller = world.controller(image_loader=loader)
    controller.add_task(task_spec(("001", "002"), image="p.jpg"))
    world.drive(controller, sends_back("001"))
    row = first_rows(controller)["001"]
    assert len(repair_calls(world)) == 1 and row.status == "hand"
    assert row.hand["reason"] == "the repair call failed (bad_request): the row waits for the owner"
    world.config = repair_config(attempts=5)
    later = world.controller(image_loader=loader)
    for _ in range(3):
        assert later.step() == 0
    assert len(world.events_of("repair_requested")) == 1


def test_a_checker_rejection_that_asks_for_a_rewrite_still_earns_another_attempt(tmp_path):
    world, controller = drive(tmp_path, config=repair_config(attempts=2), assess=fails_until(3))
    assert first_rows(controller)["001"].status == "repaired" and len(repair_calls(world)) == 2


# ------------------------------------------------------------------ the rounds lowered again


def test_a_row_waiting_after_a_failed_repair_is_not_a_candidate_when_its_round_is_not_below_the_rounds():
    reason = FAILED_CHECKER_REASON
    first = hand_row("r1")
    again = hand_row("r2", reason=reason, repair_record=failed_record(reason), repair_of="r1")
    rows = {"r1": first, "r2": again}
    calls = {"c2": Call()}
    assert repair.is_candidate(again, rows=rows, calls=calls, attempts=2, rounds=2)
    assert not repair.is_candidate(again, rows=rows, calls=calls, attempts=2, rounds=1)  # rounds lowered to 1: round 1 is the last
    plain = hand_row("r3", reason=reason, repair_record=failed_record(reason))
    assert repair.is_candidate(plain, rows={"r3": plain}, calls=calls, attempts=2, rounds=1)
