"""Checks for the faults a reviewer finds past the first checks: each fires on a broken copy of a clean row and stays quiet on the clean one."""

from __future__ import annotations

import copy

import pytest

from rps import assessor, rules
from rps.assessor import review_checks as rc
from rps.assessor.problems import ERROR, FLAG
from rps.rules import form

MODEL = "test-model"
HINT = (
    "A tidy cafe seen from the door. A cafe with a window and a wooden counter. "
    "Cafe tables near the window with a lamp. A woman at a cafe table in the morning. "
    "The morning light reaches the tables."
)


def _example(name):
    example = next(e for e in rules.examples() if e["name"] == name)
    return example["item"], [form.to_row(r, example["item"], MODEL) for r in example["output"]["rows"]]


@pytest.fixture
def medium():
    item, rows = _example("people_series")
    return dict(item, hint=HINT), copy.deepcopy(rows[1])


@pytest.fixture
def deep():
    item, rows = _example("people_series")
    return item, copy.deepcopy(rows[2])


def found(row, item, code, severity=None):
    problems = [p for check in rc.REVIEW_CHECKS for p in check(row, item) if p.code == code]
    return [p for p in problems if severity is None or p.severity == severity]


def test_the_clean_examples_raise_no_review_error():
    for example in rules.examples():
        item = example["item"]
        for r in example["output"]["rows"]:
            row = form.to_row(r, item, MODEL)
            errors = [p for check in rc.REVIEW_CHECKS for p in check(row, item) if p.severity == ERROR]
            assert not errors, (example["name"], errors)


@pytest.mark.parametrize(
    "text",
    [
        "The only recorded garment is a sweater.",
        "That period is named here and nobody is guessed at.",
        "Beside the window: the lamp count is 1.",
        "A lone kettle with a count of 1 settles the shelf.",
        "The cuff is specified by the setting.",
        "The kettle is the loudest named subject.",
    ],
)
def test_self_talk_is_an_error(medium, text):
    item, row = medium
    row["thoughts"][0]["text"] = text
    assert found(row, item, "self_talk", ERROR)


def test_a_ship_named_is_legal(medium):
    item, row = medium
    row["thoughts"][0]["text"] = "A ferry named for the harbor crosses behind her."
    assert not found(row, item, "self_talk")


def test_self_hedge_and_repeats_are_flags(medium):
    item, row = medium
    row["thoughts"][1]["text"] = "This reading is an interpretation, and no claim follows about the cafe."
    assert found(row, item, "self_hedge", FLAG)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["thoughts"].append({"step": "check", "text": row["thoughts"][1]["text"]})
    assert found(row, item, "thought_repeat", FLAG)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["thoughts"][0]["text"] = "Cozy from the first glance."
    assert found(row, item, "mood_repeat", FLAG)


def test_invented_details_are_flags(medium):
    item, row = medium
    row["thoughts"][1]["text"] = "Green cushions line a small sofa near the rug."
    assert found(row, item, "unrecorded_thing", FLAG)
    assert not found(row, item, "unrecorded_thing", ERROR)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["thoughts"][1]["text"] = "Hard shadows fall across the floor."
    assert found(row, item, "unrecorded_light", FLAG)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["thoughts"][1]["text"] = "A small bench sits by the door."
    assert found(row, item, "unrecorded_detail", FLAG)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["spec"]["shot"] = {}
    row["thoughts"][1]["text"] = "A tight crop of the sweater."
    assert found(row, item, "unrecorded_shot", FLAG)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["wordings"]["caption"] = "A woman kneeling in a cafe."
    assert found(row, item, "unrecorded_pose", FLAG)


def test_the_hint_grounds_the_details(medium):
    item, row = medium
    row["thoughts"][1]["text"] = "The wooden counter and the lamp frame the tables."
    assert not found(row, item, "unrecorded_thing")


def test_mood_checks(deep):
    item, row = deep
    row["spec"]["mood"] = {"feeling": "cozy", "from": "thing count"}
    assert found(row, item, "mood_weak_evidence", ERROR)
    item, row = deep[0], copy.deepcopy(deep[1])
    row["spec"]["mood"] = {"feeling": "cozy", "from": "thing colour"}
    for t in row["thoughts"]:
        t["text"] = t["text"].replace("cozy", "calm")
        if t["step"] in ("mood", "mean"):
            t["text"] = "Warm paint lets the nook feel cozy."
    assert found(row, item, "mood_unhedged", FLAG)
    item, row = deep[0], copy.deepcopy(deep[1])
    row["spec"]["mood"] = {"feeling": "cozy", "from": "scene time"}
    for t in row["thoughts"]:
        if t["step"] in ("mood", "mean"):
            t["text"] = "The cloudy sky keeps the cafe cozy."
    assert found(row, item, "mood_cause_differs", FLAG)


def test_required_records(medium, deep):
    item, row = medium
    del row["spec"]["mood"]
    assert found(row, item, "required_record", ERROR)
    item, row = deep
    row["spec"]["story"] = {"moment": "a pause"}
    assert found(row, item, "story_incomplete", ERROR)
    row["spec"].pop("intent")
    assert found(row, item, "required_record", ERROR)


