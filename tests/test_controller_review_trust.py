"""Reviewers that may not keep alone (`solo_keep: false`), end to end through the controller."""

import json

from rps import layout

from harness import World, keep_all, task_spec
from review_helpers import by_family, flag_everything, review_config, rows_by_line

LINES = ("001", "002", "003", "004", "005", "006")


def trust_config(*, alpha=True, beta=True, gamma=True, writer_family="writer-family", **kwargs):
    """Three reviewers from three families; each flag says whether that reviewer may keep alone."""
    reviewers = [
        {"id": "rev-a", "family": "alpha", "solo_keep": alpha},
        {"id": "rev-b", "family": "beta", "solo_keep": beta},
        {"id": "rev-c", "family": "gamma", "solo_keep": gamma},
    ]
    return review_config(reviewers, writer_family=writer_family, **kwargs)


def drive(tmp_path, config, verdict=keep_all, lines=LINES, **controller_kwargs):
    world = World(tmp_path, config=config)
    controller = world.controller(**controller_kwargs)
    controller.add_task(task_spec(lines))
    world.drive(controller, verdict)
    return world, controller


def test_the_default_leaves_every_reviewer_able_to_keep_alone(tmp_path):
    world, controller = drive(tmp_path, trust_config())
    assert world.events_of("row_escalated") == []
    assert [len(row.verdicts) for row in controller.state.rows.values()] == [1] * 6
    assert len(world.shipped_rows()) == 6


def test_a_keep_from_a_reviewer_that_may_not_keep_alone_sends_the_row_to_a_further_reviewer(tmp_path):
    world, controller = drive(tmp_path, trust_config(alpha=False))
    rows = rows_by_line(controller)
    # the first reviews go round rev-a, rev-b, rev-c: the rows rev-a saw first are the ones in question
    by_alpha = [rows["001"], rows["004"]]
    for row in by_alpha:
        assert row.status == "accepted"
        assert [v["verdict"] for v in row.verdicts] == ["keep", "keep"]
        assert row.verdicts[0]["reviewer_id"] == "rev-a" and row.verdicts[1]["reviewer_id"] in ("rev-b", "rev-c")
    escalated = world.events_of("row_escalated")
    assert sorted(e.payload["row_id"] for e in escalated) == sorted(row.row_id for row in by_alpha)
    assert {e.payload["reason"] for e in escalated} == {"in_question"}
    assert {tuple(e.payload["families_used"]) for e in escalated} == {("alpha",)}
    # the other rows were accepted on their first keep, as before
    for line in ("002", "003", "005", "006"):
        assert len(rows[line].verdicts) == 1 and rows[line].status == "accepted"
    reasons = {e.payload["row_id"]: e.payload["reason"] for e in world.events_of("row_accepted")}
    assert {reasons[row.row_id] for row in by_alpha} == {"2 keep verdicts"}
    assert {reasons[rows[line].row_id] for line in ("002", "003", "005", "006")} == {"keep with no assessor flag"}
    assert len(world.shipped_rows()) == 6


def test_a_second_keep_from_a_reviewer_that_may_not_keep_alone_does_not_accept_the_row(tmp_path):
    # The writer's family is gamma, so a further reviewer comes from gamma only when nothing else is left:
    # a row goes alpha -> beta -> gamma (or beta -> alpha -> gamma), and only gamma's keep may stand alone.
    config = trust_config(alpha=False, beta=False, gamma=True, writer_family="gamma")
    world, controller = drive(tmp_path, config)
    for row in controller.state.rows.values():
        assert row.status == "accepted"
        assert [v["reviewer_id"] for v in row.verdicts][-1] == "rev-c" and len(row.verdicts) == 3
        assert {v["verdict"] for v in row.verdicts} == {"keep"}
    assert {tuple(e.payload["families_used"]) for e in world.events_of("row_escalated")} == {
        ("alpha",), ("beta",), ("alpha", "beta")
    }
    assert len(world.shipped_rows()) == 6


