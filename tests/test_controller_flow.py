import json

import pytest

from rps import layout
from rps.assess import Assessment
from rps.config import ConfigError
from rps.controller import ControllerError, TaskConflict, format_progress, parse_output
from rps.ledger import PriceTable
from rps.providers import FakeOutcome, FakeWriter
from rps.providers.base import PollStatus
from rps.provenance import require_label
from rps.state import State

from fakes import Clock
from harness import World, keep_all, task_spec
from helpers import make_config


def test_parse_output_splits_rows_and_skip_objects():
    rows, skips, problem = parse_output('{"a": 1}')
    assert rows == [{"a": 1}] and skips == [] and problem is None
    rows, skips, problem = parse_output('[{"a": 1}, {"skip": true, "class": "person", "reason": "x"}, {"b": 2}]')
    assert rows == [{"a": 1}, {"b": 2}] and len(skips) == 1 and problem is None
    rows, skips, problem = parse_output('```json\n{"a": 1}\n```')
    assert rows == [{"a": 1}] and problem is None
    for bad in (None, "", "   ", "not json", "[1, 2]", '"text"', "[]", "[{\"skip\": true}]x"):
        rows, skips, problem = parse_output(bad)
        assert problem and not rows
    rows, skips, problem = parse_output('{"skip": true, "class": "person", "reason": "a person"}')
    assert rows == [] and len(skips) == 1 and problem is None


def test_format_progress_names_job_size_done_time_spent_and_time_left():
    line = format_progress(lines_total=120, lines_done=48, rows=91, elapsed=730, budget=7200, spent_usd=1.2345, waiting={"calls in flight": 3, "packets": 0})
    assert "job 120 lines" in line
    assert "done 48 (40%)" in line
    assert "time spent 00:12:10" in line
    assert "time left 01:47:50" in line
    assert "est." in line and "$1.2345 spent" in line and "waiting: 3 calls in flight" in line
    assert "packets" not in line
    assert "time left no limit" in format_progress(lines_total=1, lines_done=0, rows=0, elapsed=5, budget=None)


def test_a_task_runs_from_spec_lines_to_shipped_rows(tmp_path):
    world = World(tmp_path)
    controller = world.controller()
    controller.add_task(task_spec())
    world.drive(controller)
    state = controller.state
    assert state.job_progress(2)["lines_accepted"] == 6
    assert state.counts()["rows"] == {"accepted": 6}
    rows = world.shipped_rows()
    assert len(rows) == 6
    for row in rows:
        require_label(row)
        assert row["writer"]["provider"] == "fake" and row["writer"]["route"] == "batch"
        assert row["source_label"]["dataset"] == "photos" and row["source_label"]["license"] == "CC BY-NC"
        assert row["thoughts"][0].startswith("a thought about spec for")
    assert world.ship_store.list("ledger/") == ["ledger/summary.json", "ledger/tasks.jsonl"]
    summary = json.loads(world.ship_store.read_text("ledger/summary.json"))
    assert summary["routes"]["fake-batch"]["purposes"]["production"]["calls"] == 6
    assert summary["price_table_date"] == controller.prices.date
    # events and packets stay in the state store; accepted rows and the ledger stay in the ship store
    assert not world.state_store.list("accepted/") and not world.state_store.list("ledger/")
    assert not world.ship_store.list("events/") and not world.ship_store.list("packets/")


def test_every_stored_row_carries_the_writer_label(tmp_path):
    world = World(tmp_path)
    controller = world.controller()
    controller.add_task(task_spec())
    world.drive(controller)
    seen = 0
    for event in world.events_of("call_returned"):
        for row in event.payload["rows"]:
            require_label(row)
            seen += 1
    for row in world.shipped_rows():
        require_label(row)
        seen += 1
    assert seen == 12


def test_a_run_ends_when_only_the_reviewers_are_left_and_the_next_run_picks_up_their_verdicts(tmp_path):
    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec())
    summary = first.run()
    assert summary.reason == "idle"
    assert first.state.counts()["packets_open"] == 3  # two rows each for three reviewers
    assert world.shipped_rows() == []
    assert any("job 6 lines" in line for line in world.printed)

    world.run_reviewers()
    second = world.controller()
    summary = second.run()
    assert summary.reason == "idle"
    assert len(world.shipped_rows()) == 6
    assert second.state.job_progress(2)["lines_done"] == 6


