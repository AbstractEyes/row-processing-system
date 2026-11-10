"""Rows written with the picture attached: the task line, the rule texts and the checker's view of a picture row."""

from __future__ import annotations

import copy
import hashlib
import re

import pytest

from rps import assessor, rules
from rps.assessor import PICTURE_DETAIL_CODES, PICTURE_HINT_CODES, PICTURE_HINT_NOTE, PICTURE_NOTE, is_picture_row
from rps.assessor.problems import ERROR, FLAG
from rps.rules import compile as rules_compile
from rps.rules import form
from rps.prompting import PICTURE_LINE, load_writer_rules, profile_rules, render_task_text
from rps.config import RulesConfig

from harness import World, task_spec
from helpers import RecordingWriter, make_config

MODEL = "test-model"
HINT = (
    "A tidy cafe seen from the door. A cafe with a window and a wooden counter. "
    "Cafe tables near the window with a lamp. A woman at a cafe table in the morning. "
    "The morning light reaches the tables."
)
PROFILES = ("default", "deepseek", "gemini", "openai")
CLASSIFIER_LINE = "A hint that reports what a classifier or another model suggests"


def _example(name):
    example = next(e for e in rules.examples() if e["name"] == name)
    return example["item"], [form.to_row(r, example["item"], MODEL) for r in example["output"]["rows"]]


def _pictured(row, size=512):
    row = copy.deepcopy(row)
    row["writer"] = {**(row.get("writer") or {}), "image_size": size}
    return row


def _text_row(row):
    row = copy.deepcopy(row)
    row["writer"] = {**(row.get("writer") or {}), "image_size": None}
    return row


# ------------------------------------------------------------------ A: the task text


def _writer_rules(profile):
    base = load_writer_rules(RulesConfig(package="rps.rules"))
    return profile_rules(base, profile)


@pytest.mark.parametrize("profile", PROFILES)
def test_a_call_with_a_picture_says_so_and_one_without_is_what_it_was(profile):
    item, _rows = _example("seeded_single")
    chosen = _writer_rules(profile)
    plain = render_task_text(item, ordinal=3, rules=chosen)
    assert render_task_text(item, ordinal=3, rules=chosen, picture=False) == plain
    pictured = render_task_text(item, ordinal=3, rules=chosen, picture=True)
    assert pictured.startswith(plain) and "The picture is attached." in pictured[len(plain):]
    assert pictured.count("The picture is attached.") == 1


def test_the_default_task_text_without_a_picture_is_pinned():
    item, _rows = _example("seeded_single")
    text = render_task_text(item, ordinal=0, rules=_writer_rules("default"))
    assert hashlib.sha256(text.encode()).hexdigest() == hashlib.sha256(rules.task_text.build_task_text(item, 0).encode()).hexdigest()
    assert PICTURE_LINE not in text
    assert render_task_text("a plain spec") == "a plain spec"
    assert render_task_text("a plain spec", picture=True) == "a plain spec\n\n" + PICTURE_LINE


def test_a_writer_call_carries_the_line_exactly_when_it_carries_the_picture(tmp_path):
    world = World(tmp_path, writer_class=RecordingWriter)
    controller = world.controller()
    controller.add_task(task_spec(("001",), batch="with", image="p.jpg"))
    controller.add_task(task_spec(("002",), batch="without"))
    world.drive(controller)
    calls = {call.line: call for call in controller.writers["fake-batch"].calls}
    assert calls["001"].image is not None and calls["001"].task_text.endswith(PICTURE_LINE)
    assert calls["002"].image is None and PICTURE_LINE not in calls["002"].task_text


# ------------------------------------------------------------------ B: the writer's rules


@pytest.mark.parametrize("profile", PROFILES)
def test_every_writer_text_holds_the_picture_block_and_the_new_class(profile):
    system = rules_compile.writer_system() if profile == "default" else rules_compile.profile_system(profile)
    committed = rules.writer_system() if profile == "default" else _writer_rules(profile).system
    assert system.replace("\r\n", "\n") == committed.replace("\r\n", "\n")
    assert "WHEN THE PICTURE IS ATTACHED" in system
    assert "picture_conflict" in system
    assert CLASSIFIER_LINE in system
    assert '("shot light", a value from the list)' in system
    assert "facts the picture shows plainly" in system


