"""Packets addressed to a reviewer who is switched off, or no longer configured, are withdrawn."""

import json
from collections import Counter

import pytest

from rps import layout
from rps.events import replay
from rps.providers import SimulatedCrash
from rps.state import State

from harness import World, keep_all, task_spec
from review_helpers import (
    crash_after,
    crash_on,
    outcome,
    packet_files,
    pictures_in_the_store,
    review_config,
    write_hand_decisions,
)

# Reviewers whose families may see the pictures of a public dataset, so that packets carry pictures.
OPENAI = {"id": "rev-o", "family": "openai"}
GOOGLE = {"id": "rev-g", "family": "google"}
WRITER = {"id": "rev-w", "family": "writer-family"}
LINES = ("001", "002", "003", "004")


def off(reviewer):
    return dict(reviewer, enabled=False)


def published(tmp_path, reviewers=(OPENAI, GOOGLE), lines=LINES):
    """A run that has written the rows and published a packet to each reviewer (rev-o gets rows 001 and 003)."""
    world = World(tmp_path, config=review_config(list(reviewers)))
    controller = world.controller()
    controller.add_task(task_spec(lines, image="p.jpg"))
    controller.step()
    controller.step()
    packets = {e.payload["reviewer_id"]: e.payload for e in world.events_of("packet_published")}
    assert set(packets) == {reviewer["id"] for reviewer in reviewers}
    return world, controller, packets


def row_ids(packet):
    return sorted(item["row_id"] for item in packet["items"])


# ------------------------------------------------------------------ withdrawing


def test_a_packet_for_a_reviewer_who_was_switched_off_is_withdrawn_and_its_rows_go_to_another(tmp_path):
    world, _first, packets = published(tmp_path)
    old = packets["rev-o"]
    assert len(packet_files(world, "rev-o", old["packet_id"])) == 1 + len(old["items"])  # packet.json and a picture a row
    world.config = review_config([off(OPENAI), GOOGLE])
    second = world.controller()
    world.drive(second, keep_all)
    (event,) = world.events_of("packet_withdrawn")
    assert event.payload == {"packet_id": old["packet_id"], "reviewer_id": "rev-o", "row_ids": row_ids(old)}
    assert packet_files(world, "rev-o") == []  # the packet and its pictures are gone from the shared store
    assert second.state.packets[old["packet_id"]].withdrawn and not second.state.packets[packets["rev-g"]["packet_id"]].withdrawn
    for row_id in row_ids(old):
        row = second.state.rows[row_id]
        assert row.status == "accepted" and row.withdrawals == 1
        assert [v["reviewer_id"] for v in row.verdicts] == ["rev-g"]  # the reviewer that was switched off never reviewed it
        assert row.families_used() == {"google"}
    new = [e.payload for e in world.events_of("packet_published") if e.payload["reviewer_id"] == "rev-g"]
    assert len(new) == 2 and sorted(row_ids(new[1])) == row_ids(old)  # the rows came back as a packet of their own
    assert {item["round"] for item in new[1]["items"]} == {1}  # still a first review
    assert len(world.shipped_rows()) == 4 and pictures_in_the_store(world) == []
    assert second.is_idle() and second.state.snapshot() == State.from_events(world.events()).snapshot()


def test_a_packet_for_a_reviewer_the_configuration_no_longer_names_is_withdrawn_the_same_way(tmp_path):
    world, _first, packets = published(tmp_path)
    world.config = review_config([GOOGLE])
    second = world.controller()
    world.drive(second, keep_all)
    (event,) = world.events_of("packet_withdrawn")
    assert event.payload["reviewer_id"] == "rev-o" and event.payload["row_ids"] == row_ids(packets["rev-o"])
    assert packet_files(world, "rev-o") == [] and len(world.shipped_rows()) == 4
    assert any("no longer in the configuration" in line for line in world.printed)


def test_the_run_says_which_packets_it_withdrew_and_why(tmp_path):
    world, _first, _packets = published(tmp_path)
    world.config = review_config([off(OPENAI), GOOGLE])
    world.drive(world.controller(), keep_all)
    (line,) = [line for line in world.printed if "withdrew" in line]
    assert "1 packet(s) with 2 row(s)" in line and "rev-o" in line and "switched off" in line


