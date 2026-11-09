"""The repair step stops at any moment and resumes: the state is rebuilt from the events alone, and a run killed after
any event ends where an uninterrupted one does, with no row sent back twice and no repair call made twice."""

from collections import Counter

import pytest

from rps.assess import Assessment
from rps.events import replay
from rps.providers import SimulatedCrash
from rps.state import State

from harness import World, task_spec
from repair_helpers import gated_writer, repair_config, sends_back, writes_and_rewrites
from review_helpers import crash_after, crash_on, outcome

LINES = ("001", "002", "003", "004")
CHECKER_REASON = "the repair failed the checker: the row waits for the owner"


def fails_repaired_rows(row, context):
    if row["thoughts"][0].startswith("repaired"):
        return Assessment(passed=False, errors=("thought 1 is too long",))
    return Assessment(passed=True)


def turned_on_after_the_rows_are_in_the_queue(tmp_path, *, crash_at=None, min_rows=1, assess=None, verdict=None):
    """The rows of a task are reviewed with the repair step off, so that two rows wait in the hand queue for a
    fix; then the step is switched on and a run repairs them (killed after its `crash_at`-th event, if given,
    and followed by a run that goes on). Returns the world, the last controller and the events the second stage wrote."""
    verdict = verdict or sends_back("001", "003")
    world = World(tmp_path, config=repair_config(enabled=False), behavior=writes_and_rewrites)
    first = world.controller(assess=assess)
    first.add_task(task_spec(LINES))
    world.drive(first, verdict)
    assert len(first.state.hand_queue()) == 2
    world.config = repair_config(min_rows=min_rows)
    before = len(world.events())
    if crash_at is None:
        controller = world.controller(assess=assess)
        world.drive(controller, verdict)
        return world, controller, len(world.events()) - before
    crashing = world.controller(on_event=crash_after(crash_at), assess=assess)
    with pytest.raises(SimulatedCrash):
        world.drive(crashing, verdict)
    controller = world.controller(assess=assess)
    world.drive(controller, verdict)
    return world, controller, None


def check_a_run_killed_after_every_event(tmp_path, **options):
    reference, _controller, total = turned_on_after_the_rows_are_in_the_queue(tmp_path / "reference", **options)
    expected = outcome(reference)
    assert total > 12  # the events of the repair itself: the requests, the calls, the new rows, their reviews
    for count in range(1, total):
        world, controller, _ = turned_on_after_the_rows_are_in_the_queue(tmp_path / f"kill-{count}", crash_at=count, **options)
        where = f"a run killed after event {count}"
        assert outcome(world) == expected, f"{where} ended differently"
        sent = Counter(e.payload["row_id"] for e in world.events_of("repair_requested"))
        assert len(sent) == 2 and set(sent.values()) == {1}, where  # each row was sent back once
        made = Counter(e.payload["call_id"] for e in world.events_of("call_prepared") if e.payload["purpose"] == "repair")
        assert len(made) == 2 and set(made.values()) == {1}, where  # and one call was made for each
        assert set(Counter(world.provider.submitted_calls).values()) == {1}, where  # no call went to the provider twice
        state = State.from_events(replay(world.state_store).events)
        assert controller.state.snapshot() == state.snapshot(), where
        assert state.bad_events == [] and state.unknown_kinds == {}, where
        assert sorted(row.status for row in state.rows.values()) == sorted(
            row.status for row in State.from_events(reference.events()).rows.values()
        ), where
    return expected


def test_a_run_killed_after_any_event_of_a_repair_ends_where_an_uninterrupted_one_does(tmp_path):
    expected = check_a_run_killed_after_every_event(tmp_path)
    rows, shipped = expected
    assert sorted(status for status, _verdicts in rows.values()) == ["accepted"] * 4 + ["repaired"] * 2
    assert len(shipped) == 4


def test_a_run_killed_after_any_event_of_a_batch_that_waits_for_min_rows_ends_where_an_uninterrupted_one_does(tmp_path):
    # two rows are enough for a batch: a run that dies between the two events of the batch leaves one row sent back
    # and one still waiting, and the next run sends the second with the next batch or when the route is idle
    expected = check_a_run_killed_after_every_event(tmp_path, min_rows=2)
    assert len(expected[1]) == 4


def test_a_run_killed_after_any_event_of_a_repair_that_fails_the_checker_ends_where_an_uninterrupted_one_does(tmp_path):
    expected = check_a_run_killed_after_every_event(tmp_path, assess=fails_repaired_rows)
    rows, shipped = expected
    assert sorted(status for status, _verdicts in rows.values()) == ["accepted", "accepted", "hand", "hand", "rejected", "rejected"]
    assert len(shipped) == 2  # the two rows that were kept; the two sent back wait for the owner