def test_the_class_is_in_the_schema_the_format_blocks_and_the_forms():
    assert "picture_conflict" in rules_compile.SKIP_CLASSES
    assert "picture_conflict" in rules.row_schema()["properties"]["class"]["enum"]
    assert "picture_conflict" in rules.json_format()
    gemini = _writer_rules("gemini")
    assert "picture_conflict" in str(gemini.openapi_schema)
    assert "picture_conflict" in _writer_rules("deepseek").json_format


def test_a_skip_with_the_new_class_is_accepted_where_classes_are_read():
    item, _rows = _example("seeded_single")
    text = '{"skip": true, "reason": "the record count is plainly another", "class": "picture_conflict", "rows": []}'
    assert '"class": "picture_conflict"' in form.normalize_output(text, item, MODEL)
    from rps.controller import parse_output

    _rows_out, skips, problem = parse_output(text)
    assert problem is None and skips[0]["class"] == "picture_conflict"
    assert "picture_conflict" in rules.row_schema()["properties"]["class"]["enum"]


# ------------------------------------------------------------------ the picture signal


def test_a_picture_row_is_a_row_whose_writer_label_holds_a_picture_size():
    assert is_picture_row({"writer": {"image_size": 512}})
    assert not any(is_picture_row(r) for r in ({}, {"writer": {}}, {"writer": {"image_size": None}}, {"writer": None}, {"writer": {"image_size": True}}))


# ------------------------------------------------------------------ C: the checker


def _small(spec=None, caption="", prompt="", thoughts=(), depth="medium"):
    return {
        "id": "art-1",
        "series": "s-1",
        "depth": depth,
        "spec": spec if spec is not None else {"person": {"gender": "girl"}},
        "thoughts": [{"step": s, "text": t} for s, t in thoughts],
        "wordings": {"caption": caption, "prompt": prompt, "tags": "", "critique": ""},
        "questions": [],
    }


def _medium():
    item, rows = _example("people_series")
    return dict(item, hint=HINT), copy.deepcopy(rows[1])


def _case_medium(mutate):
    item, row = _medium()
    mutate(row)
    return row, item


def _case_deep(mutate):
    item, rows = _example("people_series")
    row = copy.deepcopy(rows[2])
    mutate(row)
    return row, item


def _overhead(row):
    row["spec"] = {"shot": {"angle": "overhead"}, "person": {"gender": "girl"}}
    row["thoughts"] = [{"step": "see", "text": "She is looking upward at the kites."}]


def _shot(row):
    row["spec"]["shot"] = {}
    row["thoughts"][1]["text"] = "A tight crop of the sweater."


CASES = {
    "unrecorded_pose": lambda: _case_medium(lambda r: r["wordings"].update(caption="A woman kneeling in a cafe.")),
    "unrecorded_detail": lambda: _case_medium(lambda r: r["thoughts"][1].update(text="A small bench sits by the door.")),
    "unrecorded_shot": lambda: _case_medium(_shot),
    "unrecorded_light": lambda: (_small(prompt="a girl, soft lighting, warm low light"), None),
    "unrecorded_thing": lambda: _case_medium(lambda r: r["thoughts"][1].update(text="Green cushions line a small sofa near the rug.")),
    "unrecorded_size": lambda: (_small(caption="A narrow alley."), None),
    "unrecorded_material": lambda: (_small(caption="A brick alley."), None),
    "unrecorded_color": lambda: _case_deep(lambda r: r["thoughts"][5].update(text="Teal sits behind the cup in an orange glow.")),
    "unrecorded_garment": lambda: _case_deep(lambda r: r["thoughts"][1].update(text=r["thoughts"][1]["text"] + " A hat tops it.")),
    "state_conflict": lambda: (_small(spec={"shot": {"angle": "overhead"}, "person": {"gender": "girl"}}, thoughts=[("see", "She is looking upward at the kites.")]), None),
    "added_record": lambda: (
        _small(spec={"thing": {"name": "dog"}, "form": {"space": "vast"}}),
        {"kind": "single", "hint": "A dog asleep on a rug by the stove.", "spec": {"thing": {"name": "dog"}}},
    ),
    "relation_placement": lambda: (
        _small(spec={"person": {"gender": "boy"}, "relation": [{"a": "boy", "rel": "same colour as", "b": "lamp"}]}, thoughts=[("relate", "The lamp stands beside him.")]),
        None,
    ),
}


