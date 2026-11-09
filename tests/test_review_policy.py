import pytest

from rps.config import ReviewerConfig, ReviewPolicy
from rps.review.policy import (
    ACCEPT,
    DISCARD,
    FIX_TO_HAND_REASON,
    FURTHER,
    HAND,
    NEED_FIRST,
    Settlement,
    audit_selected,
    pick_first_reviewer,
    pick_further_reviewer,
    settle,
)

POLICY = ReviewPolicy()
REVIEWERS = [
    ReviewerConfig("r-alpha", "alpha"),
    ReviewerConfig("r-beta", "beta"),
    ReviewerConfig("r-gamma", "gamma"),
]


def outcome(verdicts, *, flagged=False, reopened=False, policy=POLICY):
    return settle(policy, flagged=flagged, verdicts=verdicts, reopened=reopened).outcome


def test_no_verdict_means_a_first_review_is_needed():
    assert outcome([]) == NEED_FIRST


def test_a_keep_with_no_flag_accepts_the_row_at_once():
    assert outcome(["keep"]) == ACCEPT


def test_a_keep_on_a_flagged_row_leaves_it_in_question():
    assert outcome(["keep"], flagged=True) == FURTHER
    assert outcome(["keep", "keep"], flagged=True) == ACCEPT


@pytest.mark.parametrize("first", ["fix", "reject"])
def test_a_fix_or_reject_puts_the_row_in_question(first):
    assert outcome([first]) == FURTHER


def test_two_keeps_accept_an_in_question_row():
    assert outcome(["reject", "keep", "keep"]) == ACCEPT
    assert outcome(["fix", "keep", "keep"]) == ACCEPT
    assert outcome(["keep", "keep"], flagged=True) == ACCEPT


def test_two_rejects_discard_the_row():
    assert outcome(["reject", "reject"]) == DISCARD
    assert outcome(["keep", "reject", "reject"]) == DISCARD
    assert outcome(["fix", "reject", "reject"]) == DISCARD


def test_after_three_reviewers_without_a_settlement_the_row_goes_to_hand():
    assert outcome(["reject", "keep"]) == FURTHER  # two reviewers: still open
    assert outcome(["reject", "keep", "fix"]) == HAND
    assert outcome(["fix", "fix", "fix"]) == HAND
    assert outcome(["fix", "keep", "reject"]) == HAND


def test_a_reopened_row_does_not_use_the_first_keep_shortcut():
    # accepted on a first keep, then an audit reviewer rejected it
    assert outcome(["keep", "reject"], reopened=True) == FURTHER
    assert outcome(["keep", "reject", "keep"], reopened=True) == ACCEPT
    assert outcome(["keep", "reject", "reject"], reopened=True) == DISCARD
    assert outcome(["keep", "reject", "fix"], reopened=True) == HAND


def test_the_first_keep_shortcut_can_be_turned_off():
    strict = ReviewPolicy(first_keep_accepts=False)
    assert outcome(["keep"], policy=strict) == FURTHER
    assert outcome(["keep", "keep"], policy=strict) == ACCEPT


def test_the_thresholds_come_from_the_policy():
    loose = ReviewPolicy(keeps_to_accept=3, rejects_to_discard=3, max_reviewers=5)
    assert outcome(["reject", "reject"], policy=loose) == FURTHER
    assert outcome(["reject", "reject", "reject"], policy=loose) == DISCARD
    assert outcome(["reject", "keep", "keep", "fix"], policy=loose) == FURTHER
    assert outcome(["reject", "keep", "fix", "fix", "fix"], policy=loose) == HAND


# ------------------------------------------------------------------ reviewers that may not keep alone


def alone(verdicts, solo, *, flagged=False, reopened=False, policy=POLICY):
    return settle(policy, flagged=flagged, verdicts=verdicts, reopened=reopened, solo=solo)


