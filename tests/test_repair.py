"""The repair step's pure parts: which rows are candidates, what the writer is told and how, and the first-review pick."""

import json

import pytest

from rps import ids, repair
from rps import rules as pool
from rps.config import RulesConfig
from rps.prompting import REPAIR_CLOSING, REPAIR_INTRO, REPAIR_NOTES_HEAD, REPAIR_ROW_HEAD, load_writer_rules, render_task_text, repair_text
from rps.provenance import RESERVED_ROW_KEYS
from rps.providers import FakeWriter
from rps.providers.base import CallSpec
from rps.review.policy import FIX_TO_HAND_REASON, pick_first_reviewer
from rps.state import RowState

from helpers import Builder, make_config
from repair_helpers import FIX_REASON, THREE, repair_config, repair_route

TASK = ids.task_id("photos", "1", {"001": "a cat"})


def row(row_id="r1", *, status="hand", reason=FIX_REASON, route="fake-batch", hand_extra=None, **fields):
    hand = None if reason is None else {"reason": reason, "ts": "2026-11-07T09:00:00.000000Z", **(hand_extra or {})}
    fields.setdefault("record", {})
    return RowState(row_id=row_id, call_id="c", task_id=TASK, line="001", route=route, status=status, hand=hand, **fields)


def verdict(kind, notes="", reviewer="rev-a", family="alpha", reasons=("needs_work",)):
    return {"reviewer_id": reviewer, "family": family, "verdict": kind, "reasons": list(reasons), "notes": notes, "audit": False, "round": 1}


# ------------------------------------------------------------------ the same reason as the policy's


def test_the_reason_a_candidate_waits_for_is_the_one_the_policy_gives_a_fix():
    assert FIX_REASON == FIX_TO_HAND_REASON == "a fix verdict: the row waits for the owner"


# ------------------------------------------------------------------ which rows


def test_a_row_in_the_hand_queue_for_a_fix_that_was_never_sent_back_is_a_candidate():
    assert repair.is_candidate(row())


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "accepted"},  # the owner accepted it
        {"status": "rejected"},  # the owner discarded it
        {"status": "in_review"},
        {"status": "repairing"},
        {"status": "repaired"},
        {"reason": "3 reviewers without a settlement"},  # in the queue for another reason
        {"reason": "no reviewer from another family than the writer is available"},
        {"reason": "no reviewer from an unused family is available"},
        {"reason": None},
        {"hand_extra": {"decision": "accept"}},  # the owner has answered
        {"repair": {"call_id": "x", "route": "fake-batch", "calls": [], "rows": []}},  # it was sent back before
        {"repair": {"failed": "the repair failed the checker: the row waits for the owner"}},
        {"repair_of": "some-other-row"},  # it is itself the result of a repair
    ],
)
def test_any_other_row_is_not_a_candidate(changes):
    assert not repair.is_candidate(row(**changes))


def test_candidates_come_in_row_id_order_and_only_the_ones_that_qualify():
    rows = [row("r3"), row("r1"), row("r2", reason="other"), row("r4", repair_of="r1"), row("r0", status="accepted")]
    assert [item.row_id for item in repair.candidates(rows)] == ["r1", "r3"]
    assert repair.candidates([]) == []


@pytest.mark.parametrize(
    "repair_section, route_changes, route_name, expected",
    [
        ({"enabled": True}, {}, "fake-batch", True),
        ({"enabled": False}, {}, "fake-batch", False),  # the step is off
        ({"enabled": True}, {}, "other-route", False),  # no such route
        ({"enabled": True}, {"enabled": False}, "fake-batch", False),  # the route is switched off
        ({"enabled": True}, {"caps": {"production": 6.0}}, "fake-batch", False),  # no cap for repair
        ({"enabled": True}, {"caps": {"production": 6.0, "repair": 0}}, "fake-batch", False),  # a cap of nothing
        ({"enabled": True}, {"caps": {"repair": 0.01}}, "fake-batch", True),
    ],
)
def test_only_a_route_that_is_on_and_has_a_repair_cap_gets_repairs(repair_section, route_changes, route_name, expected):
    route = repair_route(route_changes.get("caps"))
    if "enabled" in route_changes:
        route["enabled"] = route_changes["enabled"]
    config = repair_config(route=route, enabled=repair_section["enabled"])
    assert repair.route_may_repair(config, route_name) is expected


# ------------------------------------------------------------------ what the writer is told


