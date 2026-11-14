"""The text a picture shows: the writer's rule, the reviewers' brief, and the checker's view of a row that quotes it.

A writer with the picture attached reads the text in it: a thought with the step "say" gives the words in double quotes
and the caption and the prompt quote them. On a picture row that shown text is the picture's, not the row's prose, so no
check reads it as a claim of the row. Only a quotation that a "say" thought gives, and that is no longer than a simple
row's thought, counts as shown text: any other quotation is read as the row's own words. A text row is read whole, as
before. Every row here is invented or one of the pool's worked examples.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import rps
from rps import assessor, rules
from rps.assess import AssessContext
from rps.assessor import AS_WRITTEN_CHECKS, HINT_COPY_CHECKS, ROW_CHECK_GROUPS, checks, invention_checks, review_checks, shown_text
from rps.assessor.problems import ERROR, FLAG
from rps.review.api_seat import reason_codes
from rps.rules import form, spelling

MODEL = "test-model"
RULES = Path(rules.__file__).parent
PROMPTS = {"default": RULES / "writer_system.md", **{name: RULES / "profiles" / name / "writer_system.md" for name in ("openai", "gemini", "deepseek")}}
TEMPLATES = {"default": RULES / "templates" / "writer_system.tpl.md", **{name: RULES / "profiles" / name / "writer_system.tpl.md" for name in ("openai", "gemini", "deepseek")}}
BRIEF_BEFORE = "51a624992384"  # the brief version of 0.6.0: a seat names it in also_brief_versions to answer packets made under it
# 0.6.2: the sentence that follows " / " between lines in every writer prompt (quote marks and the sentence's punctuation)
QUOTE_MARKS_RULE = (
    "Use plain straight double quotes, never curly ones, and keep your own sentence's punctuation outside them: "
    "write \"STOP\", then the comma, never \"STOP,\" (a mark that is part of the shown text stays inside the quotes)."
)
TEXT_RULE = (
    "Text is among the things you read from the picture: when the picture shows text that can be read (a sign, a label, a screen, "
    "a page, lettering on clothing or on a vehicle, an inscription), the row reads it. One thought with the step \"say\" gives the words "
    "in double quotes, exactly as written: the same spelling and letter case, with \" / \" between lines. " + QUOTE_MARKS_RULE + " The caption quotes those words "
    "in double quotes, and so does the prompt where the row has one; the tags quote nothing. A hint may quote or list text that the "
    "picture shows; it helps with spelling only. Quote a word only when you can read it in the picture yourself, in the form you see it. "
    "Never quote a hinted word you cannot find in the picture. A word cut off by the frame or by something in front of it is given only "
    "as far as it shows and is said to be cut off. Where the text is longer than the length limit of the thoughts allows, "
    "quote its main words (the largest, or the title) and say that more text follows. Text too small or too blurred to read at the "
    "picture's size is called unreadable and never guessed. A person's name is never quoted: a signature, a photo credit or a name tag "
    "is described as such, without the name; brand names and place names may be quoted. A picture that shows no text gets no \"say\" "
    "thought for text."
)
REASON_CODES = (
    "gloss", "invented", "hint_copy", "contradicts_picture", "should_skip", "person", "minor", "real_name", "mood", "voice", "relation",
    "record_missing", "genre", "wording", "garment", "padding", "stock", "question", "advice", "other",
)


def _example(name):
    example = next(e for e in rules.examples() if e["name"] == name)
    return example["item"], [form.to_row(r, example["item"], MODEL) for r in example["output"]["rows"]]


def _pictured(row, size=512):
    row = copy.deepcopy(row)
    row["writer"] = {**(row.get("writer") or {}), "image_size": size}
    return row


def _medium():
    item, rows = _example("people_series")
    return item, copy.deepcopy(rows[1])


def _quoting(row, text):
    """The row with the text of a sign in it: a say thought gives the words, the caption and the prompt quote them."""
    row = copy.deepcopy(row)
    row["thoughts"].append({"step": "say", "text": f'A sign over the door reads "{text}".'})
    row["wordings"]["caption"] = row["wordings"]["caption"].rstrip(".") + f', under a sign that reads "{text}".'
    row["wordings"]["prompt"] += f', sign that reads "{text}"'
    return row


def found(row, item=None, severity=None):
    return {p.code for p in assessor.check_row(row, item) if severity is None or p.severity == severity}


# ---------------------------------------------------------------------------- the writer's rule


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_every_writer_prompt_tells_a_writer_with_the_picture_to_read_the_text_it_shows(name):
    text = PROMPTS[name].read_text(encoding="utf-8")
    assert text.count(TEXT_RULE) == 1 and TEMPLATES[name].read_text(encoding="utf-8").count(TEXT_RULE) == 1
    assert text.index("WHEN THE PICTURE IS ATTACHED") < text.index(TEXT_RULE) < text.index("These stay as they are")
    # a brand may now be quoted, so only a person's name stays out of what is copied from writing in the picture
    assert "never copy a person's name from writing in the picture" in text
    assert "never copy a name or a brand" not in text
    assert "No real person's name" in text  # the rule on names in the wordings is what it was


def test_the_vocabulary_and_the_row_schema_are_what_they_were():
    from thought_seeds.core import tp

    row = rules.row_schema()["properties"]["rows"]["items"]["properties"]
    assert sorted(row) == sorted(["id", "series", "depth", "rank", "domain", "genre", "records", "relations", "thoughts", "wordings", "questions"])
    assert row["thoughts"]["items"]["properties"]["step"]["enum"] == list(tp.STEPS) and "say" in tp.STEPS and len(tp.STEPS) == 16
    assert row["wordings"]["items"]["properties"]["kind"]["enum"] == ["caption", "prompt", "tags", "critique"]
    assert sorted(row["thoughts"]["items"]["properties"]) == ["step", "text"]
    assert sorted(row["questions"]["items"]["properties"]) == ["key", "needs", "value"]
    for path in PROMPTS.values():
        assert ", ".join(tp.STEPS) in path.read_text(encoding="utf-8")  # the steps line names say as before


def test_a_say_thought_that_quotes_nothing_is_checked_as_any_thought_is():
    item, row = _medium()
    row["thoughts"].append({"step": "say", "text": "Mustard over navy is the whole of it."})
    assert assessor.check_row(row, item) == [] and assessor.check_row(_pictured(row), item) == []


# ---------------------------------------------------------------------------- the reviewers' brief and the pool


def test_the_brief_says_how_a_quoted_text_is_judged_and_adds_no_reason_code():
    brief = rules.review_brief()
    section = brief.split("## Rows with a picture")[1].split("## What to read for")[0]
    (paragraph,) = [p for p in section.split("\n\n") if p.startswith("Text in the picture.")]
    for phrase in (
        "a thought with the step `say` gives the words in double quotes",
        "Compare every quoted text with the picture, letter for letter",
        "A quoted text that differs from what the picture shows is `contradicts_picture`",
        "a quoted text the picture does not show at all is `invented`",
        "A row that leaves out text that is plainly readable and is the picture's main subject goes back with `fix` and the code `wording`",
        "does not send a row back by itself",
        "rightly called unreadable",
        "A quoted text that the picture does not show readably is a fault, `invented`, even when the hint lists it: "
        "the hint is no evidence that text can be read at the picture's size",
        "A word cut off by the frame or by something in front of it is quoted only as far as it shows",
        "a quoted person's name is `real_name`",
        "a brand name or a place name may be quoted",
        "Such a thought is not `padding`, and words the picture shows are not `hint_copy`, also when the hint quotes or lists them",
        "When `image_withheld` is true you cannot compare a quoted text with the picture",
    ):
        assert phrase in paragraph, phrase
    assert reason_codes(brief) == REASON_CODES  # the table is the one of the releases before


def test_the_pool_holds_the_rules_on_text_and_the_brief_version_is_new():
    pool = rules.pool()
    for number, word in ((113, "WRITER"), (114, "WRITER"), (115, "READER"), (116, "ASSESSOR")):
        (line,) = [line for line in pool.splitlines() if line.startswith(f"| {number} |")]
        assert f"| {word} |" in line
    version = rules.rules_version()
    assert version != BRIEF_BEFORE and version == (RULES / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = (RULES.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{rps.__version__}"' in pyproject


# ---------------------------------------------------------------------------- what counts as quoted


def test_a_quoted_text_is_what_stands_between_double_quotes():
    assert shown_text.quoted('A sign reads "OPEN / 24 HOURS" and a mat says “Welcome”.') == ["OPEN / 24 HOURS", "Welcome"]
    assert shown_text.quoted('A 12" ruler lies beside a "STOP" sign.') == ["STOP"]  # an inch mark opens nothing
    assert shown_text.quoted("The dog's bowl and the cats' bed.") == [] and shown_text.quoted('An empty "" mark.') == []
    assert shown_text.outside_quotes('grey "GREY" grey', str.upper) == 'GREY "GREY" GREY'
    assert shown_text.outside_quotes("no quotes here", str.upper) == "NO QUOTES HERE"


def test_a_quotation_is_shown_text_only_in_a_say_thought_or_when_it_repeats_what_one_quotes():
    row = {
        "thoughts": [
            {"step": "see", "text": 'A glass door with a "push" plate.'},
            {"step": "say", "text": 'The sign reads "OPEN / 24 HOURS" and a mat says "Welcome".'},
        ],
        "wordings": {
            "caption": 'A glass door under a sign that reads "OPEN / 24 HOURS".',
            "prompt": 'glass door, sign reading "OPEN 24 HOURS", mat reading "welcome", plate reading "push"',
            "critique": 'The "24 HOURS" line is the brightest thing here; "OPEN / 24 HOURS / DAILY" would crowd it.',
            "tags": '"OPEN" sign',
        },
    }
    before = copy.deepcopy(row)
    # in the say thought; the same words in the caption; the same words without the line mark; a run of them. Not shown
    # text: a quotation no say thought holds ("push"), another letter case ("welcome"), more words than the say thought gives
    assert shown_text.shown(row) == ["OPEN / 24 HOURS", "Welcome", "OPEN / 24 HOURS", "OPEN 24 HOURS", "24 HOURS"]
    prose = shown_text.own_prose(row)
    assert row == before  # the row is not changed
    assert [t["text"] for t in prose["thoughts"]] == ['A glass door with a "push" plate.', 'The sign reads "" and a mat says "".']
    assert prose["wordings"] == {
        "caption": 'A glass door under a sign that reads "".',
        "prompt": 'glass door, sign reading "", mat reading "welcome", plate reading "push"',
        "critique": 'The "" line is the brightest thing here; "OPEN / 24 HOURS / DAILY" would crowd it.',
        "tags": '"OPEN" sign',  # the tags are a list of tags: they are left alone
    }
    assert shown_text.own_prose(prose) is prose  # nothing is left to empty: the row itself
    no_say = {"thoughts": [{"step": "see", "text": 'A sign reads "OPEN".'}], "wordings": {"caption": 'A sign that reads "OPEN".'}}
    assert shown_text.shown(no_say) == [] and shown_text.own_prose(no_say) is no_say
    odd = {"thoughts": ["a bare string", {"step": "say"}], "wordings": None}
    assert shown_text.own_prose(odd) is odd and shown_text.beside_hint(odd, {"hint": 'A "word".'}) == (odd, {"hint": 'A "word".'})


def test_a_quotation_longer_than_a_simple_rows_thought_is_not_shown_text():
    # the cap is the simple depth's thought length: every row of a picture quotes the same words, the simple one too
    assert shown_text.SHOWN_TEXT_MAX == 100 and "1 or 2 thoughts of at most 100 bytes" in rules.writer_system()
    for size, counts in ((100, True), (101, False)):
        text = ("french fries " + "x" * 200)[:size]
        row = {"thoughts": [{"step": "say", "text": f'A sign reads "{text}".'}], "wordings": {"caption": f'A sign that reads "{text}".'}}
        assert shown_text.shown(row) == ([text, text] if counts else [])
        assert ("style_lowercase" in found(_pictured(row))) is not counts  # over the cap it is read as the row's own words
    accents = {"thoughts": [{"step": "say", "text": '"' + "é" * 51 + '"'}]}  # 51 letters, 102 bytes: the cap counts bytes
    assert shown_text.shown(accents) == [] and len(shown_text.shown({"thoughts": [{"step": "say", "text": '"' + "é" * 50 + '"'}]})) == 1


# ---------------------------------------------------------------------------- the checker: quoted words are not the row's claims

# (what the sign says, the check that would read it as the row's own words, how that check reports)
SIGNS = [
    ("STORY TIME", "spec_talk", ERROR),
    ("french fries", "style_lowercase", ERROR),
    ("THE RECORD SHOP", "spec_language", ERROR),
    ("PIXEL 9", "rule_1", ERROR),
    ("EXACTLY 5 MINUTES", "pasted_value", ERROR),
    ("TABLE SERVICE", "rank_scope", ERROR),
    ("A DRESS SALE", "dress_caption", ERROR),
    ("HAPPY HOUR", "second_mood", FLAG),
    ("RED BULL", "unrecorded_color", FLAG),
    ("BRICK LANE", "unrecorded_material", FLAG),
    ("Fresh Bread Daily", "names", FLAG),
]


@pytest.mark.parametrize("text, code, severity", SIGNS, ids=[code for _, code, _ in SIGNS])
def test_what_a_sign_says_is_not_read_as_the_rows_own_words_on_a_picture_row(text, code, severity):
    item, row = _medium()
    quoting = _quoting(row, text)
    # a text row is read whole: the check takes the quoted words for the row's
    assert code in found(quoting, item, severity)
    # a picture row: no finding at all, as for the same row before it quoted anything
    assert assessor.check_row(_pictured(row), item) == [] and assessor.check_row(_pictured(quoting), item) == []


@pytest.mark.parametrize("text", ["OPEN", "OPEN / 24 HOURS", "NO LEFT TURN", "Bakery", "ROAD CLOSED", "KEEP CALM"])
def test_capitals_double_quotes_and_line_marks_fail_no_check(text):
    item, row = _medium()
    quoting = _pictured(_quoting(row, text))  # a say thought gives the words; the caption and the prompt quote them
    assert shown_text.shown(quoting) == [text, text, text]
    assert assessor.check_row(quoting, item) == []
    context = AssessContext(task_id="t0123456789abcdef", source="s", batch="b", line="l1", attempt=1, route="r", pool_version="v", spec=item, item_id=None)
    result = assessor.Assessor()(quoting, context)
    assert result.passed and result.flags == () and result.errors == ()


# A whole thought of ordinary prose between quote marks, and a short one (the first is longer than the cap).
WRAPPED = '"I think maybe this could be a sad mood, but I am not sure. The pixel grid of the image file shows a man walking by."'
WRAPPED_SHORT = '"The pixel grid shows a man walking by."'


@pytest.mark.parametrize("step, text", [("place", WRAPPED), ("say", WRAPPED), ("place", WRAPPED_SHORT)], ids=["no say thought", "over the cap", "short, no say thought"])
def test_prose_put_between_quote_marks_is_read_as_the_rows_own_words(step, text):
    item, row = _medium()
    row["thoughts"][1] = {"step": step, "text": text}
    wrapped = _pictured(row)
    assert shown_text.shown(wrapped) == [] and shown_text.own_prose(wrapped) is wrapped
    problems = {(p.code, p.severity) for p in assessor.check_row(wrapped, item)}
    assert ("rule_1", ERROR) in problems  # the talk about the file is read again
    assert ("unrecorded_pose", FLAG) in problems  # and so is the pose no record gives (a flag on a picture row)
    assert {code for code, _ in problems} == found(row, item)  # the checks find what they find on the text row


def test_a_caption_that_quotes_what_no_say_thought_gives_is_read_as_prose():
    item, row = _medium()
    row["wordings"]["caption"] = row["wordings"]["caption"].rstrip(".") + ', under a sign that reads "french fries".'
    assert shown_text.shown(row) == [] and "style_lowercase" in found(_pictured(row), item, ERROR)
    said = copy.deepcopy(row)
    said["thoughts"].append({"step": "say", "text": 'A sign over the door reads "french fries".'})
    assert assessor.check_row(_pictured(said), item) == []  # the say thought gives the words: the caption quotes shown text
    more = copy.deepcopy(said)
    more["wordings"]["caption"] = more["wordings"]["caption"].replace('"french fries"', '"hot french fries"')
    assert "style_lowercase" in found(_pictured(more), item, ERROR)  # words the say thought does not give
    other = copy.deepcopy(said)
    other["thoughts"][-1] = {"step": "see", "text": 'A sign over the door reads "french fries".'}
    assert "style_lowercase" in found(_pictured(other), item, ERROR)  # the step is not say: nothing is shown text


def test_a_sign_that_says_open_does_not_answer_an_unknown_question():
    item, row = _medium()
    quoting = _quoting(row, "OPEN")
    next(q for q in quoting["questions"] if q["needs"] == "unknown")["key"] = "scene layout"
    assert "unknown_stated" in found(quoting, item, ERROR)  # a text row: refused, as before
    assert assessor.check_row(_pictured(quoting), item) == []
    said = copy.deepcopy(quoting)
    said["thoughts"][1]["text"] = "An open cafe in the morning, shown full length so the skirt is in the frame."
    assert "unknown_stated" in found(_pictured(said), item, ERROR)  # the row's own word still answers it


def test_the_size_of_a_row_counts_every_word_it_quotes():
    item, row = _medium()
    assert "rule_3" not in found(_pictured(_quoting(row, "OPEN")), item)
    long = _pictured(_quoting(row, " / ".join(["GRAND OPENING SALE THIS WEEKEND ONLY"] * 6)))
    (problem,) = [p for p in assessor.check_row(long, item) if p.code == "rule_3"]
    assert problem.severity == ERROR and "slices" in problem.message


def test_fit_words_about_a_minor_are_read_in_quoted_words_too():
    item, row = _medium()
    quoting = _quoting(row, "FRAGILE")
    assert assessor.check_row(_pictured(quoting), item) == []  # an adult: the word on the sign is no finding
    quoting["spec"]["person"]["age"] = "child"
    assert "minor_fit" in found(quoting, item, ERROR) and "minor_fit" in found(_pictured(quoting), item, ERROR)
    assert AS_WRITTEN_CHECKS == {review_checks.minor_fit_check}


def test_the_row_checks_are_all_run_and_the_hint_checks_are_the_ones_that_compare_words_with_the_hint():
    every = [check for group in ROW_CHECK_GROUPS for check in group]
    assert len(every) == len(set(every)) and AS_WRITTEN_CHECKS <= set(every) and HINT_COPY_CHECKS <= set(every)
    assert set(checks.ROW_CHECKS) | set(review_checks.REVIEW_CHECKS) | set(invention_checks.INVENTION_CHECKS) <= set(every)
    by_hint_name = {check for check in every if "hint" in check.__name__}
    # person_in_hint and hint_contradiction read the hint against the records, never the row's words
    assert by_hint_name - HINT_COPY_CHECKS == {checks.person_in_hint_check, review_checks.hint_contradiction_check}
    assert HINT_COPY_CHECKS <= by_hint_name and not HINT_COPY_CHECKS & AS_WRITTEN_CHECKS


# ---------------------------------------------------------------------------- the checker: the hint quotes the same words

HINT = (
    'A white sign with black text that reads "NO PARKING ANY TIME" is attached to a gray pole beside a road. '
    "A red car is parked behind the pole, and a row of trees lines the far side of the street."
)
ITEM = {"kind": "single", "hint": HINT, "spec": {"thing": {"name": "sign", "colour": "white"}}}


def _row(*thoughts, caption="", picture=True):
    row = {
        "id": "art-1",
        "series": "s-1",
        "depth": "medium",
        "spec": {"thing": {"name": "sign", "colour": "white"}},
        "thoughts": [{"step": step, "text": text} for step, text in thoughts],
        "wordings": {"caption": caption, "prompt": "", "tags": "", "critique": ""},
        "questions": [],
    }
    return _pictured(row) if picture else row


def hint_findings(row, item=ITEM):
    return {(p.code, p.severity) for p in assessor.check_row(row, item) if "hint" in p.code}


def test_words_the_row_and_the_hint_both_quote_do_not_count_toward_hint_copying():
    says = ("say", 'The sign reads "NO PARKING ANY TIME".')
    assert hint_findings(_row(says, picture=False)) == {("thought_hint_copy", ERROR)}  # a text row: every word counts, as before
    assert hint_findings(_row(says)) == set()
    own = ("say", 'Black capitals spell "NO PARKING ANY TIME" across the board.')
    assert hint_findings(_row(own)) == set() and hint_findings(_row(own, picture=False)) == set()
    # a hint that quotes in single quotes is read the same way
    single = dict(ITEM, hint=HINT.replace('"', "'"))
    assert hint_findings(_row(says), single) == set() and hint_findings(_row(says, picture=False), single) == {("thought_hint_copy", ERROR)}


def test_a_run_of_ten_words_that_is_all_quoted_text_is_not_a_copied_run():
    notice = "ALL VISITORS MUST REPORT TO THE FRONT OFFICE BEFORE ENTERING THE BUILDING"
    item = dict(ITEM, hint=f'A poster on the wall says "{notice}" in red letters. A door stands open beside it.')
    says = ("say", f'Red capitals on the poster spell "{notice}".')
    assert ("hint_copy", FLAG) in hint_findings(_row(says, picture=False), item)
    assert hint_findings(_row(says), item) == set()


def test_a_sentence_taken_from_the_hint_is_still_seen_around_a_quotation_and_inside_one():
    around = ("see", 'A white sign with black text that reads "NO PARKING ANY TIME" is attached to a gray pole beside a road.')
    assert hint_findings(_row(around)) == {("hint_copy", FLAG), ("thought_hint_copy", FLAG)}  # the words around the quotation are the hint's
    inside = ("see", 'The scene: "A red car is parked behind the pole, and a row of trees lines the far side of the street."')
    assert hint_findings(_row(inside)) == {("hint_copy", FLAG), ("thought_hint_copy", FLAG)}  # the hint does not quote these words
    leaning = ("say", 'The white sign on the gray pole reads "NO PARKING ANY TIME".')
    assert hint_findings(_row(leaning)) == {("thought_hint_copy", FLAG)}  # the row's own words are the hint's: a reader compares
    caption = 'A white sign that reads "NO PARKING ANY TIME" stands on a gray pole beside a road.'
    assert hint_findings(_row(("see", "A sign."), caption=caption)) == {("caption_hint_copy", FLAG)}


def test_the_hint_view_leaves_out_the_shared_words_on_both_sides_and_nothing_else():
    row = _row(("say", 'The sign reads "NO PARKING" and a sticker reads "PAID".'))
    viewed, item = shown_text.beside_hint(row, ITEM)
    assert viewed["thoughts"][0]["text"] == 'The sign reads " " and a sticker reads "PAID".'  # PAID is quoted by the row alone
    assert item["hint"].startswith('A white sign with black text that reads "  ANY TIME" is attached')
    assert item["spec"] is ITEM["spec"] and ITEM["hint"] == HINT and "NO PARKING" in row["thoughts"][0]["text"]
    assert shown_text.beside_hint(row, None) == (row, None) and shown_text.beside_hint(row, {"hint": "A plain hint."})[0] is row
    # only shown text is taken out: a quotation that no say thought gives stays, whatever the hint quotes
    unsaid = _row(("see", 'The sign reads "NO PARKING".'))
    assert shown_text.beside_hint(unsaid, ITEM) == (unsaid, ITEM)


# The reviewed case: the hint quotes a word, the row copies the hint's sentence with that word outside quote marks, and
# quotes the word somewhere else.
DOOR_HINT = 'A small sign that says "open" hangs by the blue door.'
DOOR_ITEM = dict(ITEM, hint=DOOR_HINT)
COPIED = "A small sign that says open hangs by the blue door."


def test_a_copied_hint_sentence_is_caught_whether_or_not_the_row_quotes_one_of_its_words_elsewhere():
    plain = _row(("see", COPIED), caption='The shop shows "open".', picture=False)
    assert ("hint_copy", FLAG) in hint_findings(plain, DOOR_ITEM)
    reviewed = _row(("see", COPIED), caption='The shop shows "open".')
    assert ("hint_copy", FLAG) in hint_findings(reviewed, DOOR_ITEM)
    # the same when a say thought gives the word, so that it is shown text the hint quotes too
    said = _row(("see", COPIED), ("say", 'The sign reads "open".'), caption='The shop shows "open".')
    assert shown_text.shown(said) == ["open", "open"] and shown_text.beside_hint(said, DOOR_ITEM)[1]["hint"] == 'A small sign that says "" hangs by the blue door.'
    assert hint_findings(said, DOOR_ITEM) == {("hint_copy", FLAG), ("thought_hint_copy", FLAG)}
    # and when the copied sentence keeps the word between quote marks: the ten words around it are a run
    around = _row(("see", DOOR_HINT), ("say", 'The sign reads "open".'))
    assert ("hint_copy", FLAG) in hint_findings(around, DOOR_ITEM)
    # a row that quotes the word and copies no sentence is clean
    clean = _row(("say", 'The door sign reads "open".'), caption='The shop shows "open".')
    assert shown_text.shown(clean) == ["open", "open"] and hint_findings(clean, DOOR_ITEM) == set()


def test_the_other_hint_checks_see_a_copied_sentence_too():
    # most of a hint sentence, with the hint's quoted words outside quote marks
    banner = dict(ITEM, hint='The banner says "grand opening sale this weekend" today. A door stands open beside it.')
    row = _row(("see", "The banner says grand opening sale this weekend today."), ("say", 'The banner reads "grand opening sale this weekend".'))
    assert hint_findings(row, banner) == {("thought_hint_copy", FLAG)}
    # the hint's sentences taken in the hint's order
    street = dict(
        ITEM,
        hint=DOOR_HINT + " A red bicycle leans on the brick wall below the sign. Two clay pots of basil stand on the stone step. A gray cat sleeps on the mat by the step.",
    )
    walked = _row(
        ("place", COPIED),
        ("relate", "A red bicycle leans on the brick wall below the sign."),
        ("count", "Two clay pots of basil stand on the stone step."),
        ("say", 'The sign reads "open".'),
    )
    assert {("hint_copy", FLAG), ("hint_order", FLAG), ("thought_hint_copy", FLAG)} <= hint_findings(walked, street)
    own = _row(
        ("place", "The blue door carries a hand-lettered board at eye height."),
        ("relate", "A bicycle rests under the board, its red frame against brick."),
        ("say", 'The board reads "open".'),
        caption='A blue door with a board that reads "open" above a red bicycle.',
    )
    assert hint_findings(own, street) == set()


# ---------------------------------------------------------------------------- the spelling step


def _british(picture):
    row = {
        "id": "art-900011",
        "depth": "medium",
        "spec": {"scene": {"setting": "harbour"}},
        "thoughts": [
            {"step": "see", "text": "A grey harbour wall at the centre."},
            {"step": "say", "text": 'The grey sign reads "TOWN CENTRE / Theatre Royal".'},
        ],
        "wordings": {"caption": 'A grey sign that reads "TOWN CENTRE / Theatre Royal" hangs on a harbour wall.', "tags": "grey sign, harbour"},
        "questions": [
            {"key": "form palette", "value": "grey", "needs": "chain"},
            {"key": "intent message", "value": "town centre", "needs": "chain"},
            {"key": "scene setting", "value": "harbour", "needs": "record"},
        ],
    }
    return _pictured(row) if picture else row


def test_the_spelling_step_leaves_the_quoted_words_of_a_picture_row_as_written():
    row = _british(picture=True)
    before = copy.deepcopy(row)
    out = spelling.americanize_row(row)
    assert row == before
    assert out["thoughts"][0]["text"] == "A gray harbor wall at the center."
    assert out["thoughts"][1]["text"] == 'The gray sign reads "TOWN CENTRE / Theatre Royal".'
    assert out["wordings"] == {"caption": 'A gray sign that reads "TOWN CENTRE / Theatre Royal" hangs on a harbor wall.', "tags": "gray sign, harbor"}
    # a chain value is spelled as the thoughts spell it: outside the quotation it follows them, inside it stays
    assert [q["value"] for q in out["questions"]] == ["gray", "town centre", "harbour"]
    stated = " ".join(t["text"] for t in out["thoughts"]).lower()
    assert all(q["value"] in stated for q in out["questions"] if q["needs"] == "chain")


def test_the_spelling_step_respells_a_text_row_as_before():
    out = spelling.americanize_row(_british(picture=False))
    assert out["thoughts"][1]["text"] == 'The gray sign reads "TOWN CENTER / Theater Royal".'
    assert out["wordings"]["caption"] == 'A gray sign that reads "TOWN CENTER / Theater Royal" hangs on a harbor wall.'
    assert [q["value"] for q in out["questions"]] == ["gray", "town center", "harbour"]
    assert json.dumps(out) == json.dumps(spelling.americanize_row(out))  # and it settles
