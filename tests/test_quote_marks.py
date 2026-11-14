"""Shown text in straight marks, with the sentence's punctuation outside them (0.6.2).

Every writer prompt tells a writer with the picture to quote in plain straight double quotes and to keep its own
sentence's comma or period outside them. On a picture call the controller makes the curly marks of a row's thoughts,
caption, prompt and critique straight before it stores the row (`shown_text.straighten`); the tags are never touched
and a text row keeps its characters. The checker refuses a quoted text of a picture row whose last character is a
comma (`quote_comma`); a period there is not refused, since shown text can end with one. Every row here is invented
or one of the pool's worked examples.
"""

from __future__ import annotations

import copy
import json

import pytest

from rps import assessor
from rps.assessor import shown_text
from rps.assessor.problems import ERROR
from rps.providers import FakeOutcome

from harness import World, task_spec
from test_shown_text import PROMPTS, QUOTE_MARKS_RULE, TEMPLATES, _medium, _pictured

CURLY = "“", "”", "‘", "’"


def _with(row, *, say=None, caption=None, prompt=None):
    """The row with a say thought, and its caption and prompt set, where given."""
    row = copy.deepcopy(row)
    if say is not None:
        row["thoughts"].append({"step": "say", "text": say})
    if caption is not None:
        row["wordings"]["caption"] = caption
    if prompt is not None:
        row["wordings"]["prompt"] = prompt
    return row


def codes(row, item):
    return {(p.code, p.severity) for p in assessor.check_row(row, item)}


# ---------------------------------------------------------------------------- A: the writer's rule


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_every_writer_prompt_asks_for_straight_marks_and_the_sentences_punctuation_outside(name):
    for path in (PROMPTS[name], TEMPLATES[name]):
        text = path.read_text(encoding="utf-8")
        assert text.count(QUOTE_MARKS_RULE) == 1, path
        assert text.index('with " / " between lines. ' + QUOTE_MARKS_RULE) < text.index("The caption quotes those words")


def test_the_pool_registers_the_rules_and_the_code():
    from rps import rules

    pool = rules.pool()
    lines = {number: next(line for line in pool.splitlines() if line.startswith(f"| {number} |")) for number in (117, 118)}
    assert "| WRITER |" in lines[117] and "quote_comma" in lines[117]
    assert "| ASSESSOR |" in lines[118] and "quote_comma" in lines[118]


# ---------------------------------------------------------------------------- B1: curly marks made straight


def test_straighten_makes_the_marks_of_thoughts_caption_prompt_and_critique_straight_and_leaves_the_tags():
    row = {
        "thoughts": [{"step": "say", "text": "A sign reads “STOP” and a tag says ‘SALE’; it’s red."}, {"step": "place", "text": "No marks."}],
        "wordings": {
            "caption": "A red sign that reads “STOP”.",
            "prompt": "red sign, “STOP”",
            "critique": "The ‘STOP’ sign is sharp.",
            "tags": "sign, “STOP”",
        },
        "spec": {"thing": {"name": "“sign”"}},
    }
    out = shown_text.straighten(row)
    assert out["thoughts"][0]["text"] == "A sign reads \"STOP\" and a tag says 'SALE'; it's red."
    assert out["thoughts"][1] == row["thoughts"][1]
    assert out["wordings"]["caption"] == 'A red sign that reads "STOP".'
    assert out["wordings"]["prompt"] == 'red sign, "STOP"'
    assert out["wordings"]["critique"] == "The 'STOP' sign is sharp."
    assert out["wordings"]["tags"] == "sign, “STOP”" and out["spec"] == row["spec"]  # never the tags, never the records
    assert row["thoughts"][0]["text"].count("“") == 1  # the input is not changed
    plain = {"thoughts": [{"step": "say", "text": 'A sign reads "STOP".'}], "wordings": {"caption": "A sign."}}
    assert shown_text.straighten(plain) is plain and shown_text.straighten(out) == out


def test_a_picture_row_with_curly_marks_is_checked_as_straight_and_its_quotation_is_shown_text():
    item, row = _medium()
    curly = _pictured(_with(row, say="A sign over the door reads “OPEN”."))
    curly["wordings"]["caption"] = curly["wordings"]["caption"].rstrip(".") + ", under a sign that reads “OPEN”."
    straight = shown_text.straighten(curly)
    assert all(mark not in json.dumps(straight["thoughts"], ensure_ascii=False) + straight["wordings"]["caption"] for mark in CURLY)
    assert shown_text.shown(straight) == ["OPEN", "OPEN"]
    assert assessor.check_row(curly, item) == assessor.check_row(straight, item)
    assert not {code for code, severity in codes(curly, item) if severity == ERROR}