def test_a_row_is_sent_back_again_to_the_owner_the_same_way_whenever_the_run_is_killed(tmp_path):
    verdict = sends_back("001", "003", repaired="fix")
    expected = check_a_run_killed_after_every_event(tmp_path, verdict=verdict)
    rows, shipped = expected
    assert sorted(status for status, _verdicts in rows.values()) == ["accepted", "accepted", "hand", "hand", "repaired", "repaired"]
    assert len(shipped) == 2


# ------------------------------------------------------------------ one stop, many restarts


def test_a_run_that_stops_while_a_repair_call_is_in_flight_polls_it_and_never_sends_it_again(tmp_path):
    world = World(tmp_path, config=repair_config(enabled=False), behavior=writes_and_rewrites)
    first = world.controller()
    first.add_task(task_spec(("001", "002")))
    world.drive(first, sends_back("001"))
    assert len(first.state.hand_queue()) == 1
    world.config = repair_config()
    world.polls_until_ended = 1000  # the provider does not finish the repair batch
    second = world.controller()
    for _ in range(4):
        second.step()
    (call,) = [c for c in second.state.calls.values() if c.repair_of]
    assert call.status == "submitted" and world.provider.submit_count == 2  # one batch for the lines, one for the repair
    world.polls_until_ended = 0  # a later run finds the batch finished
    third = world.controller()
    world.drive(third, sends_back("001"))
    assert world.provider.submit_count == 2 and third.state.counts()["rows"] == {"repaired": 1, "accepted": 2}
    assert Counter(world.provider.submitted_calls).most_common(1)[0][1] == 1


def test_a_repair_that_is_asked_for_in_one_run_and_sent_by_the_next_is_the_same_repair(tmp_path):
    world = World(tmp_path, config=repair_config(enabled=False), behavior=writes_and_rewrites)
    first = world.controller()
    first.add_task(task_spec(LINES))
    world.drive(first, sends_back("001", "003"))
    world.config = repair_config()
    crashing = world.controller(on_event=crash_on("repair_requested", nth=2))
    with pytest.raises(SimulatedCrash):
        crashing.step()
    asked = {e.payload["row_id"]: e.payload["call_id"] for e in world.events_of("repair_requested")}
    assert len(asked) == 2 and not [e for e in world.events_of("call_prepared") if e.payload["purpose"] == "repair"]
    second = world.controller()
    world.drive(second, sends_back("001", "003"))
    prepared = {e.payload["repair_of"]: e.payload["call_id"] for e in world.events_of("call_prepared") if e.payload["purpose"] == "repair"}
    assert prepared == asked  # the calls are the ones the events named
    assert second.state.counts()["rows"] == {"repaired": 2, "accepted": 4}


def test_the_state_of_the_repair_step_is_the_same_when_rebuilt_from_the_events_at_every_stage(tmp_path):
    world = World(tmp_path, config=repair_config(enabled=False), behavior=writes_and_rewrites, polls_until_ended=0)
    first = world.controller()
    first.add_task(task_spec(LINES))
    world.drive(first, sends_back("001", "003"))
    world.config = repair_config()
    controller = world.controller()
    verdict = sends_back("001", "003")
    snapshots = []
    for _ in range(12):
        controller.step()
        world.run_reviewers(verdict)
        rebuilt = State.from_events(replay(world.state_store).events)
        assert controller.state.snapshot() == rebuilt.snapshot()
        snapshots.append(sorted(row.status for row in rebuilt.rows.values()))
        if controller.is_idle() and not controller.state.open_packets():
            break
    assert snapshots[-1] == ["accepted"] * 4 + ["repaired"] * 2
    assert any("repairing" in statuses for statuses in snapshots)  # the stage in between was looked at too


def test_a_stuck_route_holds_the_repair_back_and_the_next_run_goes_on_with_it(tmp_path):
    held = {"B01"}
    world = World(tmp_path, config=repair_config(min_rows=3), behavior=writes_and_rewrites, writer_class=gated_writer(held), polls_until_ended=0)
    first = world.controller()
    first.add_task(task_spec(LINES))
    for _ in range(10):
        first.step()
        if first.state.open_packets():
            break
    first.add_task(task_spec(("B01",), batch="2"))
    first.step()
    world.run_reviewers(sends_back("001", "003"))
    first.step()
    assert len(first.state.hand_queue()) == 2 and world.events_of("repair_requested") == []
    # a new run starts with the route still busy: the two rows keep waiting for the owner or for the batch
    second = world.controller()
    for _ in range(3):
        second.step()
    assert world.events_of("repair_requested") == [] and len(second.state.hand_queue()) == 2
    held.clear()
    world.drive(second, sends_back("001", "003"))
    assert len(world.events_of("repair_requested")) == 2 and len(world.shipped_rows()) == 5
