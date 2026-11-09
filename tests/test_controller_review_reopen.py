"""Rows that went to the hand queue only for want of a reviewer are reopened when one is available."""

import json
from collections import Counter

import pytest

from rps import layout
from rps.events import replay
from rps.providers import SimulatedCrash
from rps.state import State

from harness import World, keep_all, task_spec
from review_helpers import (
    by_family,
    by_line,
    crash_after,
    flag_everything,
    flag_lines,
    outcome,
    review_config,
    rows_by_line,
    write_hand_decisions,
)

# A reviewer from the writer's own family cannot give a first review.
WRITER = {"id": "rev-w", "family": "writer-family"}
BETA = {"id": "rev-b", "family": "beta"}
FIRST_REASON = "no reviewer from another family than the writer is available"
FURTHER_REASON = "no reviewer from an unused family is available"


def first_review_stuck(tmp_path, lines=("001", "002", "003")):
    """Rows in the hand queue because the only reviewer shares the writer's family."""
    world = World(tmp_path, config=review_config([WRITER]))
    controller = world.controller()
    controller.add_task(task_spec(lines))
    controller.run()
    assert [e.payload["reason"] for e in world.events_of("row_to_hand")] == [FIRST_REASON] * len(lines)
    assert world.events_of("packet_published") == []
    return world, controller


def further_review_stuck(tmp_path):
    """A row in the hand queue because the two reviewers of the run were the only families there were."""
    config = review_config([{"id": "rev-a", "family": "alpha"}, BETA])
    world = World(tmp_path, config=config)
    controller = world.controller(assess=flag_everything)
    controller.add_task(task_spec(("001",)))
    world.drive(controller, by_family({"alpha": "reject", "beta": "keep"}))
    (row,) = controller.state.rows.values()
    assert row.status == "hand" and row.hand["reason"] == FURTHER_REASON
    return world, controller, row


# ------------------------------------------------------------------ reopening


def test_rows_that_waited_for_a_first_reviewer_are_reopened_when_one_is_added(tmp_path):
    world, first = first_review_stuck(tmp_path)
    assert len(first.state.hand_queue()) == 3
    world.config = review_config([WRITER, BETA])
    second = world.controller()
    world.drive(second, keep_all)
    reopened = world.events_of("row_reopened")
    assert sorted(e.payload["row_id"] for e in reopened) == sorted(second.state.rows)
    assert {e.payload["reason"] for e in reopened} == {"a reviewer from another family than the writer is now available"}
    assert all(e.payload["call_id"] == second.state.rows[e.payload["row_id"]].call_id for e in reopened)
    assert len(world.events_of("row_to_hand")) == 3  # nothing was sent there again
    assert {e.payload["reviewer_id"] for e in world.events_of("packet_published")} == {"rev-b"}
    assert second.state.counts()["rows"] == {"accepted": 3} and second.state.hand_queue() == []
    assert world.state_store.read_text(layout.HAND_QUEUE_PATH) == ""
    assert len(world.shipped_rows()) == 3
    # the first review is a first review: round 1, from a family other than the writer's
    assert {item["round"] for e in world.events_of("packet_published") for item in e.payload["items"]} == {1}
    assert second.state.snapshot() == State.from_events(world.events()).snapshot()


def test_rows_that_waited_for_a_further_reviewer_are_reopened_when_one_is_added(tmp_path):
    world, first, row = further_review_stuck(tmp_path)
    world.config = review_config([{"id": "rev-a", "family": "alpha"}, BETA, {"id": "rev-c", "family": "gamma"}])
    second = world.controller(assess=flag_everything)
    world.drive(second, by_family({"alpha": "reject", "beta": "keep", "gamma": "keep"}))
    (event,) = world.events_of("row_reopened")
    assert event.payload == {
        "row_id": row.row_id, "call_id": row.call_id, "reason": "a reviewer from an unused family is now available"
    }
    again = second.state.rows[row.row_id]
    assert again.status == "accepted" and again.hand is None
    assert [(v["reviewer_id"], v["verdict"]) for v in again.verdicts] == [("rev-a", "reject"), ("rev-b", "keep"), ("rev-c", "keep")]
    assert len(world.events_of("row_to_hand")) == 1
    (last,) = [e for e in world.events_of("packet_published") if e.payload["reviewer_id"] == "rev-c"]
    assert [item["round"] for item in last.payload["items"]] == [3]  # a further review, the third
    assert len(world.shipped_rows()) == 1
    assert second.state.snapshot() == State.from_events(world.events()).snapshot()