def test_a_keep_from_a_reviewer_that_may_not_keep_alone_leaves_the_row_in_question():
    assert outcome(["keep"]) == ACCEPT  # the same keep from a reviewer that may
    assert alone(["keep"], [True]).outcome == ACCEPT
    assert alone(["keep"], [False]).outcome == FURTHER  # as a flagged keep is
    assert alone(["keep"], [False]).outcome == outcome(["keep"], flagged=True)


def test_a_keep_from_a_reviewer_that_may_keep_alone_still_counts_toward_the_two_keeps():
    assert alone(["keep", "keep"], [False, True]).outcome == ACCEPT
    assert alone(["keep", "keep"], [True, False]).outcome == ACCEPT
    assert alone(["keep", "keep"], [True, True]).outcome == ACCEPT
    # an in-question row: a reject, then a keep that may not stand alone, then one that may
    assert alone(["reject", "keep", "keep"], [True, False, True]).outcome == ACCEPT
    assert alone(["keep", "keep"], [True, False], flagged=True).outcome == ACCEPT


def test_keeps_that_all_come_from_reviewers_that_may_not_keep_alone_never_accept_the_row():
    assert alone(["keep", "keep"], [False, False]).outcome == FURTHER  # two keeps are not enough
    third = alone(["keep", "keep", "keep"], [False, False, False])
    assert third.outcome == HAND and "3 reviewers without a settlement" in third.reason
    assert "may not keep alone" in third.reason
    # the third reviewer may keep alone: that keep makes the difference
    assert alone(["keep", "keep", "keep"], [False, False, True]).outcome == ACCEPT
    assert alone(["reject", "keep", "keep"], [False, False, True]).outcome == ACCEPT
    # a reject from a reviewer that may keep alone does not make the other two keeps enough
    assert alone(["reject", "keep", "keep"], [True, False, False]).outcome == HAND


def test_rejects_do_not_depend_on_who_may_keep_alone():
    assert alone(["reject", "reject"], [False, False]).outcome == DISCARD
    assert alone(["keep", "reject", "reject"], [False, True, True]).outcome == DISCARD
    assert alone(["fix", "fix", "fix"], [False, False, False]).outcome == HAND


def test_a_reviewer_that_may_not_keep_alone_cannot_accept_when_one_keep_is_enough():
    assert alone(["keep"], [False], policy=ReviewPolicy(keeps_to_accept=1)).outcome == FURTHER
    assert alone(["keep"], [True], policy=ReviewPolicy(keeps_to_accept=1)).outcome == ACCEPT
    # the first-keep shortcut turned off: a single keep never accepts, whoever gave it
    assert alone(["keep"], [True], policy=ReviewPolicy(first_keep_accepts=False)).outcome == FURTHER


def test_a_reopened_row_counts_who_may_keep_alone_among_every_verdict():
    assert alone(["keep", "reject", "keep"], [False, True, False], reopened=True).outcome == HAND
    assert alone(["keep", "reject", "keep"], [True, True, False], reopened=True).outcome == ACCEPT


@pytest.mark.parametrize(
    "verdicts",
    [[], ["keep"], ["fix"], ["reject"], ["keep", "keep"], ["reject", "keep"], ["reject", "reject"], ["reject", "keep", "keep"], ["fix", "fix", "fix"]],
)
@pytest.mark.parametrize("flagged", [False, True])
def test_leaving_out_who_may_keep_alone_means_everyone_may(verdicts, flagged):
    assert settle(POLICY, flagged=flagged, verdicts=verdicts) == settle(
        POLICY, flagged=flagged, verdicts=verdicts, solo=[True] * len(verdicts)
    )


def test_solo_needs_one_entry_for_each_verdict():
    with pytest.raises(ValueError):
        settle(POLICY, flagged=False, verdicts=["keep", "keep"], solo=[True])


# ------------------------------------------------------------------ a fix verdict handed to the owner