def test_question_keys(deep):
    item, row = deep
    chain = next(q for q in row["questions"] if q["needs"] == "chain")
    chain["key"] = "eye path"
    assert found(row, item, "question_key", ERROR)
    chain["key"] = "mood temperature"
    chain["value"] = "a long phrase that wanders over the sleeve and beyond"
    assert found(row, item, "question_value", ERROR)


def test_a_year_against_the_period_sets_the_item_aside():
    item, rows = _example("painting_deep")
    item = dict(item, hint="Oil on linen, dated 1871, showing a ferry at a pier.")
    row = copy.deepcopy(rows[0])
    row["spec"]["form"] = {"medium": "oil painting", "period": "baroque"}
    assert found(row, item, "hint_contradicts_record", ERROR)
    result = assessor.Assessor()(row, _context(item))
    assert not result.passed and result.set_aside["class"] == "contradiction"
    row["spec"]["form"]["period"] = "19th century"
    assert not found(row, item, "hint_contradicts_record")


def _context(item):
    from rps.assess import AssessContext

    return AssessContext(task_id="t0123456789abcdef", source="s", batch="b", line="l1", attempt=1, route="r", pool_version="v", spec=item, item_id=None)


def test_a_frame_position_is_not_a_place():
    item, rows = _example("seeded_single")
    row = copy.deepcopy(rows[0])
    row["spec"]["thing"]["position"] = "centre"
    row["thoughts"][0]["text"] = "The beetle rests near the centre of a river of light."
    assert found(row, item, "position_as_place", FLAG)
    row["thoughts"][0]["text"] = "The beetle rests near the centre of the frame."
    assert not found(row, item, "position_as_place")


def test_hint_restatement_is_a_flag(medium):
    item, row = medium
    item = dict(item, hint=HINT + " A woman sits at a cafe table in the morning with a lamp near the window.")
    row["thoughts"][1] = {"step": "place", "text": "A woman at a cafe table in the morning with a lamp near the window, sits there."}
    assert found(row, item, "hint_paraphrase", FLAG)
    row["thoughts"][1] = {"step": "place", "text": "The tables crowd the glass, so her mustard sweater is the one warm block in the room."}
    assert not found(row, item, "hint_paraphrase")


def test_one_caption_is_not_agreement(medium):
    item, row = medium
    item = dict(item, hint=" ".join(["A cafe room with a window.", "A cafe interior with a window.", "A room in a cafe near a window.", "A cafe room with a mirror."]))
    row["thoughts"][1]["text"] = "A mirror hangs beside the window."
    assert found(row, item, "single_caption_fact", FLAG)


@pytest.mark.parametrize(
    "step, text, code, severity",
    [
        ("count", "The woman is larger than the table.", "step_count", ERROR),
        ("compare", "A mustard sweater over a navy skirt.", "step_compare", FLAG),
        ("relate", "A mustard sweater.", "step_relate", FLAG),
        ("change", "The mustard sweater stays bright.", "step_change", FLAG),
    ],
)
def test_step_labels(medium, step, text, code, severity):
    item, row = medium
    row["thoughts"][2] = {"step": step, "text": text}
    assert found(row, item, code, severity)


def test_step_labels_stay_quiet_when_the_step_does_its_job(medium):
    item, row = medium
    row["thoughts"][2] = {"step": "count", "text": "One woman and two garments."}
    row["thoughts"][3] = {"step": "compare", "text": "Were the skirt mustard instead, the sweater would lose its edge."}
    assert not found(row, item, "step_count") and not found(row, item, "step_compare")


def test_wording_checks(medium):
    item, row = medium
    row["wordings"]["tags"] = row["wordings"]["tags"] + ", maroon bag"
    assert found(row, item, "tag_color", ERROR)
    row["wordings"]["tags"] = "1girl, golden retriever"
    assert not found(row, item, "tag_color")
    item, row = medium[0], copy.deepcopy(medium[1])
    row["spec"]["form"] = {"compose": "left third"}
    row["wordings"]["tags"] = "1girl, left third, cafe"
    assert found(row, item, "tag_value", FLAG)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["wordings"]["caption"] = "A woman in a cafe in the morning."
    assert found(row, item, "caption_garment", ERROR)
    item, row = medium[0], copy.deepcopy(medium[1])
    row["wordings"]["caption"] = "A woman sits in a Baroque cafe."
    assert found(row, item, "style_on_scene", FLAG)


def test_a_caption_that_lists_the_hint(medium):
    item, row = medium
    item = dict(item, hint="Gouache work. It depicts a birch, a barn, a moon and a meadow.")
    row["wordings"]["caption"] = "A birch, barn, and moon fill a meadow."
    assert found(row, item, "caption_hint_list", FLAG)


def test_the_assessor_reports_the_new_errors_as_lines(medium):
    item, row = medium
    row["thoughts"][0]["text"] = "The only recorded garment is a sweater."
    result = assessor.Assessor()(row, _context(item))
    assert not result.passed and any(line.startswith("self_talk: ") for line in result.errors)