def test_a_row_the_reviewers_could_not_settle_stays_in_the_hand_queue(tmp_path):
    reviewers = [{"id": "rev-a", "family": "alpha"}, BETA, {"id": "rev-c", "family": "gamma"}]
    world = World(tmp_path, config=review_config(reviewers))
    first = world.controller(assess=flag_everything)
    first.add_task(task_spec(("001", "002")))
    world.drive(first, by_family({"alpha": "fix", "beta": "keep", "gamma": "reject"}))
    rows = list(first.state.rows.values())
    assert all(row.status == "hand" and "3 reviewers without a settlement" in row.hand["reason"] for row in rows)
    # a fourth reviewer from a family none of them used, and a policy that now allows four reviewers: the policy
    # would ask for another review of each row, but they were sent to the owner for another reason than a missing reviewer
    world.config = review_config(reviewers + [{"id": "rev-d", "family": "delta"}], policy={"max_reviewers": 4})
    second = world.controller(assess=flag_everything)
    for _ in range(4):
        second.step()
    assert world.events_of("row_reopened") == []
    assert all(second.state.rows[row.row_id].status == "hand" for row in rows)
    assert len(world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()) == 2
    assert len(world.events_of("row_to_hand")) == 2


def test_a_row_the_owner_resolved_is_never_reopened(tmp_path):
    world, first = first_review_stuck(tmp_path)
    rows = rows_by_line(first)
    write_hand_decisions(
        world,
        [{"row_id": rows["001"].row_id, "decision": "accept"}, {"row_id": rows["002"].row_id, "decision": "discard"}],
    )
    first.step()  # no reviewer can take the rows, so the owner's answers are all there is
    assert rows["001"].status == "accepted" and rows["002"].status == "rejected"
    world.config = review_config([WRITER, BETA])
    second = world.controller()
    world.drive(second, keep_all)
    assert [e.payload["row_id"] for e in world.events_of("row_reopened")] == [rows["003"].row_id]
    kept = second.state.rows[rows["001"].row_id]
    assert kept.status == "accepted" and kept.accepted_by == "hand" and kept.assignments == []
    discarded = second.state.rows[rows["002"].row_id]
    assert discarded.status == "rejected" and discarded.rejection["by"] == "hand" and discarded.assignments == []
    assert {row["row_id"] for row in world.shipped_rows()} == {rows["001"].row_id, rows["003"].row_id}


def test_the_owners_decision_is_read_before_a_row_is_looked_at_again(tmp_path):
    world, first = first_review_stuck(tmp_path, lines=("001", "002"))
    rows = rows_by_line(first)
    write_hand_decisions(world, [{"row_id": rows["001"].row_id, "decision": "accept"}])
    world.config = review_config([WRITER, BETA])  # the reviewer comes in the same pass in which the decision is read
    second = world.controller()
    second.step()
    assert second.state.rows[rows["001"].row_id].status == "accepted"
    assert second.state.rows[rows["001"].row_id].accepted_by == "hand"
    assert [e.payload["row_id"] for e in world.events_of("row_reopened")] == [rows["002"].row_id]
    assert second.state.rows[rows["002"].row_id].status == "in_review"


@pytest.mark.parametrize(
    "reviewers",
    [
        [WRITER, {"id": "rev-w2", "family": "writer-family"}],  # another reviewer, from the writer's family
        [WRITER, dict(BETA, enabled=False)],  # a reviewer that is listed and switched off
        [dict(WRITER, enabled=False), dict(BETA, enabled=False)],  # every reviewer is off
        [],  # none is listed
    ],
)
def test_rows_stay_in_the_hand_queue_while_no_reviewer_can_take_them(tmp_path, reviewers):
    world, first = first_review_stuck(tmp_path)
    world.config = review_config(reviewers)
    second = world.controller()
    for _ in range(3):
        second.step()
    assert world.events_of("row_reopened") == []
    assert len(second.state.hand_queue()) == 3 and len(world.events_of("row_to_hand")) == 3
    assert len(world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()) == 3


