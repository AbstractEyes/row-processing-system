"""The controller driven with the shipped rule pool and its assessor, end to end on the fake writer."""

from __future__ import annotations

import json
import re

import pytest

from rps import rules
from rps.assess import Assessment
from rps.config import ConfigError
from rps.controller import ControllerError
from rps.providers import FakeOutcome
from rps.rules import balance, task_text
from rps.tasks import SpecLine, TaskSpec

from harness import World, keep_all
from helpers import make_config


def pool_config(**rules_section):
    return make_config(rules={"package": "rps.rules", **rules_section})


def example(name):
    return next(e for e in rules.examples() if e["name"] == name)


def example_items():
    return [e["item"] for e in rules.examples()]


def pool_task(items=None, *, batch="pool"):
    items = items if items is not None else example_items()
    lines = tuple(SpecLine(item["ids"][0], item, item_id=item["ids"][0], license=item["license"]) for item in items)
    return TaskSpec(source="photos", batch=batch, route="fake-batch", purpose="production", variant="", lines=lines)


def plain_task(spec="a plain spec", line="001"):
    return TaskSpec(
        source="photos", batch="1", route="fake-batch", purpose="production", variant="",
        lines=(SpecLine(line, spec, item_id=f"img-{line}"),),
    )


def conform(payload, output):
    """The writer's answer with its questions asked as the call's task text assigned them."""
    text = payload["task_text"]
    part = re.search(r"ask 'rel1 (a|rel|b)'", text)
    line = next((ln for ln in text.splitlines() if "Unknown question key by depth:" in ln), "")
    unknown = dict(re.findall(r"(simple|medium|deep) = '([^']+)'", line))
    out = json.loads(json.dumps(output))
    for row in out["rows"]:
        for q in row["questions"]:
            if part and re.fullmatch(r"rel1 (a|rel|b)", q["key"]):
                q["key"] = f"rel1 {part.group(1)}"
                if q["needs"] == "record":
                    q["value"] = row["relations"][0][part.group(1)]
            if q["needs"] == "unknown" and row["depth"] in unknown:
                q["key"] = unknown[row["depth"]]
    return FakeOutcome(output=json.dumps(out))


def pool_behavior(payload):
    for e in rules.examples():
        if e["item"]["ids"] == payload["spec"]["ids"]:
            return conform(payload, e["output"])
    raise AssertionError("no example for this item")


def sent_payloads(world):
    return [payload for batch in world.provider.batches.values() for _call, _custom, payload in batch["requests"]]


def test_the_rule_pool_drives_a_whole_run_through_its_task_text_normalizer_and_assessor(tmp_path):
    world = World(tmp_path, config=pool_config(), behavior=pool_behavior)
    controller = world.controller()
    real = controller.assess_call
    assert real is not None  # the pool brings its own assessor, used call by call
    seen = []

    def spy(rows, context):
        seen.append(context)
        return real(rows, context)

    controller.assess_call = spy
    controller.add_task(pool_task())
    world.drive(controller, keep_all)

    items = example_items()
    # the user text of each call is the pool's task text for the item at its place in the batch
    payloads = {payload["spec"]["ids"][0]: payload for payload in sent_payloads(world)}
    for ordinal, item in enumerate(items):
        assert payloads[item["ids"][0]]["task_text"] == task_text.build_task_text(item, ordinal)
    # the assessor was told what the writer was told
    by_line = {context.line: context for context in seen}
    for ordinal, item in enumerate(items):
        assert by_line[item["ids"][0]].assignment == balance.assign(item, ordinal).to_dict()
    # every row passed the assessor and was shipped with the rules version on its label
    shipped = world.shipped_rows()
    assert len(shipped) == sum(len(e["output"]["rows"]) for e in rules.examples())
    assert {row["writer"]["rules_version"] for row in shipped} == {rules.rules_version()}
    assert controller.state.job_progress(2)["lines_accepted"] == len(items)
    # the writer's wrapper was turned into stored rows once, by the rules
    assert all("records" not in row and "spec" in row and row["source"]["license"] for row in shipped)


def test_a_series_error_goes_to_every_row_of_the_call_and_the_series_is_rewritten_whole(tmp_path):
    series = example("people_series")
    broken = json.loads(json.dumps(series["output"]))
    broken["rows"][1]["genre"] = "fashion"  # one genre for the whole series
    attempts = []

    def behavior(payload):
        attempts.append(payload["attempt"])
        return conform(payload, broken if payload["attempt"] == 1 else series["output"])

    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([series["item"]]))
    world.drive(controller, keep_all)
    assert attempts == [1, 2]
    rejected = world.events_of("row_rejected")
    assert len(rejected) == 3 and all(event.payload["by"] == "assessor" for event in rejected)
    lines = {line for event in rejected for line in event.payload["errors"]}
    assert any(line.startswith("series_genre: ") for line in lines)  # "code: message"
    retry = next(payload for payload in sent_payloads(world) if payload["attempt"] == 2)
    assert "failed these checks" in retry["task_text"]
    assert all(line in retry["task_text"] for line in lines)
    assert len(world.shipped_rows()) == 3