def test_a_run_can_wait_for_review_until_its_time_runs_out(tmp_path):
    clock = Clock()
    config = make_config(poll_seconds=30, wait_for_review=True)
    world = World(tmp_path, config=config)
    controller = world.controller(clock=clock, sleep=clock.sleep)
    controller.add_task(task_spec(("001", "002", "003")))
    summary = controller.run(time_budget_seconds=100)
    assert summary.reason == "time_budget"
    assert controller.state.counts()["packets_open"] == 3  # one row each for three reviewers
    assert clock.sleeps and all(pause <= 30 for pause in clock.sleeps)
    assert sum(clock.sleeps) <= 100


def test_progress_lines_are_printed_without_any_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "a" * 30)
    world = World(tmp_path)
    controller = world.controller()
    controller.add_task(task_spec())
    controller.run()
    text = "\n".join(world.printed)
    assert "time spent" in text and "time left" in text and "done" in text
    assert "sk-ant" not in text


def test_a_stop_file_ends_the_run_cleanly_and_the_next_run_goes_on(tmp_path):
    config = make_config(work_dir=str(tmp_path / "work"))
    world = World(tmp_path, config=config)
    controller = world.controller()
    controller.add_task(task_spec())
    stop = config.stop_file_path()
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("stop", encoding="utf-8")
    summary = controller.run()
    assert summary.reason == "stop_file"
    assert summary.steps == 0
    assert world.events_of("controller_stopped")[-1].payload["reason"] == "stop_file"
    stop.unlink()
    resumed = world.controller()
    assert resumed.run().reason == "idle"
    assert resumed.state.counts()["packets_open"] == 3


def test_a_stop_file_in_the_shared_store_stops_a_run_elsewhere(tmp_path):
    world = World(tmp_path)
    world.state_store.write_text(layout.CONTROL_STOP_PATH, "stop")
    controller = world.controller()
    controller.add_task(task_spec())
    assert controller.run().reason == "stop_file"
    world.state_store.delete(layout.CONTROL_STOP_PATH)
    assert world.controller().run().reason == "idle"


def test_a_stuck_batch_is_waited_for_until_the_time_budget_then_resumed(tmp_path):
    clock = Clock()
    world = World(tmp_path, config=make_config(poll_seconds=30), polls_until_ended=1000)
    controller = world.controller(clock=clock, sleep=clock.sleep)
    controller.add_task(task_spec(("001", "002")))
    summary = controller.run(time_budget_seconds=100)
    assert summary.reason == "time_budget"
    assert world.provider.submit_count == 1
    assert len(controller.state.batches_to_poll()) == 1

    world.polls_until_ended = 0
    resumed = world.controller()
    resumed.run()
    assert world.provider.submit_count == 1  # polled, never resubmitted
    assert resumed.state.counts()["batches_waiting"] == 0


def test_a_keyboard_interrupt_ends_the_run_cleanly(tmp_path):
    class Interrupting(FakeWriter):
        def poll(self, handle):
            raise KeyboardInterrupt

    world = World(tmp_path, writer_class=Interrupting)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    summary = controller.run()
    assert summary.reason == "interrupt"
    stopped = world.events_of("controller_stopped")
    assert stopped and stopped[-1].payload["reason"] == "interrupt"
    assert world.events_of("batch_submitted")  # the submitted batch id was recorded before the interrupt
    # a later run finds the batch and finishes it
    world.writer_class = FakeWriter
    resumed = world.controller()
    resumed.run()
    assert world.provider.submit_count == 1
    assert resumed.state.counts()["batches_waiting"] == 0


def test_a_provider_error_is_retried_once_as_a_rebound_call(tmp_path):
    def behavior(payload):
        if payload["attempt"] == 1:
            return FakeOutcome(error="overloaded", retryable=True)
        return FakeOutcome(output=json.dumps({"id": "ok", "thoughts": ["fine"]}))

    world = World(tmp_path, behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller)
    prepared = world.events_of("call_prepared")
    assert [e.payload["purpose"] for e in prepared] == ["production"] * 3 + ["rebound"] * 3
    assert all(e.payload["attempt"] == 2 and e.payload["retry_of"] for e in prepared[3:])
    assert len(world.shipped_rows()) == 3
    assert controller.state.spend("fake-batch", "rebound").calls == 3