def _curly_rows(payload):
    row = {
        "id": f"{payload['line']}-{payload['attempt']}",
        "spec": payload["spec"],
        "thoughts": [{"step": "say", "text": "A sign reads “STOP”; it’s red."}],
        "wordings": {"caption": "A sign that reads “STOP”.", "tags": "sign, “STOP”"},
    }
    return FakeOutcome(output=json.dumps(row, ensure_ascii=False))


def test_the_controller_stores_straight_marks_for_a_picture_call_and_leaves_a_text_call_as_written(tmp_path):
    world = World(tmp_path, behavior=_curly_rows)
    controller = world.controller()
    controller.add_task(task_spec(("001",), batch="with-pictures", image="p.jpg"))
    controller.add_task(task_spec(("002",), batch="text-only"))
    world.drive(controller)
    stored = {row["row_id"].split("_")[1]: row for event in world.events_of("call_returned") for row in event.payload["rows"]}
    picture, text = stored["001"], stored["002"]
    assert picture["writer"]["image_size"] and text["writer"]["image_size"] is None
    assert picture["thoughts"][0]["text"] == "A sign reads \"STOP\"; it's red."
    assert picture["wordings"] == {"caption": 'A sign that reads "STOP".', "tags": "sign, “STOP”"}
    assert text["thoughts"][0]["text"] == "A sign reads “STOP”; it’s red."
    assert text["wordings"] == {"caption": "A sign that reads “STOP”.", "tags": "sign, “STOP”"}
    shipped = {row["row_id"].split("_")[1]: row for row in world.shipped_rows()}
    assert shipped["001"]["wordings"]["caption"] == 'A sign that reads "STOP".'
    assert shipped["002"]["wordings"]["caption"] == "A sign that reads “STOP”."


# ---------------------------------------------------------------------------- B2: the sentence's comma


@pytest.mark.parametrize("where", ["say", "caption", "prompt"])
def test_a_comma_inside_the_closing_mark_is_refused_on_a_picture_row(where):
    item, row = _medium()
    base = _with(row, say='A sign over the door reads "STOP".')
    if where == "say":
        bad = _with(row, say='A sign over the door reads "STOP," in red.')
    elif where == "caption":
        bad = _with(base, caption=base["wordings"]["caption"].rstrip(".") + ', under a sign that reads "STOP," in red.')
    else:
        bad = _with(base, prompt=base["wordings"]["prompt"] + ', sign reading "STOP," red letters')
    assert ("quote_comma", ERROR) in codes(_pictured(bad), item)
    (problem,) = [p for p in assessor.check_row(_pictured(bad), item) if p.code == "quote_comma"]
    assert problem.line() == (
        'quote_comma: quoted text holds the sentence\'s comma: put the comma after the closing quotation mark '
        '(write "STOP", not "STOP,"); the quotation holds only what the picture shows'
    )
    assert "quote_comma" not in {code for code, _ in codes(bad, item)}  # a text row is not refused


def test_a_curly_quotation_with_a_comma_inside_is_refused_too():
    item, row = _medium()
    assert ("quote_comma", ERROR) in codes(_pictured(_with(row, say="A sign over the door reads “STOP,” in red.")), item)


@pytest.mark.parametrize("say", ['A sign over the door reads "STOP", in red.', 'A low bridge sign reads "LOW CLEARANCE 9 FT."'])
def test_a_comma_after_the_closing_mark_and_a_period_inside_it_pass(say):
    item, row = _medium()
    pictured = _pictured(_with(row, say=say))
    assert "quote_comma" not in {code for code, _ in codes(pictured, item)}
    assert shown_text.comma_quotes(pictured) == []


def test_comma_quotes_names_each_quotation_that_ends_with_a_comma():
    row = {"thoughts": [{"step": "say", "text": 'Boards read "ATP," "BARCLAYS," and "LACOSTE".'}], "wordings": {"caption": 'Under "20 / MPH," a car.'}}
    assert shown_text.comma_quotes(row) == ["ATP,", "BARCLAYS,", "20 / MPH,"]
