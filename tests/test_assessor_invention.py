"""Checks for invented facts, rank scope, tags, slots and record talk: each fires on a small invented row and stays quiet on its clean twin."""

from __future__ import annotations

import copy

import pytest

from rps import assessor
from rps.assessor import invention_checks as ic
from rps.assessor import reading_checks as rd
from rps.assessor.problems import ERROR, FLAG
from rps.assessor.seed_checks import screen_item


def _row(depth="medium", spec=None, thoughts=(), caption="", prompt="", tags="", critique="", questions=()):
    return {
        "id": "art-1",
        "series": "s-1",
        "depth": depth,
        "spec": spec if spec is not None else {"person": {"gender": "girl"}},
        "thoughts": [{"step": s, "text": t} for s, t in thoughts],
        "wordings": {"caption": caption, "prompt": prompt, "tags": tags, "critique": critique},
        "questions": list(questions),
    }


def _series(simple=None, medium=None, deep=None):
    return {"kind": "series", "specs": {"simple": simple or {}, "medium": medium or {}, "deep": deep or {}}}


def codes(problems):
    return {(p.code, p.severity) for p in problems}


# ------------------------------------------------------------------ poses

def test_a_default_pose_is_an_error_for_a_person_and_an_animal():
    for text in ("A girl stands in the alley.", "A heron standing on one leg in shallow reeds.", "She sits by the window."):
        assert ("unrecorded_pose", ERROR) in codes(assessor.check_row(_row(caption=text)))
    assert ("unrecorded_pose", ERROR) in codes(assessor.check_row(_row(thoughts=[("see", "The girl stands quietly.")])))


def test_a_pose_the_record_or_hint_gives_is_fine():
    row = _row(spec={"person": {"gender": "girl"}, "body": {"pose": "standing"}}, caption="A girl stands in the alley.")
    assert not [p for p in assessor.check_row(row) if p.code in ("unrecorded_pose", "state_conflict")]
    item = {"kind": "single", "hint": "A pigeon sitting on a rock.", "spec": {}}
    assert not [p for p in assessor.check_row(_row(caption="A pigeon perches on a rock."), item) if p.code.endswith("pose")]


def test_objects_and_idioms_do_not_take_a_pose():
    for text in ("A lamp stands beside the fence.", "The tall chimney stands out over the slate roofs.", "Green leaves stand around the gate."):
        assert not [p for p in assessor.check_row(_row(caption=text)) if p.code in ("unrecorded_pose", "state_conflict")]


def test_a_pose_that_conflicts_with_another_rank_is_an_error():
    item = _series(deep={"person": {"gender": "girl"}, "body": {"pose": "climbing"}})
    problems = assessor.check_row(_row(caption="A girl sitting on a roof."), item)
    assert ("state_conflict", ERROR) in codes(problems) or ("unrecorded_pose", ERROR) in codes(problems)


def test_looking_up_contradicts_an_overhead_shot():
    row = _row(spec={"shot": {"angle": "overhead"}, "person": {"gender": "girl"}}, thoughts=[("see", "She is looking upward at the kites.")])
    assert ("state_conflict", ERROR) in codes(assessor.check_row(row))


# ------------------------------------------------------------------ rank scope

def test_a_rank_may_not_use_a_fact_only_a_deeper_rank_records():
    item = _series(medium={"person": {"gender": "girl"}}, deep={"person": {"gender": "girl"}, "scene": {"weather": "overcast"}})
    row = _row(thoughts=[("see", "A girl under an overcast sky.")])
    assert ("rank_scope", ERROR) in codes(assessor.check_row(row, item))
    deep = _row(depth="deep", spec={"person": {"gender": "girl"}, "scene": {"weather": "overcast"}}, thoughts=[("see", "A girl under an overcast sky.")])
    assert "rank_scope" not in {c for c, _ in codes(assessor.check_row(deep, item))}


# ------------------------------------------------------------------ light, size, material, placement

def test_light_the_records_do_not_give_is_an_error():
    row = _row(prompt="a girl, soft lighting, warm low light")
    assert ("unrecorded_light", ERROR) in codes(assessor.check_row(row))
    assert "unrecorded_light" not in {c for c, _ in codes(assessor.check_row(_row(spec={"shot": {"light": "candle"}}, prompt="a girl, soft lighting")))}