def test_an_unusable_output_is_a_billed_failure_retried_with_the_error_as_feedback(tmp_path):
    def behavior(payload):
        if payload["attempt"] == 1:
            return FakeOutcome(output="this is not json")
        return FakeOutcome(output=json.dumps({"id": "ok", "thoughts": ["fine"]}))

    world = World(tmp_path, behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller)
    failed = world.events_of("call_failed")
    assert len(failed) == 1
    assert failed[0].payload["billed"] is True and failed[0].payload["usd"] > 0
    assert "unusable output" in failed[0].payload["error"]
    retry_request = [r for r in world.provider.batches.values()][1]["requests"][0][2]
    assert retry_request["attempt"] == 2 and "unusable output" in retry_request["feedback"][0]
    assert len(world.shipped_rows()) == 1


def test_a_line_that_fails_twice_is_exhausted_and_the_run_still_ends(tmp_path):
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output="never json"))
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    summary = controller.run()
    assert summary.reason == "idle"
    assert summary.progress["lines_exhausted"] == 2
    assert world.provider.submit_count == 2  # one batch per attempt, no endless retrying
    assert world.shipped_rows() == []


def test_a_skip_object_sets_the_line_aside(tmp_path):
    skip = {"skip": True, "class": "person", "reason": "a person appears in the picture", "ids": ["x"]}

    def behavior(payload):
        return FakeOutcome(output=json.dumps(skip))

    world = World(tmp_path, behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    summary = controller.run()
    assert summary.progress["lines_set_aside"] == 1
    (event,) = world.events_of("row_set_aside")
    assert event.payload["class"] == "person"
    assert controller.state.rows == {}
    assert world.provider.submit_count == 1  # a skip is not retried


def test_calls_missing_from_the_batch_results_fail_and_are_retried(tmp_path):
    class Dropping(FakeWriter):
        dropped = False

        def results(self, handle):
            results = super().results(handle)
            if not Dropping.dropped and len(results) > 1:
                Dropping.dropped = True
                return results[:-1]
            return results

    Dropping.dropped = False
    world = World(tmp_path, writer_class=Dropping)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller)
    failed = world.events_of("call_failed")
    assert len(failed) == 1 and "missing from the batch results" in failed[0].payload["error"]
    assert len(world.shipped_rows()) == 3


def test_a_batch_that_fails_as_a_whole_fails_all_its_calls_and_they_are_retried(tmp_path):
    class Failing(FakeWriter):
        failed_once = False

        def poll(self, handle):
            if not Failing.failed_once:
                Failing.failed_once = True
                return PollStatus("expired", "ran past 24 hours")
            return super().poll(handle)

    Failing.failed_once = False
    world = World(tmp_path, writer_class=Failing)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller)
    errors = [e.payload["error"] for e in world.events_of("call_failed")]
    assert errors == ["the batch expired: ran past 24 hours"] * 2
    assert len(world.shipped_rows()) == 2


def test_an_assessor_failure_retries_the_call_with_the_error_lines(tmp_path):
    def assess(row, context):
        if context.attempt == 1:
            return Assessment(passed=False, errors=("a thought defines a word instead of looking",))
        return Assessment(passed=True)

    world = World(tmp_path)
    controller = world.controller(assess=assess)
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller)
    rejected = world.events_of("row_rejected")
    assert len(rejected) == 2 and all(e.payload["by"] == "assessor" and e.payload["retry"] for e in rejected)
    retry_requests = [req for batch in world.provider.batches.values() for req in batch["requests"] if req[2]["attempt"] == 2]
    assert len(retry_requests) == 2
    assert all(req[2]["feedback"] == ["a thought defines a word instead of looking"] for req in retry_requests)
    assert len(world.shipped_rows()) == 2
    assert controller.state.spend("fake-batch", "rebound").calls == 2


def test_an_assessor_can_set_a_row_aside(tmp_path):
    def assess(row, context):
        if context.line == "002":
            return Assessment(passed=True, set_aside={"class": "person", "reason": "a person in the seed"})
        return Assessment(passed=True)

    world = World(tmp_path)
    controller = world.controller(assess=assess)
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller)
    assert len(world.shipped_rows()) == 2
    assert controller.state.job_progress(2)["lines_set_aside"] == 1


