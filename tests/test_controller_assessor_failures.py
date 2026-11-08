"""A checker that cannot read a row rejects the row; it does not stop the run.

The rule pool's converter turns a record whose key starts with "relation" into `spec["relation"]` even when
the row has no relations of its own, and the answer is a dict where the checker expects a list of
`{a, rel, b}`. The checker raised on it, and an exception from an assess function ended the whole run.
"""

from __future__ import annotations

import json

from rps.assess import Assessment
from rps.providers import FakeOutcome

from harness import World, keep_all, task_spec
from test_controller_rules import conform, example, pool_config, pool_task, sent_payloads

BROKEN = "the assessor could not check the row (AttributeError: 'str' object has no attribute 'get')"


def test_a_row_the_assess_function_cannot_read_is_rejected_and_written_again(tmp_path):
    def assess(row, context):
        if context.attempt == 1:
            raise AttributeError("'str' object has no attribute 'get'")
        return Assessment(passed=True)

    world = World(tmp_path)
    controller = world.controller(assess=assess)
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller, keep_all)
    rejected = world.events_of("row_rejected")
    assert len(rejected) == 2 and all(event.payload["by"] == "assessor" and event.payload["retry"] is True for event in rejected)
    assert all(event.payload["errors"] == [BROKEN] for event in rejected)
    assessed = [event for event in world.events_of("row_assessed") if event.payload["passed"] is False]
    assert len(assessed) == 2
    retries = [req[2] for batch in world.provider.batches.values() for req in batch["requests"] if req[2]["attempt"] == 2]
    assert len(retries) == 2 and all(req["feedback"] == [BROKEN] for req in retries)  # the writer is told what happened
    assert controller.state.job_progress(2)["lines_accepted"] == 2
    assert controller.problems == [BROKEN]  # noted once, however many rows it hit
    assert sum(BROKEN in line for line in world.printed) == 1


def test_a_checker_that_keeps_failing_rejects_the_lines_without_ending_the_run(tmp_path):
    def assess(row, context):
        raise ValueError("cannot read this")

    world = World(tmp_path)
    controller = world.controller(assess=assess)
    controller.add_task(task_spec(("001", "002")))
    summary = controller.run()
    assert summary.reason == "idle" and summary.progress["lines_rejected"] == 2
    assert world.shipped_rows() == []
    assert any("ValueError: cannot read this" in problem for problem in summary.problems)


def test_a_call_level_function_that_raises_rejects_every_row_of_the_call_and_the_call_is_written_again(tmp_path):
    three = json.dumps([{"id": f"r{i}", "thoughts": ["x"]} for i in range(3)])

    def judge(rows, context):
        if context.attempt == 1:
            raise KeyError("depth")
        return [Assessment(passed=True) for _ in rows]

    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=three), polls_until_ended=0)
    controller = world.controller(assess_call=judge)
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    rejected = world.events_of("row_rejected")
    assert len(rejected) == 3 and all("KeyError: 'depth'" in event.payload["errors"][0] for event in rejected)
    assert controller.state.job_progress(2)["lines_accepted"] == 1
    assert len(world.shipped_rows()) == 3


def test_a_wrong_number_of_assessments_is_still_a_mistake_of_the_function_and_stops_the_run(tmp_path):
    import pytest

    from rps.controller import ControllerError

    three = json.dumps([{"id": f"r{i}", "thoughts": ["x"]} for i in range(3)])
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=three), polls_until_ended=0)
    controller = world.controller(assess_call=lambda rows, context: [Assessment(passed=True)])
    controller.add_task(task_spec(("001",)))
    with pytest.raises(ControllerError, match="3 rows"):
        for _ in range(6):
            controller.step()


def test_the_pools_checker_no_longer_ends_a_run_on_a_relation_record_that_became_a_dict(tmp_path):
    seeded = example("seeded_single")

    def behavior(payload):
        if payload["attempt"] > 1:
            return conform(payload, seeded["output"])
        data = json.loads(conform(payload, seeded["output"]).output)
        row = data["rows"][0]
        row["records"].append({"key": "relation 1", "value": "left of"})
        row["relations"] = []  # so spec["relation"] becomes {"1": "left of"}
        return FakeOutcome(output=json.dumps(data))

    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([seeded["item"]]))
    world.drive(controller, keep_all)
    first = [event for event in world.events_of("row_assessed") if event.payload["passed"] is False]
    assert len(first) == 1  # the row written the first time failed its check, one way or another
    assert {payload["attempt"] for payload in sent_payloads(world)} == {1, 2}
    assert len(world.shipped_rows()) == 1 and world.shipped_rows()[0]["spec"]["relation"] != {"1": "left of"}