def test_a_further_review_is_not_offered_to_a_family_that_already_reviewed_the_row(tmp_path):
    world, first, row = further_review_stuck(tmp_path)
    # a second reviewer of the alpha family is no help: the row has had its alpha review
    world.config = review_config([{"id": "rev-a", "family": "alpha"}, {"id": "rev-a2", "family": "alpha"}, BETA])
    second = world.controller(assess=flag_everything)
    for _ in range(3):
        second.step()
    assert world.events_of("row_reopened") == []
    assert second.state.rows[row.row_id].status == "hand"


def test_a_row_reopened_and_settled_again_by_three_reviewers_goes_back_to_the_hand_queue_for_good(tmp_path):
    world, first, row = further_review_stuck(tmp_path)
    world.config = review_config([{"id": "rev-a", "family": "alpha"}, BETA, {"id": "rev-c", "family": "gamma"}])
    second = world.controller(assess=flag_everything)
    world.drive(second, by_family({"alpha": "reject", "beta": "keep", "gamma": "fix"}))
    again = second.state.rows[row.row_id]
    assert again.status == "hand" and "3 reviewers without a settlement" in again.hand["reason"]
    assert len(world.events_of("row_reopened")) == 1
    # a fourth reviewer does not reopen it: the three reviewers it had could not settle it
    world.config = review_config(
        [{"id": "rev-a", "family": "alpha"}, BETA, {"id": "rev-c", "family": "gamma"}, {"id": "rev-d", "family": "delta"}]
    )
    third = world.controller(assess=flag_everything)
    for _ in range(3):
        third.step()
    assert len(world.events_of("row_reopened")) == 1 and third.state.rows[row.row_id].status == "hand"


# ------------------------------------------------------------------ the policy as it stands now has the last word


def test_fix_to_hand_keeps_a_fix_row_in_the_hand_queue_while_a_flagged_keep_beside_it_is_reopened(tmp_path):
    alpha = {"id": "rev-a", "family": "alpha"}
    world = World(tmp_path, config=review_config([alpha]))
    first = world.controller(assess=flag_lines("002"))
    first.add_task(task_spec(("001", "002")))
    world.drive(first, by_line({"001": "fix"}))
    rows = rows_by_line(first)
    # one reviewer, one family: line 001 (a fix) and line 002 (a keep on a flagged row) both wait for another family
    assert {line: row.hand["reason"] for line, row in rows.items()} == {"001": FURTHER_REASON, "002": FURTHER_REASON}
    # a reviewer of another family comes, and the policy now hands a fix to the owner instead of asking again
    world.config = review_config([alpha, {"id": "rev-c", "family": "gamma"}], policy={"fix_to_hand": True})
    second = world.controller(assess=flag_lines("002"))
    world.drive(second, by_line({"001": "fix"}))
    again = rows_by_line(second)
    assert [e.payload["row_id"] for e in world.events_of("row_reopened")] == [again["002"].row_id]
    assert again["001"].status == "hand" and [v["verdict"] for v in again["001"].verdicts] == ["fix"]
    assert [(v["reviewer_id"], v["verdict"]) for v in again["002"].verdicts] == [("rev-a", "keep"), ("rev-c", "keep")]
    assert again["002"].status == "accepted"
    queue = world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()
    assert [json.loads(line)["row_id"] for line in queue] == [again["001"].row_id]
    assert second.state.snapshot() == State.from_events(world.events()).snapshot()


@pytest.mark.parametrize(
    "policy, reopened",
    [
        ({}, True),  # the policy of the first run: the row is still in question
        ({"fix_to_hand": True}, True),  # the row has no fix verdict, so the setting does not touch it
        ({"keeps_to_accept": 1}, False),  # one keep would accept it now
        ({"rejects_to_discard": 1}, False),  # one reject would discard it now
        ({"max_reviewers": 2}, False),  # two reviewers are all it gets now, so it is the owner's
    ],
)
def test_a_row_is_reopened_only_while_the_policy_as_it_stands_now_still_asks_for_a_review(tmp_path, policy, reopened):
    world, _first, row = further_review_stuck(tmp_path)  # a reject, then a keep, on a flagged row
    world.config = review_config([{"id": "rev-a", "family": "alpha"}, BETA, {"id": "rev-c", "family": "gamma"}], policy=policy)
    second = world.controller(assess=flag_everything)
    for _ in range(3):
        second.step()
    assert len(world.events_of("row_reopened")) == (1 if reopened else 0)
    queue = world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()
    if reopened:
        assert second.state.rows[row.row_id].status == "in_review" and queue == []
    else:
        # a reviewer is available and it makes no difference: the row stays for the owner, with the reason it came with
        assert second.state.rows[row.row_id].status == "hand" and second.state.rows[row.row_id].hand["reason"] == FURTHER_REASON
        assert [json.loads(line)["row_id"] for line in queue] == [row.row_id]
        assert world.events_of("packet_published")[-1].payload["reviewer_id"] != "rev-c"