def test_size_and_material_words_are_flags():
    found = codes(assessor.check_row(_row(caption="A narrow brick alley.")))
    assert ("unrecorded_size", FLAG) in found and ("unrecorded_material", FLAG) in found
    assert not {"unrecorded_size", "unrecorded_material"} & {c for c, _ in codes(assessor.check_row(_row(spec={"scene": {"setting": "brick alley"}}, caption="A brick alley.")))}


def test_a_color_only_relation_does_not_place_the_thing():
    spec = {"person": {"gender": "boy"}, "relation": [{"a": "boy", "rel": "same colour as", "b": "lamp"}]}
    assert ("relation_placement", ERROR) in codes(assessor.check_row(_row(spec=spec, thoughts=[("relate", "The lamp stands beside him.")])))
    ok = {"person": {"gender": "boy"}, "relation": [{"a": "lamp", "rel": "beside", "b": "boy"}]}
    assert "relation_placement" not in {c for c, _ in codes(assessor.check_row(_row(spec=ok, thoughts=[("relate", "The lamp stands beside him.")])))}


# ------------------------------------------------------------------ the hint

def test_a_record_the_hint_does_not_support_is_an_error():
    item = {"kind": "single", "hint": "A dog asleep on a rug by the stove.", "spec": {"thing": {"name": "dog"}}}
    row = _row(spec={"thing": {"name": "dog"}, "form": {"space": "vast"}})
    assert ("added_record", ERROR) in codes(assessor.check_row(row, item))
    weather = _row(spec={"thing": {"name": "dog"}, "scene": {"weather": "clear"}})
    cloudy = dict(item, hint="A dog on a jetty under a cloudy sky.")
    assert ("added_record", ERROR) in codes(assessor.check_row(weather, cloudy))


def test_a_name_taken_from_the_hint_is_an_error():
    item = {"kind": "single", "hint": "A banded goby fish in a tank.", "spec": {"thing": {"name": "fish"}}}
    row = _row(spec={"thing": {"name": "fish"}}, caption="A banded fish hovers in a tank.")
    assert ("hint_name", ERROR) in codes(assessor.check_row(row, item))


def test_a_caption_that_restates_the_hints_first_sentence_is_an_error():
    item = {"kind": "single", "hint": "A green door with a brass knocker set in its upper panel. Two narrow windows flank the door.", "spec": {}}
    row = _row(caption="A green door with a brass knocker set in its upper panel.")
    assert ("caption_hint_copy", ERROR) in codes(assessor.check_row(row, item))


def test_a_garbled_seed_is_screened():
    item = {"kind": "single", "hint": "A round metal plate bolted to a post. The metal has gone dull.", "spec": {"thing": {"name": "medal"}}}
    assert screen_item(item)
    assert screen_item(dict(item, spec={"thing": {"name": "plate"}})) is None
    assert screen_item({"kind": "single", "spec": {"thing": {"name": "medal"}}}) is None


# ------------------------------------------------------------------ moods and minors

def test_a_mood_from_a_framing_or_an_angle_is_an_error():
    framing = _row(spec={"mood": {"feeling": "gentle", "from": "shot framing"}}, thoughts=[("mood", "The tight framing suggests a gentle mood.")])
    assert ("mood_weak_evidence", ERROR) in codes(assessor.check_row(framing))
    angle = _row(spec={"mood": {"feeling": "gentle", "from": "shot angle"}}, thoughts=[("mood", "The overhead view suggests a gentle mood.")])
    assert ("mood_weak_evidence", ERROR) in codes(assessor.check_row(angle))
    assert "mood_weak_source" not in {code for code, _ in codes(assessor.check_row(angle))}


@pytest.mark.parametrize("source", ["rel1 rel", "rel2 b", "rel4 rel", "rel3 a", "shot angle"])
def test_a_mood_from_a_relation_key_or_an_angle_is_one_error_that_says_what_to_do(source):
    row = _row(spec={"mood": {"feeling": "gentle", "from": source}}, thoughts=[("mood", "The pair suggests a gentle mood.")])
    found = [p for p in assessor.check_row(row) if p.code in ("mood_weak_evidence", "mood_weak_source")]
    assert [(p.code, p.severity) for p in found] == [("mood_weak_evidence", ERROR)]
    assert repr(source) in found[0].message
    for record in ("look expression", "body pose", "thing state", "shot light", "scene time", "scene weather", "scene setting", "form palette"):
        assert record in found[0].message