def test_rows_whose_keeps_all_come_from_reviewers_that_may_not_keep_alone_end_in_the_hand_queue(tmp_path):
    config = trust_config(alpha=False, beta=False, gamma=False)
    assert any("solo_keep" in note for note in config.problems())  # the configuration says so before the run
    world, controller = drive(tmp_path, config, lines=("001", "002"))
    rows = list(controller.state.rows.values())
    assert all(row.status == "hand" and len(row.verdicts) == 3 for row in rows)
    assert all({v["verdict"] for v in row.verdicts} == {"keep"} for row in rows)
    reasons = {e.payload["reason"] for e in world.events_of("row_to_hand")}
    assert len(reasons) == 1
    (reason,) = reasons
    assert reason.startswith("3 reviewers without a settlement") and "may not keep alone" in reason
    assert world.shipped_rows() == []
    queue = [json.loads(line) for line in world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()]
    assert {entry["row_id"] for entry in queue} == {row.row_id for row in rows}
    assert all(len(entry["verdicts"]) == 3 for entry in queue)  # the owner sees the three keeps


def test_a_flagged_row_needs_a_keep_from_a_reviewer_that_may_keep_alone_too(tmp_path):
    # Flagged rows already need two keeps; with rev-a unable to keep alone, a pair that includes rev-a still
    # accepts when its other keep comes from a reviewer that can.
    world, controller = drive(tmp_path, trust_config(alpha=False), assess=flag_everything)
    for row in controller.state.rows.values():
        assert row.status == "accepted"
        assert any(v["reviewer_id"] in ("rev-b", "rev-c") for v in row.verdicts)
        assert len(row.verdicts) >= 2


def test_rejects_count_whoever_gives_them(tmp_path):
    config = trust_config(alpha=False)
    world, controller = drive(tmp_path, config, by_family({"alpha": "reject", "beta": "reject", "gamma": "reject"}), lines=("001",))
    (row,) = controller.state.rows.values()
    assert row.status == "rejected" and row.rejection["by"] == "reviewers"
    assert [v["verdict"] for v in row.verdicts] == ["reject", "reject"]  # two rejects discard it, as before
    assert world.shipped_rows() == []


def test_an_audit_keep_from_a_reviewer_that_may_not_keep_alone_changes_nothing(tmp_path):
    # The writer's family is gamma: first reviews alternate between rev-a (may not keep alone) and rev-b,
    # and an audit goes to the family that has not seen the row, which for rev-b's rows is rev-a.
    config = trust_config(alpha=False, writer_family="gamma", policy={"audit_fraction": 1.0})
    world, controller = drive(tmp_path, config, lines=("001", "002", "003", "004"))
    assert not [e for e in world.events_of("row_escalated") if e.payload["reason"] == "audit_reject"]
    audited_by_alpha = [
        row for row in controller.state.rows.values() if any(v["audit"] and v["reviewer_id"] == "rev-a" for v in row.verdicts)
    ]
    assert audited_by_alpha  # rev-a did audit some rows
    for row in controller.state.rows.values():
        assert row.status == "accepted"
    assert len(world.shipped_rows()) == 4


def test_an_audit_reject_from_a_reviewer_that_may_not_keep_alone_still_reopens_the_row(tmp_path):
    config = trust_config(alpha=False, writer_family="gamma", policy={"audit_fraction": 1.0})
    seen = {}

    def verdict(reviewer, item):
        # rev-a rejects a row that somebody else has judged already (an audit); it keeps what it judges first
        earlier = seen.setdefault(item.row_id, [])
        earlier.append(reviewer.id)
        if reviewer.id == "rev-a" and len(earlier) > 1:
            return {"verdict": "reject", "reasons": ["person_visible"], "notes": "a person in the doorway"}
        return {"verdict": "keep"}

    world, controller = drive(tmp_path, config, verdict, lines=("001", "002"))
    rejected_audits = [e for e in world.events_of("row_escalated") if e.payload["reason"] == "audit_reject"]
    assert rejected_audits  # rev-b's row was audited by rev-a, which rejected it
    for event in rejected_audits:
        row = controller.state.rows[event.payload["row_id"]]
        assert row.reopened is True and row.status == "accepted"
        assert [(v["reviewer_id"], v["verdict"], v["audit"]) for v in row.verdicts] == [
            ("rev-b", "keep", False), ("rev-a", "reject", True), ("rev-c", "keep", False)
        ]
    assert len(world.shipped_rows()) == 2