def test_a_reviewer_that_is_on_keeps_its_packets(tmp_path):
    world, _first, packets = published(tmp_path)
    world.config = review_config([OPENAI, GOOGLE, WRITER])  # a reviewer was added; the two that hold packets are on
    second = world.controller()
    world.drive(second, keep_all)
    assert world.events_of("packet_withdrawn") == [] and world.events_of("row_reopened") == []
    assert [e.payload["packet_id"] for e in world.events_of("packet_published")] == [p["packet_id"] for p in packets.values()]
    assert len(world.shipped_rows()) == 4 and not any("withdrew" in line for line in world.printed)


def test_a_reviewer_whose_verdict_file_exists_is_never_withdrawn(tmp_path):
    world, _first, packets = published(tmp_path)
    old = packets["rev-o"]
    world.run_reviewers(keep_all, only="rev-o")  # rev-o pushes before the controller has read anything
    assert layout.verdicts_path("rev-o", old["packet_id"]) in world.state_store.list("review/")
    world.config = review_config([off(OPENAI), GOOGLE])  # and is switched off before the controller reads it
    second = world.controller()
    world.drive(second, keep_all)
    assert world.events_of("packet_withdrawn") == []
    for row_id in row_ids(old):
        assert [v["reviewer_id"] for v in second.state.rows[row_id].verdicts] == ["rev-o"]
        assert second.state.rows[row_id].status == "accepted" and second.state.rows[row_id].withdrawals == 0
    assert len(world.shipped_rows()) == 4


def test_a_packet_with_a_partly_answered_verdict_file_stays_with_its_reviewer(tmp_path):
    world, _first, packets = published(tmp_path)
    old = packets["rev-o"]
    answered, waiting = row_ids(old)
    path = layout.verdicts_path("rev-o", old["packet_id"])
    world.state_store.write_text(path, json.dumps({"row_id": answered, "verdict": "keep"}) + "\n")  # a hand-written file
    world.config = review_config([off(OPENAI), GOOGLE])
    second = world.controller()
    for _ in range(4):
        second.step()
    assert world.events_of("packet_withdrawn") == []
    assert second.state.rows[answered].status == "accepted"
    assert second.state.rows[waiting].status == "in_review"  # still waiting for rev-o
    assert world.state_store.exists(layout.packet_json_path("rev-o", old["packet_id"]))


def test_a_verdict_file_that_turns_up_for_a_withdrawn_packet_is_ignored_and_noted_once(tmp_path):
    world, _first, packets = published(tmp_path)
    old = packets["rev-o"]
    client = world.reviewer_client("rev-o")
    local, _missing = client.pull()  # rev-o copied the packet before it was switched off
    assert local.packet_id == old["packet_id"]
    world.config = review_config([off(OPENAI), GOOGLE])
    second = world.controller()
    second.step()
    assert len(world.events_of("packet_withdrawn")) == 1
    assert [p for p in second.problems if "withdrawn" in p] == []  # nothing has turned up yet
    result = client.run_packet(old["packet_id"], lambda item: {"verdict": "reject", "reasons": ["late"], "notes": "too late"})
    assert result.pushed
    late = layout.verdicts_path("rev-o", old["packet_id"])
    assert late in world.state_store.list("review/")
    world.drive(second, keep_all)
    notes = [p for p in second.problems if "withdrawn" in p]
    assert len(notes) == 1 and late in notes[0] and "ignored" in notes[0]
    for row_id in row_ids(old):
        row = second.state.rows[row_id]
        assert row.status == "accepted" and [v["verdict"] for v in row.verdicts] == ["keep"]
        assert [v["reviewer_id"] for v in row.verdicts] == ["rev-g"]
    assert second.is_idle()


