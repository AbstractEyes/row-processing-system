"""An answer in the writer's wrapper that the rules cannot convert is never stored as a row.

The rule pool's normalizer turns the writer's wrapper (`{"skip", "reason", "class", "rows"}`) into
stored rows. When one row cannot be converted it hands the wrapper back unchanged. The live pricing
run stored that wrapper whole as a single row; the controller now records the call as unusable, with
the rows named, and the next attempt is told what failed.
"""

from __future__ import annotations

import json

import pytest

from rps.controller import parse_output
from rps.prompting import make_rules
from rps.providers import FakeOutcome

from harness import World, keep_all
from test_controller_rules import conform, example, plain_task, pool_config, pool_task, sent_payloads

BROKEN_ROWS = {
    "a record key of one word": lambda row: row["records"].append({"key": "mood", "value": "calm"}),
    "a record value that is not text": lambda row: row["records"][0].update(value=3),
    "a wording that is not an object": lambda row: row.update(wordings=[None]),
    "a missing field": lambda row: row.pop("questions"),
}


def broken_after_conforming(payload, output, damage, which=1):
    """The writer's answer as the task text asks for it, with one row damaged."""
    data = json.loads(conform(payload, output).output)
    damage(data["rows"][min(which, len(data["rows"]) - 1)])
    return FakeOutcome(output=json.dumps(data))


@pytest.mark.parametrize("damage", BROKEN_ROWS.values(), ids=list(BROKEN_ROWS))
def test_a_series_with_one_unconvertible_row_is_one_failed_call_and_is_written_again_whole(tmp_path, damage):
    series = example("people_series")

    def behavior(payload):
        if payload["attempt"] == 1:
            return broken_after_conforming(payload, series["output"], damage)
        return conform(payload, series["output"])

    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([series["item"]]))
    world.drive(controller, keep_all)

    failed = world.events_of("call_failed")
    assert len(failed) == 1
    payload = failed[0].payload
    assert payload["kind"] == "invalid_output" and payload["billed"] is True and payload["retryable"] is True
    assert payload["error"].startswith("unusable output: ")
    assert "art-900002" in payload["error"]  # the row that cannot be converted is named
    assert "art-900001" not in payload["error"]
    # the first call recorded no row at all: the wrapper was not kept as one
    returned = world.events_of("call_returned")
    assert len(returned) == 1 and len(returned[0].payload["rows"]) == 3
    assert all(row["id"].startswith("art-9000") and "rows" not in row for row in returned[0].payload["rows"])
    # the next attempt was told which rows failed
    retry = next(item for item in sent_payloads(world) if item["attempt"] == 2)
    assert "failed these checks" in retry["task_text"] and "art-900002" in retry["task_text"]
    assert len(world.shipped_rows()) == 3


def test_a_single_row_in_a_wrapper_that_cannot_be_converted_is_a_failed_call_not_a_row(tmp_path):
    seeded = example("seeded_single")
    behavior = lambda payload: broken_after_conforming(  # noqa: E731
        payload, seeded["output"], lambda row: row["records"].append({"key": "relation 1", "value": 7}), which=0
    )
    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([seeded["item"]]))
    world.drive(controller, keep_all)

    # both attempts failed the same way: two failed calls, no row, nothing shipped, the line is out of attempts
    failed = world.events_of("call_failed")
    assert [event.payload["kind"] for event in failed] == ["invalid_output", "invalid_output"]
    assert all("art-096005" in event.payload["error"] for event in failed)
    assert world.events_of("call_returned") == []
    assert controller.state.rows == {}
    assert world.shipped_rows() == []
    assert controller.state.job_progress(2)["lines_exhausted"] == 1


def test_the_failed_call_keeps_the_rows_that_could_not_be_converted_for_whoever_reads_the_log(tmp_path):
    series = example("people_series")
    behavior = lambda payload: broken_after_conforming(  # noqa: E731
        payload, series["output"], BROKEN_ROWS["a record key of one word"]
    )
    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([series["item"]]))
    world.drive(controller, keep_all)
    failed = world.events_of("call_failed")
    assert len(failed) == 2
    for event in failed:
        kept = event.payload["unconverted_rows"]
        assert [row["id"] for row in kept] == ["art-900002"]  # only the bad row, not the two good ones
        assert {"key": "mood", "value": "calm"} in kept[0]["records"]
        assert event.payload["output"].startswith("{")  # the start of the whole answer is kept as before


def wrapper(rows, **fields):
    return {"skip": False, "reason": "", "class": "", "rows": rows, **fields}


def test_a_normalizer_that_hands_every_wrapper_back_gives_a_failed_call_that_names_no_row(tmp_path):
    """Rows that all convert alone but not together: the call still fails, and no row is blamed."""
    answer = json.dumps(wrapper([{"id": "a", "thoughts": ["x"]}, {"id": "b", "thoughts": ["y"]}]))

    def one_row_at_a_time(text, spec, model):
        data = json.loads(text)
        return json.dumps(data["rows"]) if len(data["rows"]) == 1 else text

    rules = make_rules("write json rows", {"type": "object"}, "v1", normalize=one_row_at_a_time)
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=answer), polls_until_ended=0)
    controller = world.controller(rules=rules)
    controller.add_task(plain_task())
    for _ in range(6):
        controller.step()
    failed = world.events_of("call_failed")
    assert failed and failed[0].payload["kind"] == "invalid_output"
    assert "holding 2 rows" in failed[0].payload["error"] and "cannot be turned" not in failed[0].payload["error"]
    assert controller.state.rows == {}


def test_a_rule_set_without_a_normalizer_still_does_not_store_a_wrapper_as_a_row(tmp_path):
    answer = json.dumps(wrapper([{"id": "a", "thoughts": ["x"]}]))
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=answer), polls_until_ended=0)
    controller = world.controller()
    controller.add_task(plain_task())
    for _ in range(6):
        controller.step()
    failed = world.events_of("call_failed")
    assert failed and "no function that turns it into rows" in failed[0].payload["error"]
    assert controller.state.rows == {}


# ---------------------------------------------------------------------------- parse_output


def test_a_wrapper_is_not_a_row_in_the_parsed_output():
    rows, skips, problem = parse_output(json.dumps(wrapper([{"id": "a"}, {"id": "b"}])))
    assert (rows, skips) == ([], [])
    assert problem is not None and "2 rows" in problem and "wrapper" in problem


def test_a_wrapper_among_other_objects_is_not_a_row_either():
    rows, skips, problem = parse_output(json.dumps([{"id": "x", "thoughts": []}, wrapper([{"id": "a"}])]))
    assert rows == [] and skips == [] and problem is not None


def test_a_skip_wrapper_is_still_a_skip_and_plain_rows_are_still_rows():
    rows, skips, problem = parse_output(json.dumps({"skip": True, "reason": "no person", "class": "person", "rows": []}))
    assert problem is None and rows == [] and [s["class"] for s in skips] == ["person"]
    rows, skips, problem = parse_output(json.dumps([{"id": "a", "thoughts": ["x"]}, {"id": "b", "thoughts": ["y"]}]))
    assert problem is None and [r["id"] for r in rows] == ["a", "b"] and skips == []


def test_an_object_with_a_rows_field_but_no_skip_flag_is_an_ordinary_row():
    rows, _skips, problem = parse_output(json.dumps({"id": "a", "rows": ["x"]}))
    assert problem is None and len(rows) == 1