def test_the_demoted_codes_are_exactly_the_codes_the_cases_cover():
    assert set(CASES) == set(PICTURE_DETAIL_CODES)


@pytest.mark.parametrize("code", sorted(CASES))
def test_a_detail_no_record_gives_is_a_flag_on_a_picture_row_and_what_it_was_on_a_text_row(code):
    row, item = CASES[code]()
    text_problems = [p for p in assessor.check_row(_text_row(row), item) if p.code == code]
    assert text_problems, f"the case must raise {code} on a text row"
    # the text path: the code and its severity are what they were (and the same without any writer label)
    assert [p for p in assessor.check_row(row, item) if p.code == code] == text_problems
    picture_problems = [p for p in assessor.check_row(_pictured(row), item) if p.code == code]
    assert len(picture_problems) == len(text_problems)
    assert all(p.severity == FLAG and p.message.endswith("; look for it in the picture") for p in picture_problems)
    assert PICTURE_NOTE == "; look for it in the picture"
    # the row is not refused for it
    refused = {p.code for p in assessor.check_row(_pictured(row), item) if p.severity == ERROR}
    assert code not in refused


def test_a_text_row_is_checked_exactly_as_before_whatever_the_picture_signal_is():
    rows = []
    for example in rules.examples():
        item = example["item"]
        rows += [(form.to_row(r, item, MODEL), item) for r in example["output"]["rows"]]
    rows += [factory() for factory in CASES.values()]
    for row, item in rows:
        bare = assessor.check_row(row, item)
        assert assessor.check_row(_text_row(row), item) == bare
        picture = assessor.check_row(_pictured(row), item)
        # the picture row differs only in the demoted codes
        demoted = PICTURE_DETAIL_CODES | PICTURE_HINT_CODES
        assert [p for p in picture if p.code not in demoted] == [p for p in bare if p.code not in demoted]


# ------------------------------------------------------------------ the hint describes the same picture

HINT_CAPTION = (
    "A young man stands against a plain light gray background wearing a white shirt and striped shorts. "
    "He faces the camera with one hand in his pocket."
)
HINT_CASES = {
    "thought_hint_copy": lambda: _case_medium(lambda r: r["thoughts"][1].update(text="Cafe tables stand near the window with a lamp.")),
    "caption_hint_copy": lambda: (
        _small(spec={"person": {"gender": "man"}}, caption="A young man stands in a white shirt and striped shorts against a gray background."),
        {"kind": "single", "hint": HINT_CAPTION, "spec": {"person": {"gender": "man"}}},
    ),
    "caption_hint_list": lambda: (
        _small(spec={"thing": {"name": "dog"}}, caption="A dog, a cat and a horse stand together."),
        {"kind": "single", "hint": "The painting depicts a dog, a cat and a horse. They stand in a yard.", "spec": {"thing": {"name": "dog"}}},
    ),
}


def test_the_hint_codes_are_exactly_the_codes_the_cases_cover():
    assert set(HINT_CASES) == set(PICTURE_HINT_CODES)
    assert not PICTURE_HINT_CODES & PICTURE_DETAIL_CODES


@pytest.mark.parametrize("code", sorted(HINT_CASES))
def test_a_sentence_that_says_what_the_hint_says_is_a_flag_on_a_picture_row_and_refused_on_a_text_row(code):
    row, item = HINT_CASES[code]()
    text_problems = [p for p in assessor.check_row(_text_row(row), item) if p.code == code]
    assert text_problems and all(p.severity == ERROR for p in text_problems), f"the case must refuse a text row for {code}"
    # the text path is what it was, with or without a writer label
    assert [p for p in assessor.check_row(row, item) if p.code == code] == text_problems
    picture_problems = [p for p in assessor.check_row(_pictured(row), item) if p.code == code]
    assert len(picture_problems) == len(text_problems)
    assert all(p.severity == FLAG and p.message.endswith(PICTURE_HINT_NOTE) for p in picture_problems)
    assert code not in {p.code for p in assessor.check_row(_pictured(row), item) if p.severity == ERROR}