def test_the_notes_of_every_fix_verdict_come_in_the_order_given_and_nothing_else_of_the_verdicts():
    verdicts = [
        verdict("keep", "good", reviewer="rev-k", family="kappa"),
        verdict("fix", "thought 2 names a pose no record gives", reviewer="rev-a", family="alpha"),
        verdict("reject", "never mind", reviewer="rev-r", family="rho"),
        verdict("fix", "  the mood is read from the angle \n", reviewer="rev-b", family="beta"),
    ]
    notes = repair.fix_notes(verdicts)
    assert notes == ["thought 2 names a pose no record gives", "the mood is read from the angle"]
    text = " ".join(notes)
    for who in ("rev-a", "rev-b", "rev-k", "rev-r", "alpha", "beta", "kappa", "rho"):
        assert who not in text  # no reviewer's id and no family
    assert repair.fix_notes([]) == [] and repair.fix_notes([verdict("keep", "x")]) == []


def test_a_fix_verdict_with_no_note_says_so_and_gives_its_reason_codes_so_the_writer_is_never_handed_an_empty_line():
    assert repair.fix_notes([verdict("fix", "", reasons=("invented_detail", "mood"))]) == [
        "The reviewer gave no note. Reason code(s): invented_detail, mood."
    ]
    assert repair.fix_notes([verdict("fix", "   ", reasons=())]) == ["The reviewer gave no note."]
    assert repair.fix_notes([{"verdict": "fix"}]) == ["The reviewer gave no note."]


def test_the_row_as_it_was_written_is_the_stored_row_without_the_labels_the_runner_stamped():
    builder = Builder()
    task = builder.task(lines={"001": "a cat"})
    call = builder.prepare(task, "001")
    record = builder.row(call, 0, text="a cat with a hat 猫")
    text = repair.written_row_text(record)
    written = json.loads(text)
    assert written == {"id": "001-0", "thoughts": ["a cat with a hat 猫"]}  # what the writer's own answer held
    assert not set(RESERVED_ROW_KEYS) & set(written)
    assert "猫" in text  # kept as written, not escaped
    assert list(written) == ["id", "thoughts"]  # in the writer's order
    # a stored row keeps its labels; they are only left out of what the writer is shown
    assert set(RESERVED_ROW_KEYS) <= set(record)


def test_the_feedback_is_the_notes_then_the_row_as_written_as_the_last_item():
    builder = Builder()
    task = builder.task(lines={"001": "a cat"})
    call = builder.prepare(task, "001")
    record = builder.row(call, 0, text="old thought")
    stored = row(record=record, verdicts=[verdict("fix", "first problem"), verdict("keep"), verdict("fix", "second problem", reviewer="rev-b")])
    feedback = repair.repair_feedback(stored)
    assert isinstance(feedback, tuple) and all(isinstance(item, str) for item in feedback)
    assert feedback[:-1] == ("first problem", "second problem")
    assert json.loads(feedback[-1]) == {"id": "001-0", "thoughts": ["old thought"]}
    assert "rev-" not in " ".join(feedback)


# ------------------------------------------------------------------ the words around them


def test_the_repair_text_says_a_reviewer_sent_the_row_back_lists_the_notes_and_shows_the_row():
    text = repair_text(["the first problem", "the second\nproblem", '{"id": "x"}'])
    assert text.startswith(REPAIR_INTRO) and text.endswith(REPAIR_CLOSING)
    assert "sent back by a reviewer" in REPAIR_INTRO and "fix exactly the problems the notes name" in REPAIR_INTRO
    assert "keep everything else in the earlier row that was right" in REPAIR_INTRO
    assert f"{REPAIR_NOTES_HEAD}\n- the first problem\n- the second\n  problem\n\n{REPAIR_ROW_HEAD}\n" + '{"id": "x"}' in text
    assert "failed these checks" not in text  # that is the retry's wording, about checks
    # a row with no note at all still goes back with the row
    only_row = repair_text(['{"id": "x"}'])
    assert REPAIR_NOTES_HEAD not in only_row and '{"id": "x"}' in only_row
    assert repair_text([]) == "" and repair_text(()) == ""


def test_a_call_of_the_repair_step_follows_the_task_text_with_the_repair_words_not_the_retry_words():
    feedback = ("a problem", '{"id": "x"}')
    plain = render_task_text("a plain spec", feedback, repair=True)
    assert plain == "a plain spec\n\n" + repair_text(feedback)
    retry = render_task_text("a plain spec", feedback)
    assert "failed these checks" in retry and "sent back by a reviewer" not in retry  # a retry is unchanged
    assert render_task_text("a plain spec", (), repair=True) == "a plain spec"  # nothing to say, nothing added