FIX_TO_HAND = ReviewPolicy(fix_to_hand=True)
VERDICT_LISTS = [
    [], ["keep"], ["fix"], ["reject"], ["keep", "keep"], ["keep", "fix"], ["fix", "keep"], ["reject", "keep"], ["reject", "fix"],
    ["fix", "reject"], ["reject", "reject"], ["fix", "fix"], ["reject", "keep", "keep"], ["fix", "keep", "keep"],
    ["keep", "keep", "fix"], ["fix", "reject", "reject"], ["reject", "reject", "fix"], ["fix", "fix", "fix"],
    ["fix", "keep", "reject"], ["keep", "keep", "keep"],
]


def test_the_wording_of_the_fix_reason_is_fixed():
    assert FIX_TO_HAND_REASON == "a fix verdict: the row waits for the owner"


def test_by_default_a_fix_sends_the_row_on_to_a_further_reviewer():
    assert ReviewPolicy().fix_to_hand is False
    for verdicts in (["fix"], ["keep", "fix"], ["reject", "fix"], ["fix", "keep"], ["fix", "reject"]):
        assert outcome(verdicts) == FURTHER
    assert outcome(["fix", "fix", "fix"]) == HAND and "3 reviewers without a settlement" in settle(
        POLICY, flagged=False, verdicts=["fix", "fix", "fix"]
    ).reason


@pytest.mark.parametrize("flagged", [False, True])
@pytest.mark.parametrize("verdicts", [["fix"], ["keep", "fix"], ["fix", "keep"], ["reject", "fix"], ["fix", "reject"], ["fix", "fix"]])
def test_with_fix_to_hand_a_fix_hands_the_row_to_the_owner_at_once(verdicts, flagged):
    assert settle(FIX_TO_HAND, flagged=flagged, verdicts=verdicts) == Settlement(HAND, FIX_TO_HAND_REASON)


def test_with_fix_to_hand_the_accept_and_discard_rules_are_checked_first_and_win():
    assert settle(FIX_TO_HAND, flagged=False, verdicts=["fix", "keep", "keep"]) == settle(POLICY, flagged=False, verdicts=["fix", "keep", "keep"])
    assert outcome(["fix", "keep", "keep"], policy=FIX_TO_HAND) == ACCEPT
    assert outcome(["keep", "keep", "fix"], policy=FIX_TO_HAND) == ACCEPT
    assert outcome(["fix", "reject", "reject"], policy=FIX_TO_HAND) == DISCARD
    assert outcome(["reject", "reject", "fix"], policy=FIX_TO_HAND) == DISCARD
    # a looser policy settles a row earlier, and that settlement still comes before the fix
    assert outcome(["fix", "keep"], policy=ReviewPolicy(fix_to_hand=True, keeps_to_accept=1)) == ACCEPT
    assert outcome(["fix", "reject"], policy=ReviewPolicy(fix_to_hand=True, rejects_to_discard=1)) == DISCARD


def test_with_fix_to_hand_keeps_that_may_not_stand_alone_do_not_beat_a_fix():
    # two keeps, none from a reviewer that may keep alone: not an acceptance, so the fix hands the row on
    assert alone(["fix", "keep", "keep"], [True, False, False], policy=FIX_TO_HAND) == Settlement(HAND, FIX_TO_HAND_REASON)
    assert alone(["fix", "keep", "keep"], [True, True, False], policy=FIX_TO_HAND).outcome == ACCEPT


def test_with_fix_to_hand_the_fix_reason_replaces_the_reviewer_limit_reason():
    assert settle(FIX_TO_HAND, flagged=True, verdicts=["fix", "fix", "fix"]) == Settlement(HAND, FIX_TO_HAND_REASON)
    assert settle(FIX_TO_HAND, flagged=False, verdicts=["keep", "reject", "fix"], reopened=True) == Settlement(HAND, FIX_TO_HAND_REASON)


