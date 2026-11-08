"""An answer in the writer's wrapper that the rules cannot convert is never stored as a row.

The rule pool's normalizer turns the writer's wrapper (`{"skip", "reason", "class", "rows"}`) into
stored rows. When one row cannot be converted it hands the wrapper back unchanged. The live pricing
run stored that wrapper whole as a single row; the controller now records the call as unusable, with
the rows named, and the next attempt is told what failed. When the rule set can explain each row
(its `explain` function), the lines are part of what the next attempt is told, within the error's length.
"""

from __future__ import annotations

import json

import pytest

from rps.controller import _fit_lines, parse_output
from rps.failures import PROVIDER_ERROR_CHARS
from rps.prompting import load_writer_rules, make_rules
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


# ---------------------------------------------------------------------------- the converter's advice for the retry


def numeric_depth_and_one_word_key(row):
    row["depth"] = 2
    row["records"].append({"key": "mood", "value": "calm"})


def test_the_pool_rules_carry_the_converters_explanation():
    from rps.config import RulesConfig
    from rps.rules import form

    rules = load_writer_rules(RulesConfig(package="rps.rules"))
    assert rules.explain is form.explain_failures and rules.normalize is form.normalize_output


def test_the_retry_is_told_what_is_wrong_with_each_row_in_the_converters_words(tmp_path):
    """The real converter: a numeric depth and a one-word key in one row give both messages in the retry's text."""
    series = example("people_series")

    def behavior(payload):
        if payload["attempt"] == 1:
            return broken_after_conforming(payload, series["output"], numeric_depth_and_one_word_key)
        return conform(payload, series["output"])

    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([series["item"]]))
    world.drive(controller, keep_all)

    [failed] = world.events_of("call_failed")
    error = failed.payload["error"]
    assert error.startswith("unusable output: the answer is the writer's wrapper, and 1 of its 3 rows cannot be turned")
    assert "(art-900002); check each of them, field by field, against the format in the rules. Row by row:" in error
    assert "art-900002: 'depth' is 2; it is one of the words simple, medium, deep" in error
    assert "record key 'mood' is one word" in error
    assert "art-900001:" not in error and "art-900003:" not in error  # only the row that fails is explained
    assert len(error) <= PROVIDER_ERROR_CHARS
    retry = next(item for item in sent_payloads(world) if item["attempt"] == 2)
    assert "failed these checks" in retry["task_text"]
    assert "art-900002: 'depth' is 2; it is one of the words simple, medium, deep" in retry["task_text"]
    assert "record key 'mood' is one word" in retry["task_text"]
    assert len(world.shipped_rows()) == 3  # and the second attempt was taken


def test_each_failing_row_of_a_series_gets_its_own_line(tmp_path):
    series = example("people_series")

    def damage_two(payload, output):
        data = json.loads(conform(payload, output).output)
        data["rows"][0]["depth"] = 1
        data["rows"][2]["records"].append({"key": "story", "value": "x"})
        return FakeOutcome(output=json.dumps(data))

    behavior = lambda payload: damage_two(payload, series["output"]) if payload["attempt"] == 1 else conform(payload, series["output"])  # noqa: E731
    world = World(tmp_path, config=pool_config(), behavior=behavior)
    controller = world.controller()
    controller.add_task(pool_task([series["item"]]))
    world.drive(controller, keep_all)
    [failed] = world.events_of("call_failed")
    error = failed.payload["error"]
    assert "2 of its 3 rows cannot be turned" in error and "(art-900001, art-900003)" in error
    lines = [line.strip() for line in error.splitlines()[1:]]
    assert [line.split(":")[0] for line in lines] == ["art-900001", "art-900003"]
    assert "'depth' is 1" in lines[0] and "record key 'story' is one word" in lines[1]


def explained_world(tmp_path, explain, answer=None):
    """A plain rule set whose converter hands every wrapper back, with `explain` to say why."""
    answer = answer or json.dumps(wrapper([{"id": "a", "thoughts": ["x"]}, {"id": "b", "thoughts": ["y"]}]))
    rules = make_rules("write json rows", {"type": "object"}, "v1", normalize=lambda text, spec, model: text, explain=explain)
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=answer), polls_until_ended=0)
    controller = world.controller(rules=rules)
    controller.add_task(plain_task())
    for _ in range(6):
        controller.step()
    return world


