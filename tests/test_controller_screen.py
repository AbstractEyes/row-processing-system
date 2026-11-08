"""A spec line the rule set's screen names a problem with is set aside before its first call; no call is made for it."""

from __future__ import annotations

import json
import threading

from rps import events
from rps.assess import Assessment
from rps.assessor import screen_item
from rps.events import replay
from rps.providers import FakeOutcome, FakeWriter
from rps.state import TERMINAL_LINE_OUTCOMES, State
from rps.tasks import SpecLine, TaskSpec

from harness import World, keep_all, task_spec
from helpers import Builder
from test_controller_api import ROUTE, lines, pool_world, run_until, settled
from test_controller_budgets import bench
from test_controller_rules import conform, example, pool_config, pool_task, sent_payloads

REASON = "the record names a thing the hint never mentions"


def screen_002(spec):
    return REASON if spec == "spec for 002" else None


def recording_writer():
    """A writer class that notes the line of every call it is asked to build, and that list."""
    built: list[str] = []

    class Recording(FakeWriter):
        def prepare(self, calls):
            built.extend(call.line for call in calls)
            return super().prepare(calls)

    return Recording, built


def garbled_item():
    """The pool's seeded example with its record misspelled: the thing is a 'windom' and the hint says 'window'."""
    item = json.loads(json.dumps(example("seeded_single")["item"]))
    item["ids"] = ["art-garbled-1"]
    item["spec"]["thing"]["name"] = "windom"
    return item


# ------------------------------------------------------------------ the screen in a run


def test_a_screened_line_gets_no_call_and_is_recorded_as_its_own_kind_of_event(tmp_path):
    writer_class, built = recording_writer()
    world = World(tmp_path, writer_class=writer_class)
    controller = world.controller(screen=screen_002)
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller, keep_all)

    # nothing was built, prepared, submitted or sent for the screened line
    assert sorted(built) == ["001", "003"]
    assert sorted(event.payload["line"] for event in world.events_of("call_prepared")) == ["001", "003"]
    assert sorted(payload["spec"] for payload in sent_payloads(world)) == ["spec for 001", "spec for 003"]
    assert len(world.provider.submitted_calls) == 2
    assert len(controller.state.calls) == 2
    # one event, of its own kind, with the class other and the reason, and no row ever existed for the line
    (event,) = world.events_of("line_screened")
    assert event.payload["line"] == "002" and event.payload["item_id"] == "img-002"
    assert event.payload["class"] == "other" and event.payload["reason"] == REASON
    assert event.payload["task_id"] == next(iter(controller.state.tasks))
    assert world.events_of("row_set_aside") == []
    assert {row.line for row in controller.state.rows.values()} == {"001", "003"}
    # the line counts as done, and as a screened line, not a set-aside one
    progress = controller.state.job_progress(2)
    assert (progress["lines_total"], progress["lines_done"]) == (3, 3)
    assert progress["lines_screened"] == 1 and progress["lines_accepted"] == 2 and "lines_set_aside" not in progress
    assert controller.state.counts()["screened_lines"] == 1
    assert len(world.shipped_rows()) == 2


def test_a_line_the_screen_lets_through_is_written_as_before(tmp_path):
    world = World(tmp_path)
    controller = world.controller(screen=lambda spec: None)
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller, keep_all)
    assert world.events_of("line_screened") == []
    assert len(world.shipped_rows()) == 2 and controller.state.job_progress(2)["lines_accepted"] == 2


def test_a_task_whose_lines_are_all_screened_ends_the_run_with_no_call_at_all(tmp_path):
    world = World(tmp_path)
    controller = world.controller(screen=lambda spec: "every one of them is garbled")
    controller.add_task(task_spec(("001", "002", "003")))
    summary = controller.run()
    assert summary.reason == "idle"
    assert summary.progress["lines_screened"] == 3 and summary.progress["lines_done"] == 3
    assert world.provider.submit_count == 0 and world.events_of("call_prepared") == []
    assert len(world.events_of("line_screened")) == 3
    assert "lines the screen set aside before any call: 3" in controller.format_status()