def test_a_row_handed_over_for_a_fix_stays_in_the_hand_queue_when_the_policy_no_longer_does_that(tmp_path):
    reviewers = [{"id": "rev-a", "family": "alpha"}, BETA, {"id": "rev-c", "family": "gamma"}]
    world = World(tmp_path, config=review_config(reviewers, policy={"fix_to_hand": True}))
    first = world.controller()
    first.add_task(task_spec(("001",)))
    world.drive(first, by_family({"alpha": "fix", "beta": "keep", "gamma": "keep"}))
    (row,) = first.state.rows.values()
    assert row.status == "hand" and row.hand["reason"] == "a fix verdict: the row waits for the owner"
    # the setting is off again, so a fix would go on to a further reviewer, and two are available; but this row
    # was sent to the owner for a fix, not for want of a reviewer
    world.config = review_config(reviewers)
    second = world.controller()
    for _ in range(3):
        second.step()
    assert world.events_of("row_reopened") == [] and second.state.rows[row.row_id].status == "hand"


def test_rows_that_never_had_a_review_are_reopened_whatever_the_policy_says(tmp_path):
    world, _first = first_review_stuck(tmp_path)
    policy = {"fix_to_hand": True, "keeps_to_accept": 1, "rejects_to_discard": 1, "max_reviewers": 1}
    world.config = review_config([WRITER, BETA], policy=policy)
    second = world.controller()
    world.drive(second, keep_all)
    assert len(world.events_of("row_reopened")) == 3
    assert second.state.counts()["rows"] == {"accepted": 3} and len(world.shipped_rows()) == 3


def test_the_run_prints_how_many_rows_it_reopened(tmp_path):
    world, _first = first_review_stuck(tmp_path)
    world.config = review_config([WRITER, BETA])
    world.drive(world.controller(), keep_all)
    lines = [line for line in world.printed if "reopened" in line]
    assert len(lines) == 1 and "3 row(s)" in lines[0]


# ------------------------------------------------------------------ resuming


def stuck_then_reopened(tmp_path, crash_at=None):
    """The stuck rows of `first_review_stuck`, then a run with a reviewer added (killed after its crash_at-th event)."""
    world, _first = first_review_stuck(tmp_path)
    world.config = review_config([WRITER, BETA])
    before = len(world.events())
    if crash_at is None:
        controller = world.controller()
        world.drive(controller, keep_all)
        return world, controller, len(world.events()) - before
    first = world.controller(on_event=crash_after(crash_at))
    with pytest.raises(SimulatedCrash):
        world.drive(first, keep_all)
    second = world.controller()
    world.drive(second, keep_all)
    return world, second, None


def test_a_run_killed_after_any_event_of_a_reopening_ends_where_an_uninterrupted_one_does(tmp_path):
    reference, _controller, total = stuck_then_reopened(tmp_path / "reference")
    expected = outcome(reference)
    assert total > 10 and len(expected[1]) == 3
    for count in range(1, total):
        world, second, _ = stuck_then_reopened(tmp_path / f"kill-{count}", crash_at=count)
        assert outcome(world) == expected, f"a run killed after event {count} ended differently"
        reopened = Counter(e.payload["row_id"] for e in world.events_of("row_reopened"))
        assert set(reopened.values()) == {1} and len(reopened) == 3, f"after event {count}"
        assert second.state.snapshot() == State.from_events(replay(world.state_store).events).snapshot()
        assert world.state_store.read_text(layout.HAND_QUEUE_PATH) == ""
