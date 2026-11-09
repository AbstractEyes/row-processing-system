"""The policy setting `fix_to_hand`: a row with a fix verdict goes to the owner at once, through the controller."""

import json

from rps import layout

from harness import World, task_spec
from review_helpers import by_family, by_line, flag_everything, review_config, rows_by_line, write_hand_decisions

FIX_REASON = "a fix verdict: the row waits for the owner"
LINES = ("001", "002", "003", "004", "005", "006")
THREE = [
    {"id": "rev-a", "family": "alpha"},
    {"id": "rev-b", "family": "beta"},
    {"id": "rev-c", "family": "gamma"},
]
ALPHA_FIXES = by_family({"alpha": "fix", "beta": "keep", "gamma": "keep"})


def drive(tmp_path, *, fix_to_hand, verdict, lines=LINES, writer_family="writer-family", **controller_kwargs):
    config = review_config(THREE, writer_family=writer_family, policy={"fix_to_hand": fix_to_hand})
    world = World(tmp_path, config=config)
    controller = world.controller(**controller_kwargs)
    controller.add_task(task_spec(lines))
    world.drive(controller, verdict)
    return world, controller


def test_with_fix_to_hand_a_row_with_a_fix_goes_to_the_owner_after_one_review(tmp_path):
    world, controller = drive(tmp_path, fix_to_hand=True, verdict=ALPHA_FIXES)
    rows = rows_by_line(controller)
    # the first reviews go round rev-a, rev-b, rev-c: the rows rev-a saw first are the ones it asked a fix for
    for line in ("001", "004"):
        row = rows[line]
        assert row.status == "hand" and row.hand["reason"] == FIX_REASON
        assert [(v["reviewer_id"], v["verdict"]) for v in row.verdicts] == [("rev-a", "fix")]  # no second review
    for line in ("002", "003", "005", "006"):
        assert rows[line].status == "accepted" and len(rows[line].verdicts) == 1
    to_hand = world.events_of("row_to_hand")
    assert sorted(e.payload["row_id"] for e in to_hand) == sorted(rows[line].row_id for line in ("001", "004"))
    assert {e.payload["reason"] for e in to_hand} == {FIX_REASON}
    assert {tuple(e.payload["verdicts"]) for e in to_hand} == {("rev-a: fix",)}
    assert world.events_of("row_escalated") == []
    assert len(world.events_of("packet_published")) == 3  # the three first reviews and nothing after them
    assert len(world.shipped_rows()) == 4 and controller.is_idle()
    queue = [json.loads(line) for line in world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()]
    assert {entry["row_id"] for entry in queue} == {rows["001"].row_id, rows["004"].row_id}
    assert {entry["reason"] for entry in queue} == {FIX_REASON}
    assert all([v["verdict"] for v in entry["verdicts"]] == ["fix"] for entry in queue)  # the owner sees the fix


def test_without_the_setting_a_row_with_a_fix_goes_on_to_further_reviewers(tmp_path):
    world, controller = drive(tmp_path, fix_to_hand=False, verdict=ALPHA_FIXES)
    rows = rows_by_line(controller)
    for line in ("001", "004"):
        row = rows[line]
        assert row.verdicts[0]["verdict"] == "fix" and len(row.verdicts) >= 2  # it was asked again, as it always was
    assert FIX_REASON not in {e.payload["reason"] for e in world.events_of("row_to_hand")}
    assert len(world.events_of("row_escalated")) >= 2 and len(world.events_of("packet_published")) > 3


def test_with_fix_to_hand_a_fix_that_comes_after_a_keep_hands_the_row_over_as_well(tmp_path):
    # The writer's family is gamma, so first reviews alternate between rev-a and rev-b, and a further review goes
    # to a family that is not the writer's: a row that rev-a kept (it is flagged) goes to rev-b next.
    verdict = by_family({"alpha": "keep", "beta": "fix", "gamma": "keep"})
    world, controller = drive(
        tmp_path, fix_to_hand=True, verdict=verdict, lines=("001", "002"), writer_family="gamma", assess=flag_everything
    )
    rows = rows_by_line(controller)
    assert [(v["reviewer_id"], v["verdict"]) for v in rows["001"].verdicts] == [("rev-a", "keep"), ("rev-b", "fix")]
    assert [(v["reviewer_id"], v["verdict"]) for v in rows["002"].verdicts] == [("rev-b", "fix")]
    assert all(row.status == "hand" and row.hand["reason"] == FIX_REASON for row in rows.values())
    assert world.shipped_rows() == []
    assert [e.payload["reviewer_id"] for e in world.events_of("packet_published")].count("rev-b") == 2
    assert "rev-c" not in {e.payload["reviewer_id"] for e in world.events_of("packet_published")}  # never asked


def test_two_keeps_still_accept_a_flagged_row_with_fix_to_hand_on(tmp_path):
    # rev-a and rev-b keep each flagged row, so the two keeps accept it: gamma, who would ask for a fix, is never asked
    verdict = by_family({"alpha": "keep", "beta": "keep", "gamma": "fix"})
    world, controller = drive(
        tmp_path, fix_to_hand=True, verdict=verdict, lines=("001", "002"), writer_family="gamma", assess=flag_everything
    )
    for row in controller.state.rows.values():
        assert row.status == "accepted" and [v["verdict"] for v in row.verdicts] == ["keep", "keep"]
    assert world.events_of("row_to_hand") == []


def test_with_fix_to_hand_rejects_still_discard_a_row_and_keeps_still_accept_it(tmp_path):
    # line 001 is rejected by whoever sees it, 002 is kept: the setting changes nothing for rows with no fix
    world, controller = drive(tmp_path, fix_to_hand=True, verdict=by_line({"001": "reject"}), lines=("001", "002"))
    rows = rows_by_line(controller)
    assert rows["001"].status == "rejected" and rows["001"].rejection["by"] == "reviewers"
    assert [v["verdict"] for v in rows["001"].verdicts] == ["reject", "reject"]
    assert rows["002"].status == "accepted" and len(rows["002"].verdicts) == 1
    assert world.events_of("row_to_hand") == []


def test_the_owner_can_answer_a_row_that_was_handed_over_for_a_fix(tmp_path):
    world, controller = drive(tmp_path, fix_to_hand=True, verdict=by_line({"001": "fix"}), lines=("001", "002"))
    rows = rows_by_line(controller)
    assert rows["001"].status == "hand" and rows["002"].status == "accepted"
    replacement = {"id": "001-by-hand", "thoughts": ["the owner's own thought"]}
    write_hand_decisions(world, [{"row_id": rows["001"].row_id, "decision": "replace", "row": replacement}])
    controller.step()
    controller.step()
    assert controller.state.hand_queue() == []
    shipped = {row["row_id"]: row for row in world.shipped_rows()}
    assert set(shipped) == {rows["002"].row_id, rows["001"].row_id + "-h1"}
    assert shipped[rows["001"].row_id + "-h1"]["writer"]["provider"] == "human"