@pytest.mark.parametrize("source", ["look expression", "thing state", "scene setting", "shot light"])
def test_a_mood_from_a_record_that_can_carry_a_feeling_is_not_weak(source):
    row = _row(spec={"mood": {"feeling": "gentle", "from": source}}, thoughts=[("mood", "The scene suggests a gentle mood.")])
    assert not {p.code for p in assessor.check_row(row)} & {"mood_weak_evidence", "mood_weak_source"}


def test_makes_it_is_a_flat_verb():
    row = _row(spec={"mood": {"feeling": "wistful", "from": "scene time"}}, thoughts=[("mood", "The dusk over the harbor makes it wistful.")])
    assert ("mood_flat", ERROR) in codes(assessor.check_row(row))


@pytest.mark.parametrize("text", ["A loose shirt over his shoulders.", "Delicate features and a sense of vulnerability.", "A quiet intimacy in the frame."])
def test_appraisal_and_fit_words_about_a_minor_are_errors(text):
    row = _row(spec={"person": {"gender": "girl", "age": "child"}}, caption=text)
    assert ("minor_fit", ERROR) in codes(assessor.check_row(row))
    assert "minor_fit" not in {c for c, _ in codes(assessor.check_row(_row(spec={"person": {"gender": "woman", "age": "30s"}}, caption=text)))}


# ------------------------------------------------------------------ tags

def test_a_recorded_color_stays_with_its_thing_in_the_tags():
    spec = {"thing": {"name": "sedan", "colour": "dark blue"}}
    assert ("tag_color_missing", ERROR) in codes(assessor.check_row(_row(spec=spec, tags="car, street, night")))
    assert ("tag_color_detached", ERROR) in codes(assessor.check_row(_row(spec=spec, tags="dark blue, car, street")))
    assert not {"tag_color_missing", "tag_color_detached"} & {c for c, _ in codes(assessor.check_row(_row(spec=spec, tags="navy car, street")))}


def test_a_person_record_yields_a_person_tag_and_solo_needs_a_person():
    assert ("tag_person", ERROR) in codes(assessor.check_row(_row(tags="alley, braided hair")))
    assert "tag_person" not in {c for c, _ in codes(assessor.check_row(_row(tags="1girl, solo, alley")))}
    assert ("tag_solo", ERROR) in codes(assessor.check_row(_row(spec={"thing": {"name": "cat"}}, tags="cat, solo, couch")))


# ------------------------------------------------------------------ slots

def test_story_slots_hold_events_and_a_cause_is_not_a_time():
    spec = {"story": {"moment": "a pause", "after": "instant after", "cause": "afternoon"}}
    found = {c for c, _ in codes(assessor.check_row(_row(spec=spec)))}
    assert {"story_slot", "story_cause"} <= found
    clean = {"story": {"moment": "a pause", "after": "she steps down", "cause": "a letter arrived"}}
    assert not {"story_slot", "story_cause"} & {c for c, _ in codes(assessor.check_row(_row(spec=clean)))}


def test_a_chain_answer_is_a_value_of_its_keys_kind():
    bad = _row(questions=[{"key": "look eyes", "value": "low light", "needs": "chain"}, {"key": "shot lens", "value": "lens", "needs": "chain"}])
    assert ("chain_kind", ERROR) in codes(assessor.check_row(bad))
    good = _row(questions=[{"key": "look eyes", "value": "green", "needs": "chain"}])
    assert "chain_kind" not in {c for c, _ in codes(assessor.check_row(good))}


def test_an_unknown_keys_answer_may_not_appear_in_the_row():
    row = _row(thoughts=[("place", "A boy in an empty park.")], questions=[{"key": "scene layout", "value": "unknown", "needs": "unknown"}])
    assert ("unknown_stated", ERROR) in codes(assessor.check_row(row))


# ------------------------------------------------------------------ record talk and stock openings

@pytest.mark.parametrize("text", ["The dusk scene time reads cool.", "Its shot light falls from one bare bulb.", "That relation holds the pair together.", "These relations tie it together."])
def test_record_talk_is_an_error(text):
    assert ("spec_talk", ERROR) in codes(assessor.check_row(_row(thoughts=[("see", text)])))