def test_a_screened_line_does_not_use_up_a_free_worker_of_a_one_call_route(tmp_path):
    gate_open = threading.Event()
    world = pool_world(tmp_path, workers=2, gate=lambda: None if gate_open.is_set() else 0.002)
    controller = world.controller(screen=lambda spec: REASON if spec in ("spec for 001", "spec for 002") else None)
    controller.add_task(task_spec(lines(6), route=ROUTE))
    controller.step()
    # the two screened lines head the list, yet both workers were still given a call in the same pass
    assert sorted(event.payload["line"] for event in world.events_of("call_prepared")) == ["003", "004"]
    assert sorted(event.payload["line"] for event in world.events_of("line_screened")) == ["001", "002"]
    gate_open.set()
    run_until(world, controller, lambda: settled(controller))
    assert len(world.shipped_rows()) == 4 and controller.state.job_progress(2)["lines_screened"] == 2


# ------------------------------------------------------------------ once, and never on a retry


def test_the_screen_reads_a_line_once_and_never_a_retry(tmp_path):
    asked = []

    def screen(spec):
        asked.append(spec)
        return None if len(asked) == 1 else "a second look says no"

    def assess(row, context):
        if context.attempt == 1:
            return Assessment(passed=False, errors=("a thought defines a word instead of looking",))
        return Assessment(passed=True)

    world = World(tmp_path)
    controller = world.controller(screen=screen, assess=assess)
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    assert [event.payload["attempt"] for event in world.events_of("call_prepared")] == [1, 2]  # the retry was made
    assert asked == ["spec for 001"]  # and the screen was not asked about it
    assert world.events_of("line_screened") == []
    assert len(world.shipped_rows()) == 1


def test_a_retry_after_a_restart_is_not_screened_either(tmp_path):
    def assess(row, context):
        if context.attempt == 1:
            return Assessment(passed=False, errors=("a thought defines a word instead of looking",))
        return Assessment(passed=True)

    world = World(tmp_path)
    first = world.controller(screen=lambda spec: None, assess=assess)
    first.add_task(task_spec(("001",)))
    (task_id,) = first.state.tasks
    for _ in range(20):
        first.step()
        if first.state.line_status(task_id, "001", 2).outcome == "retry":
            break
    assert first.state.line_status(task_id, "001", 2).outcome == "retry"
    # the new run remembers nothing of what was screened, and its screen would set every line aside
    asked = []
    second = world.controller(screen=lambda spec: asked.append(spec) or "set aside", assess=assess)
    world.drive(second, keep_all)
    assert asked == []
    assert [event.payload["attempt"] for event in world.events_of("call_prepared")] == [1, 2]
    assert world.events_of("line_screened") == [] and len(world.shipped_rows()) == 1


def test_a_line_that_waits_for_room_under_a_cap_is_not_read_again_when_it_is_taken_up_again(tmp_path):
    asked = []

    def screen(spec):
        asked.append(spec)
        return None

    # two calls fill the cap while they are in flight; the other three wait for them, then are refused for good
    world = bench(tmp_path, production=0.004)
    controller = world.controller(screen=screen)
    controller.add_task(task_spec(("001", "002", "003", "004", "005")))
    world.drive(controller)
    assert len(world.events_of("call_prepared")) == 2 and len(world.events_of("budget_refused")) == 3
    assert sorted(asked) == [f"spec for 00{number}" for number in range(1, 6)]  # five lines, five readings


# ------------------------------------------------------------------ a screened line stays aside


def test_a_screened_line_stays_aside_after_a_restart_with_no_second_event(tmp_path):
    world = World(tmp_path)
    first = world.controller(screen=screen_002)
    first.add_task(task_spec(("001", "002")))
    world.drive(first, keep_all)
    (task_id,) = first.state.tasks
    assert first.state.line_status(task_id, "002", 2).outcome == "screened"

    asked = []
    second = world.controller(screen=lambda spec: asked.append(spec) or REASON)
    second.start()
    assert second.state.line_status(task_id, "002", 2).outcome == "screened"
    assert second.state.screened_lines() == {(task_id, "002"): {"route": "fake-batch", "class": "other", "reason": REASON}}
    assert second.state.is_screened(task_id, "002") and not second.state.is_screened(task_id, "001")
    second.step()
    assert asked == []  # the accepted line and the screened line are both done: nothing is read again
    assert len(world.events_of("line_screened")) == 1
    assert second.is_idle()
    assert second.state.snapshot() == State.from_events(replay(world.state_store).events).snapshot()


