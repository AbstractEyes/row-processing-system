import pytest

from rps.config import ReviewerConfig, ReviewPolicy
from rps.review.policy import (
    ACCEPT,
    DISCARD,
    FURTHER,
    HAND,
    NEED_FIRST,
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


def test_first_reviewer_is_round_robin_across_all_reviewers():
    picks = []
    pointer = 0
    for _ in range(6):
        reviewer, pointer = pick_first_reviewer(REVIEWERS, "writer-family", pointer)
        picks.append(reviewer.id)
    assert picks == ["r-alpha", "r-beta", "r-gamma", "r-alpha", "r-beta", "r-gamma"]


def test_first_reviewer_is_never_from_the_writers_family():
    picks = []
    pointer = 0
    for _ in range(6):
        reviewer, pointer = pick_first_reviewer(REVIEWERS, "alpha", pointer)
        picks.append(reviewer.id)
    assert "r-alpha" not in picks
    assert picks == ["r-beta", "r-gamma", "r-beta", "r-gamma", "r-beta", "r-gamma"]


def test_first_reviewer_is_none_when_every_reviewer_shares_the_writers_family():
    only = [ReviewerConfig("a1", "alpha"), ReviewerConfig("a2", "alpha")]
    assert pick_first_reviewer(only, "alpha", 0) == (None, 0)
    assert pick_first_reviewer([], "alpha", 0) == (None, 0)


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
