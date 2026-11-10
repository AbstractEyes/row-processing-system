"""The compiled rule pool: generated files are current, the schema is strict-safe, the examples are valid."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from thought_seeds.core import HELD, tp

from rps import rules
from rps.rules import balance, compile as rules_compile, form, task_text

RULES_DIR = Path(rules.__file__).parent
FORBIDDEN_SCHEMA_KEYWORDS = {"minLength", "maxLength", "minimum", "maximum", "pattern", "minItems", "maxItems", "$ref", "$defs", "oneOf", "allOf"}


def test_generated_files_are_current(capsys):
    assert rules_compile.main(["--check"]) == 0, capsys.readouterr().err


def test_version_is_a_content_hash():
    version = rules.rules_version()
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    assert version == rules_compile.rules_version()


def _walk(schema, path="root"):
    yield path, schema
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties":
                for name, sub in value.items():
                    yield from _walk(sub, f"{path}.{name}")
            elif key == "items":
                yield from _walk(value, f"{path}[]")


def test_schema_is_strict_safe():
    schema = rules.row_schema()
    for path, node in _walk(schema):
        assert not (FORBIDDEN_SCHEMA_KEYWORDS & set(node)), path
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, path
            assert set(node["required"]) == set(node["properties"]), path
    assert set(schema["properties"]) == set(form.WRAPPER_FIELDS)


def test_writer_system_prompt_content():
    text = rules.writer_system()
    assert "json" in text.lower()
    assert '"rows"' in text  # the example row is inside
    assert not re.search(r"\b[OPQS]\d{1,2}\b", text), "no rule numbers"
    assert not re.search(r"\.(md|py|jsonl)\b", text), "no file names"
    for sealed in ("bicycle", "from above", "jeans"):
        assert sealed in text
    assert "pixel" in text.lower()


def test_pool_numbers_every_rule_once_with_a_tag():
    rows = [line for line in rules.pool().splitlines() if re.match(r"\| \d+ \|", line)]
    numbers = [int(re.match(r"\| (\d+) \|", line).group(1)) for line in rows]
    assert numbers == list(range(1, len(numbers) + 1))
    for line in rows:
        assert re.search(r"\| (WRITER|ASSESSOR|READER)( \([a-z]+\))? \|", line), line


def test_review_brief_codes_fit_the_verdict_alphabet():
    from rps.review.packets import REASON_PATTERN

    codes = re.findall(r"^\| ([a-z0-9_]+) \|", rules.review_brief(), flags=re.M)
    assert {"gloss", "invented", "hint_copy", "should_skip", "person", "minor", "other"} <= set(codes)
    assert all(REASON_PATTERN.match(code) for code in codes)


@pytest.mark.parametrize("example", rules.examples(), ids=lambda e: e["name"])
def test_examples_pass_the_validator(example):
    rows = [form.to_row(row, example["item"], "claude-haiku-5-5") for row in example["output"]["rows"]]
    assert len(rows) == len(example["item"]["ids"])
    for row in rows:
        assert tp.validate(row, HELD) == []
    if len(rows) > 1:
        assert tp.validate_series(rows) == []


def test_form_round_trip():
    example = next(e for e in rules.examples() if e["name"] == "people_series")
    row = form.to_row(example["output"]["rows"][2], example["item"], "m")
    again = form.to_model_form(row)
    assert again == example["output"]["rows"][2]
    assert row["source"] == {"kind": "synthetic", "model": "m", "seed_dataset": None, "seed_id": None, "license": "Apache-2.0"}
    assert row["held"] == {"layout": None, "wordings": False, "values": []}


def test_normalize_output():
    example = next(e for e in rules.examples() if e["name"] == "seeded_single")
    text = json.dumps(example["output"])
    out = json.loads(form.normalize_output("```json\n" + text + "\n```", example["item"], "m"))
    assert isinstance(out, list) and out[0]["source"]["seed_dataset"] == "docci"
    skipped = form.normalize_output(json.dumps(form.skip_object("a figure", "person")), example["item"], "m")
    assert json.loads(skipped) == {"skip": True, "reason": "a figure", "class": "person"}
    assert form.normalize_output("not json", example["item"], "m") == "not json"
    broken = json.dumps({"skip": False, "reason": "", "class": "", "rows": [{"id": "x"}]})
    assert form.normalize_output(broken, example["item"], "m") == broken
    assert form.explain_failures(broken, example["item"], "m")[0].startswith("x: the row lacks")


def test_relation_part_cycle_keeps_the_second_thing_to_a_third():
    parts = [balance.relation_part(n) for n in range(300)]
    assert parts.count("b") == 100
    assert parts[:3] == ["a", "rel", "b"]


def test_unknown_keys_are_distinct_unheld_and_valid():
    for example in rules.examples():
        item = example["item"]
        for ordinal in range(40):
            chosen = balance.unknown_keys(item, ordinal)
            keys = list(chosen.values())
            assert len(keys) == len(set(keys))
            deep = item["specs"]["deep"] if item["kind"] == "series" else item["spec"]
            assert not set(keys) & balance.spec_keys(deep)
            assert "simple" not in chosen
            for key in keys:
                system, attribute = key.split(" ", 1)
                assert attribute in tp.SYSTEMS[system]


def test_unknown_key_rotation_spreads_over_the_keys_that_fit():
    person = {"kind": "single", "depth": "deep", "spec": {"person": {"gender": "woman"}}}
    used = {balance.unknown_keys(person, n)["deep"] for n in range(len(balance.UNKNOWN_KEYS))}
    assert used == set(balance.UNKNOWN_KEYS) - balance.THING_KEYS - balance.NEVER_ASKED
    thing = {"kind": "single", "depth": "deep", "spec": {"thing": {"name": "bench"}}}
    used = {balance.unknown_keys(thing, n)["deep"] for n in range(len(balance.UNKNOWN_KEYS))}
    assert used == set(balance.UNKNOWN_KEYS) - balance.PERSON_KEYS - balance.NEVER_ASKED


def test_a_key_must_fit_the_item_it_is_asked_about():
    thing = {"kind": "single", "depth": "deep", "spec": {"thing": {"name": "bench"}}}
    asked = {balance.unknown_keys(thing, n)["deep"] for n in range(60)}
    assert not asked & balance.PERSON_KEYS and "mood tone" not in asked
    posed = {"kind": "single", "depth": "deep", "spec": {"person": {"gender": "man"}, "body": {"pose": "kneeling"}}}
    assert "body gesture" not in {balance.unknown_keys(posed, n)["deep"] for n in range(60)}
    mixed = {"kind": "single", "depth": "deep", "spec": {"person": {"gender": "man"}}}
    assert not {balance.unknown_keys(mixed, n)["deep"] for n in range(60)} & balance.THING_KEYS
    seeded = {"kind": "single", "depth": "deep", "hint": "A bench by a path.", "spec": {"thing": {"name": "bench"}}}
    assert not {balance.unknown_keys(seeded, n)["deep"] for n in range(60)} & balance.THING_KEYS


def test_task_text_carries_ids_records_hint_and_assignment():
    example = next(e for e in rules.examples() if e["name"] == "seeded_single")
    text = task_text.build_task_text(example["item"], 4)
    assert "art-096005" in text and "Hint:" in text and "thing name = grasshopper" in text
    assert "holds no relation" in text  # the seeded item gives no relation: no relation question
    series = next(e for e in rules.examples() if e["name"] == "people_series")
    text = task_text.build_task_text(series["item"], 0)
    assert "art-900001, art-900002, art-900003" in text
    assert "relation 1: woman | beside | table" in text
    assert "ask 'rel1 a'" in text  # ordinal 0 -> first part, for an item that holds a relation
    assert "medium = caption, prompt, tags" in text


def test_lists_offer_no_sealed_value():
    text = rules.writer_system()
    lists = text[text.index("THE LISTS") :]
    offered = {v.strip() for line in lists.splitlines()[1:] if ": " in line and not line.startswith("relation") for v in line.split(": ", 1)[1].split(",")}
    assert not offered & set(tp.sealed_all(HELD))


def test_a_missing_license_is_an_error():
    with pytest.raises(form.FormError):
        form.source_block({"seed_id": None}, "m")


def test_hint_rule_is_in_the_prompt():
    assert "never copy a sentence or phrase from it" in rules.writer_system()


def test_the_prompt_holds_no_example_row_by_default():
    assert rules_compile.EXAMPLE_FOR_PROMPT is None
    text = rules.writer_system()
    assert "EXAMPLE ROW" not in text and "example row" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")
    for example in rules.examples():
        for row in example["output"]["rows"]:
            assert row["id"] not in text
            assert all(t["text"] not in text for t in row["thoughts"])


def test_naming_an_example_puts_its_row_back():
    text = rules_compile.writer_system("people_series")
    assert "One example row is at the end." in text and "EXAMPLE ROW" in text
    medium = next(r for r in next(e for e in rules.examples() if e["name"] == "people_series")["output"]["rows"] if r["depth"] == "medium")
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert medium["thoughts"][0]["text"] in text


def test_the_length_caps_in_the_prompt_fit_the_validators_frames():
    text = rules.writer_system()
    for phrase in ("HARD LENGTH LIMIT", "at most 8 slices (simple), 16 (medium) or 64 (deep)", "KEEP THE GIVEN RECORDS", "RELATIONS LIVE ONLY", "A SERIES NESTS"):
        assert phrase in text
    assert tp.SLICE_ROOM == 29 and {d: tp.CHAIN_MAX[d] // tp.SLICE for d in tp.DEPTHS} == {"simple": 8, "medium": 16, "deep": 64}
    longest_step = max(tp.STEPS, key=len)
    caps = {
        "simple": [(1, 100), (2, 100)],
        "medium": [(3, 110), (4, 80), (5, 50)],
        "deep": [(6, 270), (8, 185), (10, 150)],
    }
    for depth, options in caps.items():
        for count, size in options:
            thoughts = [{"step": longest_step, "text": "x" * size} for _ in range(count)]
            assert tp.chain_slices(thoughts) * tp.SLICE <= tp.CHAIN_MAX[depth], (depth, count, size)
            assert str(size) in text


def test_relations_are_not_records_in_the_prompt():
    text = rules.writer_system()
    assert "no record key starts with" in text
    assert "Count the relations as records" not in text


def test_the_deep_floor_is_reachable_within_the_caps():
    assert "about 1,100 bytes of thoughts only when the records are full" in rules.writer_system()
    assert all(count * size >= 1100 for count, size in ((6, 270), (8, 185), (10, 150)))


def _row_in_writer_form():
    example = next(e for e in rules.examples() if e["name"] == "people_series")
    return copy.deepcopy(example["output"]["rows"][1]), example["item"]


def test_a_numeric_depth_and_rank_are_named():
    row, _ = _row_in_writer_form()
    row["depth"], row["rank"] = 2, "2"
    problems = form.row_problems(row)
    assert any("'depth' is 2" in p and "simple, medium, deep" in p for p in problems)
    assert any("'rank' is '2'" in p and "number 1, 2 or 3" in p for p in problems)


def test_a_one_word_record_key_names_the_keys_its_system_adds():
    row, item = _row_in_writer_form()
    row["records"] = [{"key": "mood", "value": "cozy"}, {"key": "story", "value": "a pause"}, {"key": "palette", "value": "warm"}]
    problems = form.row_problems(row)
    assert any("'mood' is one word" in p and "'mood feeling' and 'mood from'" in p for p in problems)
    assert any("'story' is one word" in p and "'story before', 'story after', 'story cause'" in p for p in problems)
    assert any("'palette' is one word" in p and "'system attribute'" in p for p in problems)
    wrapper = json.dumps(form.wrap([row]))
    assert form.normalize_output(wrapper, item, "m") == wrapper
    lines = form.explain_failures(wrapper, item, "m")
    assert len(lines) == 1 and lines[0].startswith(row["id"] + ": ") and "'mood' is one word" in lines[0]
    assert form.explain_failures(json.dumps(form.wrap([_row_in_writer_form()[0]])), item, "m") == []
    assert form.explain_failures("not json", item, "m") == []


def test_list_fields_say_the_form_they_need():
    row, _ = _row_in_writer_form()
    row["wordings"] = {"caption": "A woman."}
    row["thoughts"] = [{"text": "No step."}]
    problems = form.row_problems(row)
    assert any("'wordings'" in p and "a list" in p for p in problems)
    assert any("thoughts item 1" in p and "step" in p for p in problems)


def test_a_good_row_has_no_problem():
    row, _ = _row_in_writer_form()
    assert form.row_problems(row) == []


def test_the_prompt_states_the_format_and_the_keys_each_depth_adds():
    text = rules.writer_system()
    for phrase in ("FORMAT:", "depth is a word", '"mood feeling" and "mood from"', '"story before", "story after", "story cause"', "prefer US spelling", "the deep rank keeps every record and relation of the medium rank unchanged"):
        assert phrase in text


def test_the_json_format_block_is_placeholders_only():
    text = rules.json_format()
    assert '"depth": "<simple or medium or deep>"' in text and "Never 1, 2 or 3" in text
    assert '"mood feeling", "mood from"' in text and '"story before", "story after", "story cause"' in text
    assert "skip" in text and "<one sentence>" in text
    for example in rules.examples():
        for row in example["output"]["rows"]:
            assert row["id"] not in text and not any(t["text"] in text for t in row["thoughts"])


def test_the_format_block_is_part_of_the_version():
    assert "json_format.md" in rules_compile.VERSIONED


def test_the_genres_drawn_specs_carry_are_on_the_list_and_in_the_prompt():
    from rps.assessor.checks import EXTRA_GENRES, GENRES

    text = rules.writer_system()
    assert "animal study" in GENRES and EXTRA_GENRES <= GENRES
    assert all(genre in text for genre in EXTRA_GENRES)
    assert "so replace it" in text and "first add one fact its hint states plainly" in text


def test_an_off_list_drawn_genre_is_named_in_the_task_text():
    item = dict(_example_item(), genre="amphora")
    text = task_text.build_task_text(item, 0)
    assert 'the drawn genre "amphora" is not on the genre list' in text and "choose the genre that fits from the list" in text
    assert "Genre: amphora." not in text
    listed = task_text.build_task_text(dict(item, genre="animal study"), 0)
    assert "Genre: animal study." in listed
    assert "choose the one that fits" in task_text.build_task_text({k: v for k, v in item.items() if k != "genre"}, 0)


def _example_item():
    return copy.deepcopy(next(e for e in rules.examples() if e["name"] == "painting_deep")["item"])