def test_flags_are_bare_codes_and_do_not_reject_the_row(tmp_path):
    seeded = example("seeded_single")
    output = json.loads(json.dumps(seeded["output"]))
    output["rows"][0]["thoughts"][0]["text"] = "Someone stops to look at the grasshopper on the glass."
    world = World(tmp_path, config=pool_config(), behavior=lambda payload: conform(payload, output))
    controller = world.controller()
    controller.add_task(pool_task([seeded["item"]]))
    world.drive(controller, keep_all)
    assessed = world.events_of("row_assessed")
    assert len(assessed) == 1 and assessed[0].payload["passed"] is True
    assert "passerby_present" in assessed[0].payload["flags"]
    assert all(" " not in flag for flag in assessed[0].payload["flags"])
    assert assessed[0].payload["errors"] == []
    assert len(world.shipped_rows()) == 1


def test_a_rule_set_without_task_text_sends_the_spec_as_it_is(tmp_path):
    seen = {}

    def behavior(payload):
        seen["text"] = payload["task_text"]
        return FakeOutcome(output=json.dumps({"id": "ok", "thoughts": ["fine"]}))

    world = World(tmp_path, behavior=behavior, polls_until_ended=0)
    controller = world.controller()
    controller.add_task(plain_task("a plain spec"))
    controller.step()
    controller.step()
    assert seen["text"] == "a plain spec"


def test_explicit_assess_functions_win_over_the_rule_sets_assessor(tmp_path):
    calls = []

    def mine(row, context):
        calls.append(context.line)
        return Assessment(passed=True)

    world = World(tmp_path, config=pool_config(), behavior=pool_behavior)
    controller = world.controller(assess=mine)
    assert controller.assess is mine and controller.assess_call is None
    controller.add_task(pool_task())
    world.drive(controller, keep_all)
    assert len(calls) == sum(len(e["output"]["rows"]) for e in rules.examples())


def test_a_call_level_function_must_answer_for_every_row(tmp_path):
    two_rows = json.dumps([{"id": "a", "thoughts": ["x"]}, {"id": "b", "thoughts": ["y"]}])
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=two_rows), polls_until_ended=0)
    controller = world.controller(assess_call=lambda rows, context: [Assessment(passed=True)])
    controller.add_task(plain_task())
    with pytest.raises(ControllerError, match="2 rows"):
        for _ in range(5):
            controller.step()


def test_the_call_level_function_sees_every_row_of_one_call_together(tmp_path):
    rows_seen = []

    def judge(rows, context):
        rows_seen.append([row["id"] for row in rows])
        return [Assessment(passed=True) for _ in rows]

    three_rows = json.dumps([{"id": f"r{i}", "thoughts": ["x"]} for i in range(3)])
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=three_rows), polls_until_ended=0)
    controller = world.controller(assess_call=judge)
    controller.add_task(plain_task())
    world.drive(controller, keep_all)
    assert rows_seen == [["r0", "r1", "r2"]]


def test_the_assessors_name_in_the_configuration_is_checked_and_can_be_turned_off(tmp_path):
    with pytest.raises(ConfigError):
        pool_config(assessor="no_colon")
    world = World(tmp_path, config=pool_config(assessor="none"), behavior=pool_behavior)
    controller = world.controller()
    assert controller.rules is not None and controller.rules.assessor is None
    assert controller.assess_call is None


def test_an_assessor_that_cannot_be_loaded_is_a_configuration_error(tmp_path):
    world = World(tmp_path, config=pool_config(assessor="rps.no_such_module:Nothing"))
    with pytest.raises(ConfigError, match="cannot be loaded"):
        world.controller()


def test_an_answer_the_rules_cannot_turn_into_rows_is_an_unusable_output_and_is_retried(tmp_path):
    seeded = example("seeded_single")
    def behavior(payload):
        return FakeOutcome(output="this is not json at all") if payload["attempt"] == 1 else conform(payload, seeded["output"])

    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([seeded["item"]]))
    world.drive(controller, keep_all)
    failed = world.events_of("call_failed")
    assert len(failed) == 1 and failed[0].payload["kind"] == "invalid_output" and failed[0].payload["billed"] is True
    assert len(world.shipped_rows()) == 1