@pytest.mark.parametrize("profile", ["default", "openai", "gemini", "deepseek"])
def test_every_prompt_profile_sends_a_row_back_in_the_same_words_after_its_own_task_text(profile):
    rules = load_writer_rules(RulesConfig(package="rps.rules"))
    chosen = rules.profile_loader(profile) if profile != "default" else rules
    item = next(entry for entry in pool.examples() if entry["item"].get("kind") == "series")["item"]
    feedback = ("the mood in thought 3 is read from the camera angle", '{"id": "x", "thoughts": ["a"]}')
    task = render_task_text(item, ordinal=2, rules=chosen)
    repaired = render_task_text(item, feedback, ordinal=2, rules=chosen, repair=True)
    assert repaired.startswith(task.rstrip("\n"))  # the same task, from the same spec line and the same choices
    assert repaired.endswith(repair_text(feedback))
    assert "- the mood in thought 3 is read from the camera angle" in repaired
    retry = render_task_text(item, feedback, ordinal=2, rules=chosen)
    assert retry != repaired and "sent back by a reviewer" not in retry  # the retry block is the profile's own


def test_a_call_spec_names_the_row_it_writes_again_and_the_fake_writer_hands_that_on():
    config = make_config()
    writer = FakeWriter("fake-batch", config.routes["fake-batch"])
    call = CallSpec(
        call_id=ids.call_id(TASK, "001", 2), task_id=TASK, line="001", attempt=2, route="fake-batch", provider="fake",
        model="fake-model", kind="batch", effort=None, max_output_tokens=100, spec="a cat", pool_version="1",
        purpose="repair", feedback=("note", "{}"), repair_of="some-row",
    )
    (request,) = writer.prepare([call])
    assert request.payload["repair_of"] == "some-row" and request.payload["feedback"] == ["note", "{}"]
    other = CallSpec(**{**call.__dict__, "repair_of": None, "call_id": ids.call_id(TASK, "001", 1), "attempt": 1})
    assert writer.prepare([other])[0].payload["repair_of"] is None
    assert CallSpec(**{k: v for k, v in other.__dict__.items() if k != "repair_of"}).repair_of is None  # the field has a default


# ------------------------------------------------------------------ why a row came back to the owner


def test_the_reasons_a_row_whose_repair_came_to_nothing_gives_the_owner_say_what_happened():
    assert repair.failure_reason("checker") == "the repair failed the checker: the row waits for the owner"
    assert repair.failure_reason("checker") == repair.FAILED_CHECKER_REASON
    assert "writer set it aside" in repair.failure_reason("no_row")
    assert repair.failure_reason("call_failed", "provider_error") == "the repair call failed (provider_error): the row waits for the owner"
    assert repair.failure_reason("call_failed") == "the repair call failed: the row waits for the owner"
    assert "switched off" in repair.failure_reason("switched_off")
    reasons = {repair.failure_reason(code) for code in ("checker", "no_row", "call_failed", "switched_off")}
    assert len(reasons) == 4 and FIX_REASON not in reasons  # a returned row is never a candidate again
    assert all(reason.endswith("the row waits for the owner") for reason in reasons)


# ------------------------------------------------------------------ the first review of a row written again


def reviewers_of(reviewers):
    return repair_config(list(reviewers)).review.reviewers


def test_the_first_review_prefers_a_reviewer_that_did_not_review_the_row_it_replaces():
    reviewers = reviewers_of(THREE)
    for ordinal in range(6):
        picked = pick_first_reviewer(reviewers, "writer-family", ordinal, avoid={"rev-a"})
        assert picked.id in ("rev-b", "rev-c")
    # round robin among those left
    assert [pick_first_reviewer(reviewers, "writer-family", n, avoid={"rev-a"}).id for n in range(4)] == ["rev-b", "rev-c", "rev-b", "rev-c"]
    assert pick_first_reviewer(reviewers, "writer-family", 0, avoid={"rev-b", "rev-c"}).id == "rev-a"


def test_the_first_review_goes_back_to_the_same_reviewer_when_no_other_one_is_available():
    reviewers = reviewers_of([THREE[0]])
    assert pick_first_reviewer(reviewers, "writer-family", 0, avoid={"rev-a"}).id == "rev-a"
    both = reviewers_of(THREE[:2])
    assert pick_first_reviewer(both, "writer-family", 3, avoid={"rev-a", "rev-b"}).id == "rev-b"  # all were used: the usual rotation
    # the writer's own family is never eligible, whatever the preference
    own = reviewers_of([{"id": "rev-w", "family": "writer-family"}])
    assert pick_first_reviewer(own, "writer-family", 0, avoid=()) is None
    assert pick_first_reviewer(own, "writer-family", 0, avoid={"rev-x"}) is None


def test_without_a_preference_the_pick_is_the_one_it_always_was():
    reviewers = reviewers_of(THREE)
    assert [pick_first_reviewer(reviewers, "writer-family", n).id for n in range(6)] == ["rev-a", "rev-b", "rev-c"] * 2
    assert [pick_first_reviewer(reviewers, "alpha", n).id for n in range(4)] == ["rev-b", "rev-c", "rev-b", "rev-c"]
    assert [pick_first_reviewer(reviewers, "writer-family", n, avoid=()).id for n in range(3)] == ["rev-a", "rev-b", "rev-c"]
