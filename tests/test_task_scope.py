"""The task text says what each row may not use: the facts only a deeper row of a drawn series records, the details
the item records nowhere, and what an unknown question's key covers. The writer's template states the rules behind
those lines."""

import re

from rps import rules
from rps.rules import balance, task_text


def drawn_series():
    simple = {"person": {"gender": "woman"}, "scene": {"setting": "harbour"}}
    medium = {"person": {"gender": "woman", "count": "1"}, "scene": {"setting": "harbour", "time": "dusk"}, "shot": {"framing": "waist up"}}
    deep = {
        "person": {"gender": "woman", "count": "1"},
        "body": {"pose": "leaning"},
        "scene": {"setting": "harbour", "time": "dusk", "weather": "fog"},
        "shot": {"framing": "waist up", "light": "lantern"},
        "thing": {"name": "boat", "colour": "red"},
        "relation": [{"a": "woman", "rel": "beside", "b": "boat"}],
    }
    return {
        "kind": "series",
        "series": "s-900",
        "ids": ["art-900100", "art-900101", "art-900102"],
        "domain": "photograph",
        "genre": None,
        "specs": {"simple": simple, "medium": medium, "deep": deep},
        "seed_id": None,
        "license": "Apache-2.0",
    }


def seeded_single():
    return {
        "kind": "single",
        "series": "s-901",
        "ids": ["art-900200"],
        "depth": "medium",
        "domain": "photograph",
        "genre": "still life",
        "spec": {"thing": {"name": "lamp", "colour": "green"}, "scene": {"setting": "library"}},
        "seed_id": "docci:test_99999",
        "hint": "A green lamp on a reading desk.",
        "license": "CC-BY-4.0",
    }


def scope_line(text, depth):
    return next(line for line in text.splitlines() if line.startswith(f"- {depth} (rank"))


def test_a_shallower_row_is_told_the_facts_only_deeper_rows_record():
    text = task_text.build_task_text(drawn_series(), 0)
    simple, medium = scope_line(text, "simple"), scope_line(text, "medium")
    for fact in ("scene time = dusk", "shot framing = waist up", "shot light = lantern", "woman beside boat"):
        assert fact in simple
    assert "scene time = dusk" not in medium and "shot light = lantern" in medium and "scene weather = fog" in medium
    assert "count = 1" not in text.split("Each row writes only from its own records:")[1]
    assert "- deep (rank" not in text


def test_the_details_the_item_records_nowhere_are_named():
    text = task_text.build_task_text(drawn_series(), 0)
    line = next(line for line in text.splitlines() if line.startswith("Recorded nowhere in this item"))
    assert "materials and surfaces" in line and "anything held or carried" in line and line.endswith("sound.")
    assert "a pose or motion (standing" not in line  # the deep rank records a pose
    assert "the weather" not in line and "the light (" not in line and "the time of day" not in line
    assert "a facial expression" in line  # a person with no expression record


def test_a_holding_relation_lifts_the_held_thing_line():
    item = drawn_series()
    item["specs"]["deep"]["relation"] = [{"a": "woman", "rel": "holding", "b": "rope"}]
    text = task_text.build_task_text(item, 0)
    assert "anything held or carried" not in text


def test_a_seeded_item_may_take_a_detail_its_hint_states():
    text = task_text.build_task_text(seeded_single(), 0)
    line = next(line for line in text.splitlines() if line.startswith("Not in the records"))
    assert "only where the hint states it plainly" in line and "neither the records nor the hint gives" in line
    assert "a facial expression" not in line and "a pose or motion (standing" not in line  # no person in the records
    assert "a posture or motion of the thing" in line
    assert "Each row writes only from its own records" not in text  # a single has no ranks


def test_a_seeded_series_is_told_to_cut_its_rows_and_keep_to_them():
    item = drawn_series()
    item["seed_id"], item["hint"] = "docci:test_99998", "A woman beside a red boat at a foggy harbour."
    item["specs"] = {"deep": item["specs"]["deep"]}
    text = task_text.build_task_text(item, 0)
    assert "cut the simple and medium rows from the deep records" in text and "must not use:" not in text


def test_every_unknown_key_says_what_it_covers_and_the_line_still_parses():
    assert set(task_text.UNKNOWN_COVERS) == set(balance.UNKNOWN_KEYS)
    text = task_text.build_task_text(drawn_series(), 3)
    line = next(line for line in text.splitlines() if "Unknown question key by depth:" in line)
    keys = dict(re.findall(r"(simple|medium|deep) = '([^']+)'", line))
    assert set(keys) == {"medium", "deep"}
    for key in keys.values():
        assert task_text.UNKNOWN_COVERS[key] in line
    assert "no thought, wording or tag answers them" in line


def test_the_template_leads_with_the_facts_and_ends_with_the_check():
    text = rules.writer_system()
    assert text.index("THE FIRST RULE: ONLY THE FACTS") < text.index("RANKS") < text.index("THE RECORDS YOU ADD") < text.index("RECORDS\n")
    assert text.index("THE LISTS") < text.index("BEFORE YOU ANSWER")
    for phrase in (
        "Each row of a series knows only its own records",
        "The framing limits what can show",
        "They are about the picture, never about what the people or things in it do",
        "never a moment word (instant before, instant after)",
        "in that sentence name its source",
        "Directions belong to the frame, never to a person",
        "never walk through its sentences in their order",
        "a person tag whenever a person is recorded",
    ):
        assert phrase in text, phrase