def test_a_picture_row_that_drops_a_given_record_is_still_refused():
    item, rows = _example("people_series")
    row = copy.deepcopy(rows[2])
    dropped = next(iter(item["specs"]["deep"]))
    del row["spec"][dropped]
    found = [p for p in assessor.check_row(_pictured(row), item) if p.code == "spec_kept"]
    assert found and all(p.severity == ERROR for p in found)


def _seeded():
    item, rows = _example("seeded_single")
    row = copy.deepcopy(rows[0])
    row["questions"] = [q if q["key"] != "shot light" else dict(q, key="scene time") for q in row["questions"]]
    return item, row


def test_a_record_added_from_the_picture_passes_and_a_value_off_the_list_is_refused():
    item, row = _seeded()
    row["spec"].setdefault("form", {})["space"] = "vast"
    assert any(p.code == "added_record" and p.severity == ERROR for p in assessor.check_row(_text_row(row), item))
    problems = assessor.check_row(_pictured(row), item)
    assert not [p for p in problems if p.severity == ERROR]
    assert any(p.code == "added_record" and p.severity == FLAG for p in problems)
    bad = copy.deepcopy(row)
    bad["spec"].setdefault("shot", {})["light"] = "plasma"
    refused = [p for p in assessor.check_row(_pictured(bad), item) if p.severity == ERROR]
    assert any(p.code == "rule_2" for p in refused)
    assert any(p.code == "rule_2" for p in assessor.check_row(_text_row(bad), item) if p.severity == ERROR)


def test_a_light_record_the_writer_added_and_a_light_thought_pass_on_a_picture_row():
    item, row = _seeded()
    row["spec"].setdefault("shot", {})["light"] = "soft window"
    row["thoughts"][1]["text"] += " Soft window light lies across the pane."
    assert not [p for p in assessor.check_row(_pictured(row), item) if p.severity == ERROR]
    # light with no record at all: a flag for the picture row (the assessor's verdict passes it), an error as before for a text row
    row, none = CASES["unrecorded_light"]()
    assert any(p.code == "unrecorded_light" and p.severity == ERROR for p in assessor.check_row(_text_row(row), none))
    context = type("Context", (), {"spec": none, "assignment": None})()
    result = assessor.Assessor()(_pictured(row), context)
    assert "unrecorded_light" in result.flags and not any(e.startswith("unrecorded_light") for e in result.errors)
    assert any(e.startswith("unrecorded_light") for e in assessor.Assessor()(_text_row(row), context).errors)


# ------------------------------------------------------------------ D: the reviewers' brief and the pool


def test_the_review_brief_holds_the_picture_sections():
    brief = rules.review_brief()
    assert "## Rows with a picture" in brief
    for phrase in ("image_withheld", "`image` field", "packet.json", "`invented`", "`contradicts_picture`", "`added_record`", "Check the light."):
        assert phrase in brief
    assert "A classifier's lighting hint" in brief
    assert "`unrecorded_...`" in brief
    # the hint describes the same picture: agreement with it is not a copy
    assert "The hint of such a row describes the same picture" in brief and "That is not `hint_copy`." in brief
    for flag in ("`thought_hint_copy`", "`caption_hint_copy`", "`caption_hint_list`"):
        assert brief.count(flag) >= 2  # in the picture section and in the list of flags


def test_the_pool_has_the_picture_rules_and_closes_with_version_two():
    pool = rules.pool()
    assert "## L. Rows written with the picture attached" in pool
    assert "Version 2 covers rows written with the picture attached" in pool
    assert "Version 1 covers text items" not in pool
    assert "classifier" in pool and "picture_conflict" in pool
    numbers = [int(m.group(1)) for m in re.finditer(r"^\| (\d+) \|", pool, flags=re.M)]
    assert numbers == list(range(1, len(numbers) + 1)) and numbers[-1] >= 100