def test_the_explain_function_is_handed_the_answer_the_spec_and_the_model(tmp_path):
    seen = []

    def explain(text, spec, model):
        seen.append((json.loads(text)["rows"][0]["id"], spec, model))
        return ["a: the thoughts are not objects", "b: the thoughts are not objects either"]

    world = explained_world(tmp_path, explain)
    [first, *_more] = world.events_of("call_failed")
    assert seen and seen[0] == ("a", "a plain spec", world.config.routes["fake-batch"].model)
    assert first.payload["error"].endswith("Row by row:\n  a: the thoughts are not objects\n  b: the thoughts are not objects either")
    assert first.payload["error"].startswith("unusable output: the answer is the writer's wrapper, and 2 of its 2 rows")


def test_an_explain_function_that_gives_nothing_or_fails_changes_nothing(tmp_path):
    def broken(text, spec, model):
        raise RuntimeError("the explainer is broken")

    for explain in (lambda text, spec, model: [], lambda text, spec, model: None, broken):
        sub = tmp_path / str(id(explain))
        world = explained_world(sub, explain)
        failed = world.events_of("call_failed")
        assert failed and all(event.payload["kind"] == "invalid_output" for event in failed)
        assert "Row by row" not in failed[0].payload["error"]
        assert failed[0].payload["error"].endswith("against the format in the rules")


def test_a_rule_set_without_an_explain_function_gives_the_sentence_alone(tmp_path):
    answer = json.dumps(wrapper([{"id": "a", "thoughts": ["x"]}]))
    rules = make_rules("write json rows", {"type": "object"}, "v1", normalize=lambda text, spec, model: text)
    assert rules.explain is None
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output=answer), polls_until_ended=0)
    controller = world.controller(rules=rules)
    controller.add_task(plain_task())
    for _ in range(6):
        controller.step()
    [first, *_more] = world.events_of("call_failed")
    assert first.payload["error"].endswith("against the format in the rules") and "\n" not in first.payload["error"]


def test_many_long_lines_are_cut_to_whole_lines_within_the_error_length(tmp_path):
    lines = [f"row-{n:02d}: " + "the thoughts are wrong in this way; " * 6 for n in range(1, 21)]
    world = explained_world(tmp_path, lambda text, spec, model: lines)
    [first, *_more] = world.events_of("call_failed")
    error = first.payload["error"]
    assert len(error) <= PROVIDER_ERROR_CHARS
    shown = [line for line in error.splitlines()[1:] if line.strip().startswith("row-")]
    assert shown and all(line.strip() == lines[n].strip() for n, line in enumerate(shown))  # whole lines, in order
    last = error.splitlines()[-1].strip()
    assert last == f"and {len(lines) - len(shown)} more"
    retry = next(item for item in sent_payloads(world) if item["attempt"] == 2)
    assert error in retry["task_text"] and last in retry["task_text"]


def test_one_very_long_line_is_cut_short_and_marked():
    lines = ["a: " + "word " * 600]
    block = _fit_lines(lines, 400)
    assert block.startswith("\n  a: word word") and block.endswith(" ...") and len(block) <= 400
    assert "\n" not in block[1:]


def test_fit_lines_keeps_whole_lines_and_counts_the_rest():
    lines = [f"r{n}: " + "x" * 50 for n in range(10)]
    block = _fit_lines(lines, 5 * 60 + 20)
    shown = block.count("\n  r")
    assert 1 <= shown < 10 and len(block) <= 5 * 60 + 20
    assert block.endswith(f"\n  and {10 - shown} more")
    assert _fit_lines(lines, 100000) == "".join(f"\n  {line}" for line in lines)  # all fit: no "more"
    assert _fit_lines(lines, 30) == ""  # not even one line is worth showing
    assert _fit_lines([], 1000) == "" and _fit_lines(["", "  "], 1000) == ""
    assert _fit_lines(["a:  two   spaces\nand a break"], 1000) == "\n  a: two spaces and a break"


def test_an_error_text_of_another_kind_is_held_to_the_limit_too(tmp_path):
    def normalize(text, spec, model):
        raise ValueError("x" * 4000)

    rules = make_rules("write json rows", {"type": "object"}, "v1", normalize=normalize)
    world = World(tmp_path, behavior=lambda payload: FakeOutcome(output="not json"), polls_until_ended=0)
    controller = world.controller(rules=rules)
    controller.add_task(plain_task())
    for _ in range(6):
        controller.step()
    [first, *_more] = world.events_of("call_failed")
    assert first.payload["error"].startswith("unusable output: ") and len(first.payload["error"]) == PROVIDER_ERROR_CHARS


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