def test_rows_wait_when_every_reviewer_is_switched_off_and_a_reviewer_switched_on_again_gets_packets_of_a_new_id(tmp_path):
    world, _first, _packets = published(tmp_path)
    withdrawn_ids = {e.payload["packet_id"] for e in world.events_of("packet_published")}
    world.config = review_config([off(OPENAI), off(GOOGLE)])
    second = world.controller()
    summary = second.run()
    assert summary.reason == "idle"
    assert len(world.events_of("packet_withdrawn")) == 2
    assert second.state.counts()["rows"] == {"passed": 4} and world.events_of("row_to_hand") == []
    assert second.state.open_packets() == [] and world.state_store.list("packets/") == []
    world.config = review_config([OPENAI, GOOGLE])
    third = world.controller()
    world.drive(third, keep_all)
    again = [e.payload for e in world.events_of("packet_published")][2:]
    assert {p["reviewer_id"] for p in again} == {"rev-o", "rev-g"}
    assert not {p["packet_id"] for p in again} & withdrawn_ids  # same reviewers, same rows: packets of their own
    assert third.state.counts()["rows"] == {"accepted": 4} and {r.withdrawals for r in third.state.rows.values()} == {1}
    assert len(world.shipped_rows()) == 4 and pictures_in_the_store(world) == []
    assert third.state.snapshot() == State.from_events(world.events()).snapshot()


def test_a_row_that_loses_its_further_reviewer_keeps_the_review_it_had(tmp_path):
    # rev-a rejects the row, the row goes on to another reviewer, and that reviewer is switched off before it answers
    reviewers = [{"id": f"rev-{name}", "family": family} for name, family in (("a", "alpha"), ("b", "beta"), ("c", "gamma"), ("d", "delta"))]
    world = World(tmp_path, config=review_config(reviewers))
    first = world.controller()
    first.add_task(task_spec(("001",)))
    first.step()
    first.step()
    (row,) = first.state.rows.values()
    (opening,) = world.events_of("packet_published")
    assert opening.payload["reviewer_id"] == "rev-a"
    reject = {"verdict": "reject", "reasons": ["needs_work"], "notes": "thought 1"}
    world.run_reviewers(lambda reviewer, item: reject, only="rev-a")
    first.step()  # the reject is read and the row goes on to a further reviewer
    (further,) = [e.payload for e in world.events_of("packet_published") if e.payload["reviewer_id"] != "rev-a"]
    assert row.status == "in_review" and len(row.assignments) == 2
    gone = further["reviewer_id"]
    world.config = review_config([off(r) if r["id"] == gone else r for r in reviewers])
    second = world.controller()
    world.drive(second, keep_all)
    (event,) = world.events_of("packet_withdrawn")
    assert event.payload == {"packet_id": further["packet_id"], "reviewer_id": gone, "row_ids": [row.row_id]}
    again = second.state.rows[row.row_id]
    assert [(v["reviewer_id"], v["verdict"]) for v in again.verdicts][0] == ("rev-a", "reject")  # the review it had stays
    assert gone not in {v["reviewer_id"] for v in again.verdicts}
    assert again.status == "accepted" and [v["verdict"] for v in again.verdicts] == ["reject", "keep", "keep"]
    # the family of the reviewer that was switched off had given no review, so it counts as unused: the three
    # reviews came from the three reviewers that were on
    assert {v["family"] for v in again.verdicts} == {r["family"] for r in reviewers if r["id"] != gone}


def test_the_family_of_a_withdrawn_reviewer_is_free_again_for_another_reviewer_of_that_family(tmp_path):
    alpha = {"id": "rev-a", "family": "alpha"}
    beta = {"id": "rev-b", "family": "beta"}
    world = World(tmp_path, config=review_config([alpha, beta]))
    first = world.controller()
    first.add_task(task_spec(("001",)))
    first.step()
    first.step()
    reject = {"verdict": "reject", "reasons": ["needs_work"], "notes": "thought 1"}
    world.run_reviewers(lambda reviewer, item: reject, only="rev-a")
    first.step()  # rev-a's reject is read, and the row goes to the one family left, beta
    (further,) = [e.payload for e in world.events_of("packet_published") if e.payload["reviewer_id"] == "rev-b"]
    # rev-b is switched off before it answers, and another reviewer of its family is on
    world.config = review_config([alpha, off(beta), {"id": "rev-b2", "family": "beta"}])
    second = world.controller()
    world.drive(second, lambda reviewer, item: reject)
    (event,) = world.events_of("packet_withdrawn")
    assert event.payload["packet_id"] == further["packet_id"] and event.payload["reviewer_id"] == "rev-b"
    (row,) = second.state.rows.values()
    assert [(v["reviewer_id"], v["verdict"]) for v in row.verdicts] == [("rev-a", "reject"), ("rev-b2", "reject")]
    assert row.status == "rejected"  # the withdrawn reviewer never reviewed the row, so its family was still to be asked