def test_a_model_cannot_write_its_own_writer_label(tmp_path):
    forged = {"id": "x", "thoughts": ["t"], "writer": {"provider": "human", "model": "phil", "route": "hand"}}
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=json.dumps(forged)))
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller)
    (row,) = world.shipped_rows()
    assert row["writer"]["provider"] == "fake" and row["writer"]["route"] == "batch"
    # the forgery is not a free pass: the row was flagged and needed two keeps
    assert len(world.events_of("verdict_received")) == 2


def test_a_route_without_a_writer_or_a_license_is_refused_up_front(tmp_path):
    world = World(tmp_path)
    controller = world.controller()
    with pytest.raises(ConfigError, match="no license"):
        controller.add_task(task_spec(source="no-license-source"))
    with pytest.raises(ConfigError, match="unknown route"):
        controller.add_task(task_spec(route="nowhere"))
    broken = World(tmp_path / "other", config=make_config())
    controller = broken.controller()
    controller.writers.pop("fake-batch")
    with pytest.raises(ConfigError, match="no writer"):
        controller.start()


def test_a_task_with_the_same_id_and_another_route_is_a_conflict(tmp_path):
    config = make_config(
        routes={
            "fake-batch": {"provider": "fake", "model": "fake-model", "kind": "batch", "budget_usd": 5, "purpose_caps": {"production": 5}},
            "other-route": {"provider": "fake", "model": "fake-model", "kind": "batch", "budget_usd": 5, "purpose_caps": {"production": 5}},
        }
    )
    world = World(tmp_path, config=config)
    controller = world.controller()
    assert controller.add_task(task_spec()) is not None
    assert controller.add_task(task_spec()) is None  # recording the same task again changes nothing
    with pytest.raises(TaskConflict):
        controller.add_task(task_spec(route="other-route"))
    # the same lines through another route are a different task when given a variant
    assert controller.add_task(task_spec(route="other-route", variant="other-route")) is not None


def test_the_planner_is_asked_every_pass_and_known_tasks_are_ignored(tmp_path):
    world = World(tmp_path)
    asked = []

    def planner(state):
        asked.append(len(state.tasks))
        return [task_spec(("001", "002", "003")), task_spec(("004", "005", "006"), batch="2")][: 1 + (len(asked) > 1)]

    controller = world.controller(planner=planner)
    world.drive(controller)
    assert len(controller.state.tasks) == 2
    assert len(world.events_of("task_created")) == 2
    assert len(world.shipped_rows()) == 6


def test_a_torn_last_event_line_is_reported_and_the_run_carries_on(tmp_path):
    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003")))
    first.run()
    path = world.state_store.root / sorted(p for p in world.state_store.list("events/"))[-1]
    with open(path, "ab") as handle:
        handle.write(b'{"ts":"2026-11-07T09:59:59.000000Z","actor":"controller-x","kind":"call_pre')
    second = world.controller()
    second.start()
    assert any("torn last line" in problem for problem in second.problems)
    assert any("torn last line" in line for line in world.printed)
    second.run()
    world.run_reviewers()
    third = world.controller()
    third.run()
    assert len(world.shipped_rows()) == 3


def test_config_notes_are_printed_when_a_run_starts(tmp_path):
    config = make_config(
        routes={"fake-batch": {"provider": "fake", "model": "fake-model", "kind": "batch", "budget_usd": 5, "purpose_caps": {"production": 5}}}
    )
    world = World(tmp_path, config=config)
    world.controller().start()
    assert any("no cap for purpose 'pricing'" in line for line in world.printed)


def test_the_status_text_shows_spend_per_route_and_purpose_against_its_cap(tmp_path):
    world = World(tmp_path)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller)
    text = controller.format_status()
    for expected in ("job 3 lines", "production", "rebound", "pricing", "(route total)", "prices dated"):
        assert expected in text
    assert controller.status()["progress"]["lines_total"] == 3
    state = State.from_events(world.events())
    assert state.snapshot() == controller.state.snapshot()  # the live state is the replayed state