# ------------------------------------------------------------------ counted apart


def test_screened_lines_are_counted_apart_from_a_writers_skip_and_the_assessors_set_aside(tmp_path):
    skip = {"skip": True, "class": "person", "reason": "a person appears in the picture", "ids": ["x"]}

    def behavior(payload):
        if payload["spec"] == "spec for 002":
            return FakeOutcome(output=json.dumps(skip))
        return FakeOutcome(output=json.dumps({"id": f"{payload['line']}-1", "thoughts": ["a thought"]}))

    def assess(row, context):
        if context.line == "003":
            return Assessment(passed=True, set_aside={"class": "contradiction", "reason": "the hint contradicts the record"})
        return Assessment(passed=True)

    def screen(spec):
        return REASON if spec == "spec for 001" else None

    world = World(tmp_path, behavior=behavior)
    controller = world.controller(screen=screen, assess=assess)
    controller.add_task(task_spec(("001", "002", "003", "004")))
    world.drive(controller, keep_all)

    kinds = [event.kind for event in world.events() if event.kind in ("line_screened", "row_set_aside")]
    assert sorted(kinds) == ["line_screened", "row_set_aside", "row_set_aside"]
    (screened,) = world.events_of("line_screened")
    skipped, set_aside = sorted(world.events_of("row_set_aside"), key=lambda event: "row_id" in event.payload)
    assert screened.payload["line"] == "001" and screened.payload["class"] == "other"
    assert "row_id" not in skipped.payload and skipped.payload["class"] == "person"  # the writer's own skip
    assert set_aside.payload["row_id"] and set_aside.payload["class"] == "contradiction"  # the assessor's set-aside of a row
    progress = controller.state.job_progress(2)
    assert (progress["lines_screened"], progress["lines_set_aside"], progress["lines_accepted"]) == (1, 2, 1)
    assert progress["lines_done"] == progress["lines_total"] == 4


def test_the_screened_outcome_is_a_finished_outcome_and_the_event_is_applied_once():
    assert "screened" in TERMINAL_LINE_OUTCOMES
    b = Builder()
    task = b.task(lines={"001": "a", "002": "b"})
    b.add("line_screened", {"task_id": task, "line": "001", "item_id": "img-001", "route": "fake-batch", "class": "other", "reason": "garbled"})
    b.add("line_screened", {"task_id": task, "line": "001", "item_id": "img-001", "route": "fake-batch", "class": "other", "reason": "again"})
    state = State.from_events(b.events)
    assert state.line_status(task, "001", 2).outcome == "screened"
    assert state.line_status(task, "002", 2).outcome == "new"
    assert state.screened_lines()[(task, "001")]["reason"] == "garbled"  # the first record stands
    assert [line.line_id for _task, line, _status in state.lines_needing_calls(2)] == ["002"]
    progress = state.job_progress(2)
    assert (progress["lines_total"], progress["lines_done"], progress["lines_screened"], progress["lines_new"]) == (2, 1, 1, 1)
    again = State.from_events(b.events + b.events[1:])
    assert json.dumps(again.snapshot(), sort_keys=True) == json.dumps(state.snapshot(), sort_keys=True)


def test_an_event_for_a_task_or_line_the_state_does_not_hold_is_ignored():
    b = Builder()
    task = b.task(lines={"001": "a"})
    b.add("line_screened", {"task_id": "no-such-task", "line": "001", "class": "other", "reason": "x"})
    b.add("line_screened", {"task_id": task, "line": "no-such-line", "class": "other", "reason": "x"})
    state = State.from_events(b.events)
    assert state.screened_lines() == {} and state.bad_events == [] and state.unknown_kinds == {}
    assert state.line_status(task, "001", 2).outcome == "new"