def test_a_withdrawn_audit_is_given_to_another_reviewer(tmp_path):
    deepseek = {"id": "rev-d", "family": "deepseek"}
    reviewers = [OPENAI, GOOGLE, deepseek]
    world = World(tmp_path, config=review_config(reviewers, policy={"audit_fraction": 1.0}))
    first = world.controller()
    first.add_task(task_spec(("001",), image="p.jpg"))
    first.step()
    first.step()  # the row is written, assessed and out with its first reviewer, rev-o
    world.run_reviewers(keep_all)
    first.step()  # that keep accepts the row, and its audit goes out to a reviewer of another family
    (audit,) = [e.payload for e in world.events_of("packet_published") if e.payload["items"][0]["audit"]]
    auditor = audit["reviewer_id"]
    assert auditor in ("rev-g", "rev-d")
    other = "rev-d" if auditor == "rev-g" else "rev-g"
    world.config = review_config([off(r) if r["id"] == auditor else r for r in reviewers], policy={"audit_fraction": 1.0})
    second = world.controller()
    world.drive(second, keep_all)
    (event,) = world.events_of("packet_withdrawn")
    assert event.payload["packet_id"] == audit["packet_id"] and event.payload["reviewer_id"] == auditor
    (row,) = second.state.rows.values()
    assert row.status == "accepted" and row.withdrawals == 1
    assert [(v["reviewer_id"], v["audit"]) for v in row.verdicts] == [("rev-o", False), (other, True)]
    assert len(world.shipped_rows()) == 1 and pictures_in_the_store(world) == []


def test_an_audit_nobody_else_can_take_is_withdrawn_and_the_row_stays_accepted(tmp_path):
    world = World(tmp_path, config=review_config([OPENAI, GOOGLE], policy={"audit_fraction": 1.0}))
    first = world.controller()
    first.add_task(task_spec(("001",), image="p.jpg"))
    first.step()
    first.step()
    world.run_reviewers(keep_all)
    first.step()
    (audit,) = [e.payload for e in world.events_of("packet_published") if e.payload["items"][0]["audit"]]
    assert audit["reviewer_id"] == "rev-g"  # the only family that had not seen the row
    world.config = review_config([OPENAI, off(GOOGLE)], policy={"audit_fraction": 1.0})
    second = world.controller()
    world.drive(second, keep_all)
    assert len(world.events_of("packet_withdrawn")) == 1
    (row,) = second.state.rows.values()
    assert row.status == "accepted" and [v["audit"] for v in row.verdicts] == [False]  # no audit could be given
    assert len(world.events_of("packet_published")) == 2 and len(world.shipped_rows()) == 1


def test_the_hand_queue_and_the_withdrawal_work_together(tmp_path):
    # Every reviewer that can take a first review is switched off and only the writer's family is on: the rows
    # that were out with a reviewer go to the hand queue for want of one.
    world, _first, _packets = published(tmp_path)
    world.config = review_config([off(OPENAI), off(GOOGLE), WRITER])
    second = world.controller()
    second.run()
    assert len(world.events_of("packet_withdrawn")) == 2
    assert [e.payload["reason"] for e in world.events_of("row_to_hand")] == [
        "no reviewer from another family than the writer is available"
    ] * 4
    assert len(second.state.hand_queue()) == 4 and world.state_store.list("packets/") == []
    # the owner answers one of them; a reviewer comes back for the others
    rows = sorted(second.state.rows.values(), key=lambda row: row.row_id)
    write_hand_decisions(world, [{"row_id": rows[0].row_id, "decision": "accept"}])
    world.config = review_config([OPENAI, GOOGLE, WRITER])
    third = world.controller()
    world.drive(third, keep_all)
    assert len(world.events_of("row_reopened")) == 3
    assert third.state.rows[rows[0].row_id].accepted_by == "hand"
    assert third.state.counts()["rows"] == {"accepted": 4} and len(world.shipped_rows()) == 4