@pytest.mark.parametrize("verdicts", VERDICT_LISTS)
@pytest.mark.parametrize("flagged", [False, True])
def test_fix_to_hand_changes_only_rows_that_have_a_fix_verdict_and_are_not_settled_by_keeps_or_rejects(verdicts, flagged):
    without = settle(POLICY, flagged=flagged, verdicts=verdicts)
    with_it = settle(FIX_TO_HAND, flagged=flagged, verdicts=verdicts)
    if "fix" not in verdicts or without.outcome in (ACCEPT, DISCARD):
        assert with_it == without
    else:
        assert with_it == Settlement(HAND, FIX_TO_HAND_REASON)


def test_first_reviewer_is_round_robin_across_all_reviewers():
    picks = [pick_first_reviewer(REVIEWERS, "writer-family", ordinal).id for ordinal in range(6)]
    assert picks == ["r-alpha", "r-beta", "r-gamma", "r-alpha", "r-beta", "r-gamma"]


def test_first_reviewer_is_never_from_the_writers_family():
    picks = [pick_first_reviewer(REVIEWERS, "alpha", ordinal).id for ordinal in range(6)]
    assert "r-alpha" not in picks
    assert picks == ["r-beta", "r-gamma", "r-beta", "r-gamma", "r-beta", "r-gamma"]


def test_first_reviewer_depends_only_on_its_arguments():
    assert pick_first_reviewer(REVIEWERS, "alpha", 7) is pick_first_reviewer(REVIEWERS, "alpha", 7)
    assert [pick_first_reviewer(REVIEWERS, "alpha", n).id for n in (4, 5)] == ["r-beta", "r-gamma"]  # a restart picks the same


def test_first_reviewer_is_none_when_every_reviewer_shares_the_writers_family():
    only = [ReviewerConfig("a1", "alpha"), ReviewerConfig("a2", "alpha")]
    assert pick_first_reviewer(only, "alpha", 0) is None
    assert pick_first_reviewer([], "alpha", 0) is None


def test_further_reviewer_comes_from_a_family_not_yet_used():
    chosen = pick_further_reviewer(REVIEWERS, {"beta"}, "alpha", "row-1")
    assert chosen.family == "gamma"
    assert pick_further_reviewer(REVIEWERS, {"beta", "gamma"}, "other", "row-1").family == "alpha"
    assert pick_further_reviewer(REVIEWERS, {"alpha", "beta", "gamma"}, "other", "row-1") is None
    assert pick_further_reviewer([], set(), "x", "row-1") is None


def test_further_reviewer_prefers_other_families_than_the_writer():
    # alpha wrote the row; beta already reviewed; gamma and alpha are unused. gamma comes first.
    for key in ("a", "b", "c", "d", "e"):
        assert pick_further_reviewer(REVIEWERS, {"beta"}, "alpha", key).family == "gamma"
    # With nothing else left the writer's own family is used.
    assert pick_further_reviewer(REVIEWERS, {"beta", "gamma"}, "alpha", "a").family == "alpha"


def test_further_reviewer_choice_is_stable_and_spreads_over_equals():
    many = [ReviewerConfig(f"r{n}", "same") for n in range(3)] + [ReviewerConfig("x", "other")]
    picks = {pick_further_reviewer(many, {"other"}, "writer", f"row-{n}").id for n in range(60)}
    assert picks == {"r0", "r1", "r2"}
    assert pick_further_reviewer(many, {"other"}, "writer", "row-5") == pick_further_reviewer(many, {"other"}, "writer", "row-5")


def test_audit_sample_is_reproducible_and_near_the_fraction():
    selected = [n for n in range(4000) if audit_selected(f"row-{n}", 0.10, "seed")]
    assert 280 < len(selected) < 520
    assert selected == [n for n in range(4000) if audit_selected(f"row-{n}", 0.10, "seed")]
    other = [n for n in range(4000) if audit_selected(f"row-{n}", 0.10, "other-seed")]
    assert selected != other


def test_audit_fraction_extremes():
    assert not any(audit_selected(f"r{n}", 0.0, "s") for n in range(100))
    assert all(audit_selected(f"r{n}", 1.0, "s") for n in range(100))