@pytest.mark.parametrize("text", ["Check the olive: it is darker than the jacket.", "Compare the cup with the wall: the cup is brighter.", "Every element from the lamp to the door sits low."])
def test_stock_openings_are_errors(text):
    assert ("stock_opening", ERROR) in codes(assessor.check_row(_row(thoughts=[("check", text)])))


def test_a_check_that_repeats_an_earlier_thought_is_an_error():
    thoughts = [
        ("colour", "The mint top and the tan trousers set a cool color against a warm wall in the evening."),
        ("check", "Mint top and tan trousers set a cool color against a warm wall in the evening."),
    ]
    assert ("check_repeat", ERROR) in codes(assessor.check_row(_row(thoughts=thoughts)))


def test_the_new_error_codes_are_wired_into_check_row():
    row = copy.deepcopy(_row(caption="A girl stands."))
    assert any(p.code == "unrecorded_pose" for p in assessor.check_row(row))
    assert ic.INVENTION_CHECKS and rd.READING_CHECKS


# ------------------------------------------------------------------ the subject screen

def test_a_thing_the_hint_never_shows_is_set_aside():
    harbor = "A quiet harbor at first light. Two skiffs rest against the pier and gulls sit along the rail."
    assert screen_item({"kind": "single", "hint": harbor, "spec": {"thing": {"name": "cat"}}})
    assert screen_item({"kind": "single", "hint": harbor, "spec": {"thing": {"name": "skiff"}}}) is None
    assert screen_item({"kind": "single", "hint": harbor, "spec": {"thing": {"name": "stone bench"}}})


def test_a_listed_synonym_or_a_metadata_hint_is_not_set_aside():
    room = "A room with a green sofa under the window and a rug across the floor."
    assert screen_item({"kind": "single", "hint": room, "spec": {"thing": {"name": "couch"}}}) is None
    assert screen_item({"kind": "single", "hint": "A pilot waves beside a small plane on the apron.", "spec": {"thing": {"name": "airplane"}}}) is None
    assert screen_item({"kind": "single", "hint": "Medium: tin. Subjects: portrait.", "spec": {"thing": {"name": "coin"}}}) is None
    assert screen_item({"kind": "single", "hint": "Medium: tin. Subjects: portrait.", "spec": {"thing": {"name": "coin"}}, "extra": 1}) is None


# ------------------------------------------------------------------ walking the hint, caption form, pasted values

HINT_FOUR = "A red kayak lies on a gravel bank. Two paddles are crossed over its hull. A heron stands in the shallows beyond it. Morning mist hangs over the far water."


def test_a_thought_that_repeats_a_hint_sentence_is_an_error():
    item = {"kind": "single", "hint": HINT_FOUR, "spec": {}}
    copy_ = _row(thoughts=[("see", "A red kayak lies on a gravel bank.")])
    assert ("thought_hint_copy", ERROR) in codes(assessor.check_row(copy_, item))
    own = _row(thoughts=[("see", "The kayak is the one saturated color against the pale stones.")])
    assert "thought_hint_copy" not in {c for c, _ in codes(assessor.check_row(own, item))}


def test_a_caption_that_compresses_a_one_sentence_hint_is_a_flag():
    item = {"kind": "single", "hint": "A heron stands in the shallows beside a red kayak.", "spec": {}}
    row = _row(caption="A heron stands in the shallows beside a red kayak.")
    assert ("caption_compresses_hint", FLAG) in codes(assessor.check_row(row, item))


def test_a_caption_without_a_verb_is_a_flag():
    bare = _row(caption="A tall lighthouse on a rocky point, a gull overhead, in evening haze.")
    assert ("caption_no_verb", FLAG) in codes(assessor.check_row(bare))
    for text in ("A lighthouse stands on a rocky point with a gull overhead.", "Two kayaks rest on the bank and a heron wades beyond them."):
        assert "caption_no_verb" not in {c for c, _ in codes(assessor.check_row(_row(caption=text)))}


def test_a_record_value_pasted_into_prose_is_an_error():
    for text in ("Exactly 1 girl stands on the dock.", "One child boy waits by the gate."):
        assert ("pasted_value", ERROR) in codes(assessor.check_row(_row(thoughts=[("count", text)])))
    assert "pasted_value" not in {c for c, _ in codes(assessor.check_row(_row(thoughts=[("count", "One girl waits on the dock.")])))}