# ------------------------------------------------------------------ resuming


def test_a_run_that_stopped_after_recording_a_withdrawal_deletes_the_files_on_the_next_one(tmp_path):
    world, _first, packets = published(tmp_path)
    old = packets["rev-o"]
    world.config = review_config([off(OPENAI), GOOGLE])
    crashing = world.controller(on_event=crash_on("packet_withdrawn"))
    with pytest.raises(SimulatedCrash):
        crashing.step()
    assert len(world.events_of("packet_withdrawn")) == 1
    assert packet_files(world, "rev-o", old["packet_id"])  # recorded, and the files are still there
    second = world.controller()
    world.drive(second, keep_all)
    assert packet_files(world, "rev-o") == [] and pictures_in_the_store(world) == []
    assert len(world.events_of("packet_withdrawn")) == 1
    assert len(world.shipped_rows()) == 4


def test_a_run_that_starts_again_deletes_the_files_of_every_packet_withdrawn_before(tmp_path):
    world, _first, packets = published(tmp_path)
    old = packets["rev-o"]
    world.config = review_config([off(OPENAI), GOOGLE])
    second = world.controller()
    second.step()
    # the files come back (a store that was slow to delete them, say), and a later run clears them again
    world.state_store.write_text(layout.packet_json_path("rev-o", old["packet_id"]), "{}")
    assert packet_files(world, "rev-o", old["packet_id"])
    third = world.controller()
    third.step()
    assert packet_files(world, "rev-o", old["packet_id"]) == []


def run_through_withdrawal_and_reopening(tmp_path, crash_at=None):
    """Packets are out, then every reviewer that can take them is off (the rows go to the hand queue), then they are back.

    The run that is killed after its `crash_at`-th event (counted over both stages) is started again with the
    same configuration. Returns the world, the last controller and the number of events the two stages wrote.
    """
    world, _first, _packets = published(tmp_path)
    before = len(world.events())
    hook = crash_after(crash_at) if crash_at else None
    controller = None
    for reviewers in ([off(OPENAI), off(GOOGLE), WRITER], [OPENAI, GOOGLE, WRITER]):
        world.config = review_config(reviewers)
        controller = world.controller(on_event=hook)
        try:
            world.drive(controller, keep_all)
        except SimulatedCrash:
            controller = world.controller()
            world.drive(controller, keep_all)
    return world, controller, len(world.events()) - before


def test_a_run_killed_after_any_event_of_a_withdrawal_and_a_reopening_ends_where_an_uninterrupted_one_does(tmp_path):
    reference, _controller, total = run_through_withdrawal_and_reopening(tmp_path / "reference")
    expected = outcome(reference)
    assert total > 20 and len(expected[1]) == 4
    for count in range(1, total):
        world, controller, _ = run_through_withdrawal_and_reopening(tmp_path / f"kill-{count}", crash_at=count)
        where = f"a run killed after event {count}"
        assert outcome(world) == expected, f"{where} ended differently"
        withdrawn = Counter(e.payload["packet_id"] for e in world.events_of("packet_withdrawn"))
        assert len(withdrawn) == 2 and set(withdrawn.values()) == {1}, where
        reopened = Counter(e.payload["row_id"] for e in world.events_of("row_reopened"))
        assert len(reopened) == 4 and set(reopened.values()) == {1}, where
        state = State.from_events(replay(world.state_store).events)
        assert controller.state.snapshot() == state.snapshot(), where
        for packet in state.withdrawn_packets():
            assert packet_files(world, packet.reviewer_id, packet.packet_id) == [], where
        assert pictures_in_the_store(world) == [], where