def test_a_line_that_already_has_a_call_is_not_taken_for_a_screened_one():
    b = Builder()
    task = b.task(lines={"001": "a"})
    call = b.prepare(task, "001")
    b.add("line_screened", {"task_id": task, "line": "001", "item_id": "img-001", "route": "fake-batch", "class": "other", "reason": "late"})
    state = State.from_events(b.events)
    assert state.screened_lines() == {} and not state.is_screened(task, "001")
    assert state.line_status(task, "001", 2).outcome == "in_flight" and call in state.calls


def test_the_new_event_kind_is_known_and_documented():
    assert "line_screened" in events.EVENT_KINDS
    assert "line_screened" in (events.__doc__ or "")


# ------------------------------------------------------------------ a screen that fails


def test_a_screen_that_cannot_read_a_line_lets_the_line_through_and_is_noted_once(tmp_path):
    def broken(spec):
        raise AttributeError("'str' object has no attribute 'get'")

    world = World(tmp_path)
    controller = world.controller(screen=broken)
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller, keep_all)
    assert world.events_of("line_screened") == []
    assert len(world.shipped_rows()) == 2
    notes = [problem for problem in controller.problems if "the screen could not read a spec line" in problem]
    assert len(notes) == 1 and "AttributeError" in notes[0]
    assert sum("the screen could not read a spec line" in line for line in world.printed) == 1


# ------------------------------------------------------------------ the rule pool's screen


def pool_run(tmp_path, config):
    clean = example("seeded_single")
    garbled = garbled_item()
    assert screen_item(garbled) and screen_item(clean["item"]) is None

    def behavior(payload):
        assert payload["spec"]["ids"] == clean["item"]["ids"], "a call was made for the garbled seed"
        return conform(payload, clean["output"])

    world = World(tmp_path, config=config, behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([clean["item"], garbled]))
    return world, controller, clean, garbled


def test_the_rule_pool_sets_a_garbled_seed_aside_before_any_call(tmp_path):
    world, controller, clean, garbled = pool_run(tmp_path, pool_config())
    assert controller.screen is screen_item  # the pool brings its own screen
    world.drive(controller, keep_all)
    assert [payload["spec"]["ids"] for payload in sent_payloads(world)] == [clean["item"]["ids"]]
    (event,) = world.events_of("line_screened")
    assert event.payload["line"] == garbled["ids"][0] and event.payload["class"] == "other"
    assert "'windom'" in event.payload["reason"] and "'window'" in event.payload["reason"]
    assert event.payload["reason"] == screen_item(garbled)
    assert len(world.shipped_rows()) == len(clean["output"]["rows"])
    progress = controller.state.job_progress(2)
    assert (progress["lines_screened"], progress["lines_accepted"], progress["lines_done"]) == (1, 1, 2)


def test_the_pools_screen_can_be_turned_off_in_the_configuration(tmp_path):
    clean = example("seeded_single")
    world = World(tmp_path, config=pool_config(screen="none"), behavior=lambda payload: FakeOutcome(output="not json"))
    controller = world.controller()
    assert controller.screen is None
    controller.add_task(pool_task([clean["item"], garbled_item()]))
    controller.step()
    assert world.events_of("line_screened") == []
    assert sorted(event.payload["line"] for event in world.events_of("call_prepared")) == sorted(
        [garbled_item()["ids"][0], clean["item"]["ids"][0]]
    )


def test_a_screen_of_the_callers_wins_over_the_rules(tmp_path):
    clean = example("seeded_single")
    world = World(tmp_path, config=pool_config(), behavior=lambda payload: FakeOutcome(output="not json"))
    controller = world.controller(screen=lambda spec: None)
    controller.add_task(pool_task([clean["item"], garbled_item()]))
    controller.step()
    assert world.events_of("line_screened") == [] and len(world.events_of("call_prepared")) == 2


def test_with_no_rules_nothing_is_screened(tmp_path):
    world = World(tmp_path)
    controller = world.controller()
    assert controller.screen is None
    controller.add_task(
        TaskSpec(
            source="photos", batch="1", route="fake-batch", purpose="production", variant="",
            lines=(SpecLine("001", garbled_item(), item_id="art-garbled-1"),),
        )
    )
    controller.step()
    assert world.events_of("line_screened") == [] and len(world.events_of("call_prepared")) == 1
