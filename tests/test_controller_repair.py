"""The repair step through the controller: a row a reviewer sent back with a fix verdict is written again, once."""

import json

import pytest

from rps import ids, layout
from rps.assess import Assessment
from rps.images import ImageError
from rps.providers import FakeOutcome, SimulatedCrash
from rps.state import State

from harness import World, task_spec
from repair_helpers import (
    CAPS,
    FIX_REASON,
    THREE,
    gated_writer,
    repair_config,
    repair_route,
    sends_back,
    writes_and_rewrites,
)
from review_helpers import crash_on, write_hand_decisions

LINES = ("001", "002", "003", "004")
CHECKER_REASON = "the repair failed the checker: the row waits for the owner"


def drive_repairs(tmp_path, *, fix=("001", "003"), lines=LINES, config=None, verdict=None, behavior=writes_and_rewrites, **kw):
    world = World(tmp_path, config=config or repair_config(), behavior=behavior)
    controller = world.controller(**kw)
    controller.add_task(task_spec(lines))
    world.drive(controller, verdict or sends_back(*fix))
    return world, controller


def originals(controller):
    """The rows the first calls wrote, by spec line."""
    return {row.line: row for row in controller.state.rows.values() if row.repair_of is None}


def repaired(controller):
    """The rows repair calls wrote, by the id of the row they repair."""
    return {row.repair_of: row for row in controller.state.rows.values() if row.repair_of is not None}


def payloads(world, kind="call_prepared", **match):
    return [e.payload for e in world.events_of(kind) if all(e.payload.get(k) == v for k, v in match.items())]


def hand_queue_file(world):
    """The rows the controller listed for the owner; none when it never listed one (the file is not written empty)."""
    if not world.state_store.exists(layout.HAND_QUEUE_PATH):
        return []
    return [json.loads(line) for line in world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()]


def reviewers_of(controller, row_id):
    return [v["reviewer_id"] for v in controller.state.rows[row_id].verdicts]


def busy_route(tmp_path, *, min_rows, fix, lines=LINES):
    """A route kept busy by a call that is held, while the rows of an earlier task have been reviewed: their
    verdicts are in the store and the controller has not read them yet. Returns (world, controller, held)."""
    held = {"B01"}
    config = repair_config(min_rows=min_rows)
    world = World(tmp_path, config=config, behavior=writes_and_rewrites, writer_class=gated_writer(held), polls_until_ended=0)
    controller = world.controller()
    controller.add_task(task_spec(lines))
    for _ in range(10):
        controller.step()
        if controller.state.open_packets():
            break
    else:
        raise AssertionError("no packet was published")
    controller.add_task(task_spec(("B01",), batch="2"))
    controller.step()
    assert controller.state.unfinished_calls("fake-batch") == 1  # the held call: the route has other work
    world.run_reviewers(sends_back(*fix))
    return world, controller, held


# ------------------------------------------------------------------ the whole way


def test_a_row_with_a_fix_verdict_is_written_again_and_the_new_row_ships_like_any_row(tmp_path):
    world, controller = drive_repairs(tmp_path)
    state, first = controller.state, originals(controller)
    task = next(iter(state.tasks))
    sent = world.events_of("repair_requested")
    assert [e.payload for e in sent] == [
        {"row_id": first[line].row_id, "call_id": ids.call_id(task, line, 2), "route": "fake-batch"} for line in ("001", "003")
    ]
    assert list(sent[0].payload) == ["row_id", "call_id", "route"]
    again = repaired(controller)
    assert set(again) == {first["001"].row_id, first["003"].row_id}
    for line in ("001", "003"):
        new = again[first[line].row_id]
        assert first[line].status == "repaired" and first[line].repair_of is None
        assert new.row_id == ids.row_id(ids.call_id(task, line, 2), 0) and new.status == "accepted"
        assert new.accepted_by == "reviewers" and new.line == line and new.route == "fake-batch"
    assert [first[line].status for line in ("002", "004")] == ["accepted", "accepted"]
    assert state.counts()["rows"] == {"repaired": 2, "accepted": 4} and state.hand_queue() == []
    # the calls: one for each row, charged to repair, the second attempt of the line, linked to the row they write again
    prepared = payloads(world, purpose="repair")
    assert [(p["attempt"], p["repair_of"], p["retry_of"]) for p in prepared] == [(2, first[line].row_id, None) for line in ("001", "003")]
    assert {p["call_id"] for p in prepared} == {e.payload["call_id"] for e in sent}
    assert [p["repair_of"] for p in payloads(world, "call_returned") if "repair_of" in p] == [first["001"].row_id, first["003"].row_id]
    assert state.spend("fake-batch", "repair").calls == 2 and state.spend("fake-batch", "rebound").calls == 0
    assert state.spend("fake-batch", "production").calls == 4
    # what ships: the rows that were kept and the rows written again, never the rows that were sent back
    shipped = {row["row_id"]: row for row in world.shipped_rows()}
    assert set(shipped) == {first["002"].row_id, first["004"].row_id} | {again[first[line].row_id].row_id for line in ("001", "003")}
    plain = shipped[first["002"].row_id]
    for new in again.values():
        assert set(shipped[new.row_id]) == set(plain)  # no new column: the link is in the events and the state
        assert shipped[new.row_id]["thoughts"][0].startswith("repaired:")
        assert shipped[new.row_id]["writer"]["task_id"] == task and shipped[new.row_id]["source_label"]["item_id"].startswith("img-")
    assert not any("repair" in key for row in shipped.values() for key in row)
    summary = json.loads(world.ship_store.read_text("ledger/summary.json"))["routes"]["fake-batch"]["purposes"]
    assert summary["repair"]["calls"] == 2 and summary["production"]["calls"] == 4 and summary["rebound"]["calls"] == 0
    # the rows were sent back in the pass that put them in the queue, so the owner was never shown them
    assert controller.is_idle() and hand_queue_file(world) == []
    assert controller.state.snapshot() == State.from_events(world.events()).snapshot()


def test_nothing_is_repaired_twice_and_a_run_that_has_finished_has_nothing_more_to_do(tmp_path):
    world, controller = drive_repairs(tmp_path)
    before = len(world.events())
    for _ in range(4):
        assert controller.step() == 0
    assert len(world.events()) == before
    assert len(world.events_of("repair_requested")) == 2 and len(payloads(world, purpose="repair")) == 2


# ------------------------------------------------------------------ what the writer is told


def test_the_writer_is_told_the_notes_and_shown_the_row_as_written_and_never_told_who_wrote_the_notes(tmp_path):
    world, controller = drive_repairs(tmp_path, fix=("001",), lines=("001", "002"))
    (new,) = [r for r in controller.state.rows.values() if r.repair_of is not None]
    first = controller.state.rows[new.repair_of]
    requests = [p for batch in world.provider.batches.values() for _call, _custom, p in batch["requests"]]
    (request,) = [p for p in requests if p["repair_of"]]
    (note,) = [v["notes"] for v in first.verdicts]
    assert request["repair_of"] == first.row_id and request["attempt"] == 2 and request["spec"] == "spec for 001"
    assert request["feedback"][:-1] == [note] and note == "line 001: thought 1 states a pose that no record gives"
    written = json.loads(request["feedback"][-1])
    assert written == {key: value for key, value in first.record.items() if key not in ("row_id", "writer", "source_label")}
    assert written == {"id": "001-1", "thoughts": ["a thought about spec for 001"]}
    # the feedback in the log is the feedback the writer got
    (prepared,) = payloads(world, purpose="repair")
    assert prepared["feedback"] == request["feedback"]
    for who in ("rev-a", "rev-b", "rev-c", "alpha", "beta", "gamma", "writer-family", first.row_id):
        assert not any(who in item for item in request["feedback"]) and who not in request["task_text"]
    # the text of the call is the task, then the words of the repair
    text = request["task_text"]
    assert text.startswith("spec for 001\n\nAn earlier row for this item was sent back by a reviewer.")
    assert f"- {note}" in text and request["feedback"][-1] in text and "failed these checks" not in text
    # a row that was never sent back is written from the task text alone
    (plain,) = [p for p in requests if p["line"] == "002"]
    assert plain["task_text"] == "spec for 002" and plain["feedback"] == [] and plain["repair_of"] is None


def test_the_notes_of_every_fix_verdict_on_the_row_go_with_it(tmp_path):
    # a second reviewer was asked about the row before the policy handed a fix to the owner: the row has two fixes
    reviewers = THREE[:2]
    world = World(tmp_path, config=repair_config(reviewers, enabled=False, policy={"fix_to_hand": False}), behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))

    def says(reviewer, item):
        if item.row["thoughts"][0].startswith("repaired"):
            return {"verdict": "keep"}
        return {"verdict": "fix", "reasons": ["needs_work"], "notes": f"{reviewer.family} found: {item.task['line_id']} has a problem"}

    controller.step()
    controller.step()
    world.run_reviewers(says, only="rev-a")
    controller.step()  # the first fix: it is not handed over, so the row goes to a second reviewer
    assert [v["reviewer_id"] for v in next(iter(controller.state.rows.values())).verdicts] == ["rev-a"]
    world.config = repair_config(reviewers, policy={"fix_to_hand": True})
    second = world.controller()
    world.drive(second, says)
    row = next(r for r in second.state.rows.values() if r.repair_of is None)
    assert [v["verdict"] for v in row.verdicts] == ["fix", "fix"] and row.status == "repaired"
    (prepared,) = payloads(world, purpose="repair")
    assert prepared["feedback"][:-1] == ["alpha found: 001 has a problem", "beta found: 001 has a problem"]
    assert json.loads(prepared["feedback"][-1])["id"] == "001-1"


def test_a_repair_call_carries_the_picture_of_its_line_like_any_call(tmp_path):
    world = World(tmp_path, config=repair_config(), behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002"), image="p.jpg"))
    world.drive(controller, sends_back("001"))
    repair_calls = [p for batch in world.provider.batches.values() for _c, _x, p in batch["requests"] if p["repair_of"]]
    assert len(repair_calls) == 1 and repair_calls[0]["has_image"] is True
    assert len(world.shipped_rows()) == 2 and controller.state.counts()["rows"] == {"repaired": 1, "accepted": 2}


# ------------------------------------------------------------------ which rows


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(repair_config(enabled=False), id="the step is off"),
        pytest.param(repair_config(caps={k: v for k, v in CAPS.items() if k != "repair"}), id="the route has no cap for repair"),
        pytest.param(repair_config(caps={**CAPS, "repair": 0}), id="the cap for repair is nothing"),
    ],
)
def test_rows_stay_in_the_hand_queue_for_the_owner_unless_the_step_is_on_and_the_route_has_a_repair_cap(tmp_path, config):
    world, controller = drive_repairs(tmp_path, config=config)
    assert world.events_of("repair_requested") == [] and payloads(world, purpose="repair") == []
    rows = originals(controller)
    assert {line: rows[line].status for line in LINES} == {"001": "hand", "002": "accepted", "003": "hand", "004": "accepted"}
    assert {rows[line].hand["reason"] for line in ("001", "003")} == {FIX_REASON}
    assert {entry["row_id"] for entry in hand_queue_file(world)} == {rows["001"].row_id, rows["003"].row_id}
    assert len(world.shipped_rows()) == 2 and controller.is_idle()
    assert all("repair_of" not in p for p in payloads(world))  # a run without the step is what it always was


def test_a_route_with_no_repair_cap_is_noted_when_the_run_starts_and_no_call_is_charged_to_repair(tmp_path):
    config = repair_config(caps={k: v for k, v in CAPS.items() if k != "repair"})
    world, controller = drive_repairs(tmp_path, config=config)
    assert "[rps] note: route fake-batch: no cap for purpose 'repair' (calls with that purpose are refused)" in world.printed
    assert controller.state.spend("fake-batch", "repair").calls == 0 and controller.state.counts()["refused_calls"] == 0


def test_a_run_with_the_step_off_prints_no_note_about_a_repair_cap(tmp_path):
    world, _controller = drive_repairs(tmp_path, config=repair_config(enabled=False))
    assert not any("repair" in line for line in world.printed if line.startswith("[rps] note:"))


def test_a_route_that_is_switched_off_in_the_configuration_is_not_asked_to_repair(tmp_path):
    world, first = drive_repairs(tmp_path, config=repair_config(enabled=False))
    waiting = sorted(row.row_id for row in first.state.hand_queue())
    assert len(waiting) == 2
    world.config = repair_config(route=repair_route(enabled=False))
    second = world.controller()
    for _ in range(3):
        second.step()
    assert world.events_of("repair_requested") == [] and sorted(r.row_id for r in second.state.hand_queue()) == waiting


def test_a_row_in_the_hand_queue_for_another_reason_is_never_sent_back(tmp_path):
    # three reviewers could not settle the rows (a keep, a fix and a reject): they wait for the owner, not for a repair
    def verdict(reviewer, item):
        if reviewer.family == "alpha":
            return {"verdict": "keep"}
        return {"verdict": "fix" if reviewer.family == "beta" else "reject", "reasons": ["needs_work"], "notes": "n"}

    world = World(tmp_path, config=repair_config(policy={"fix_to_hand": False}), behavior=writes_and_rewrites)
    controller = world.controller(assess=lambda row, context: Assessment(passed=True, flags=("needs_a_look",)))
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller, verdict)
    assert all(row.status == "hand" and "3 reviewers without a settlement" in row.hand["reason"] for row in controller.state.rows.values())
    for _ in range(3):
        controller.step()
    assert world.events_of("repair_requested") == [] and len(controller.state.hand_queue()) == 2


def test_a_row_the_owner_answered_before_the_pass_that_would_send_it_back_is_never_sent_back(tmp_path):
    world = World(tmp_path, config=repair_config(), behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(LINES))
    task = next(iter(controller.state.tasks))
    first = lambda line: ids.row_id(ids.call_id(task, line, 1), 0)
    # the owner's answers are in the store before the rows reach the queue; they are read in the pass that puts the rows there
    write_hand_decisions(world, [{"row_id": first("001"), "decision": "accept"}, {"row_id": first("003"), "decision": "discard"}])
    world.drive(controller, sends_back("001", "003"))
    rows = originals(controller)
    assert rows["001"].status == "accepted" and rows["001"].accepted_by == "hand"
    assert rows["003"].status == "rejected" and rows["003"].rejection["by"] == "hand"
    assert world.events_of("repair_requested") == [] and payloads(world, purpose="repair") == []
    assert {row["row_id"] for row in world.shipped_rows()} == {first("001"), first("002"), first("004")}


def test_a_row_the_owner_answers_while_it_waits_for_its_batch_is_not_repaired_and_the_other_is(tmp_path):
    world, controller, held = busy_route(tmp_path, min_rows=3, fix=("001", "003"))
    controller.step()
    first = originals(controller)
    assert {first[line].status for line in ("001", "003")} == {"hand"}  # two rows wait: the batch is not full, the route busy
    assert world.events_of("repair_requested") == []
    write_hand_decisions(world, [{"row_id": first["001"].row_id, "decision": "accept"}])
    controller.step()
    assert first["001"].status == "accepted" and first["003"].status == "hand" and world.events_of("repair_requested") == []
    held.clear()
    world.drive(controller, sends_back("001", "003"))
    assert [e.payload["row_id"] for e in world.events_of("repair_requested")] == [first["003"].row_id]
    assert first["001"].status == "accepted" and first["001"].accepted_by == "hand" and first["003"].status == "repaired"


# ------------------------------------------------------------------ in batches


def test_repairs_wait_for_min_rows_while_the_route_has_other_work_and_go_when_it_has_none(tmp_path):
    world, controller, held = busy_route(tmp_path, min_rows=3, fix=("001", "003"))
    controller.step()
    state = controller.state
    assert len(state.hand_queue()) == 2  # two rows wait for the owner: the batch is not full and the route is busy
    assert world.events_of("repair_requested") == [] and payloads(world, purpose="repair") == []
    assert {row["row_id"] for row in hand_queue_file(world)} == {row.row_id for row in state.hand_queue()}
    for _ in range(2):
        controller.step()
    assert world.events_of("repair_requested") == []
    held.clear()  # the route has nothing else to do once its last call is in: the smaller batch goes
    controller.step()
    assert len(world.events_of("repair_requested")) == 2 and state.hand_queue() == []
    assert any("sent 2 row(s) back to fake-batch" in line for line in world.printed)
    world.drive(controller, sends_back("001", "003"))
    assert len(payloads(world, purpose="repair")) == 2 and len(world.shipped_rows()) == 5


def test_repairs_go_together_as_soon_as_min_rows_are_gathered_even_when_the_route_is_busy(tmp_path):
    world, controller, held = busy_route(tmp_path, min_rows=2, fix=("001", "003"))
    controller.step()
    assert len(world.events_of("repair_requested")) == 2 and controller.state.hand_queue() == []  # the held call does not hold them back
    controller.step()
    prepared = payloads(world, purpose="repair")
    assert len(prepared) == 2  # both calls are prepared in the same pass...
    handles = {e.payload["handle_id"] for e in world.events_of("batch_submitted") if set(e.payload["call_ids"]) & {p["call_id"] for p in prepared}}
    assert len(handles) == 1  # ...and go out as one batch
    held.clear()
    world.drive(controller, sends_back("001", "003"))
    assert len(world.shipped_rows()) == 5


def test_more_rows_than_min_rows_all_go_in_the_same_batch(tmp_path):
    world, controller, held = busy_route(tmp_path, min_rows=2, fix=("001", "002", "003"))
    controller.step()
    assert len(world.events_of("repair_requested")) == 3  # every candidate, not only min_rows of them
    held.clear()
    world.drive(controller, sends_back("001", "002", "003"))
    # five rows were written first (001-004 and the held one): three sent back and written again, two kept
    assert controller.state.counts()["rows"] == {"repaired": 3, "accepted": 2 + 3}


def test_with_nothing_else_to_do_a_single_row_is_not_left_waiting_for_a_batch_that_will_never_fill(tmp_path):
    world, controller = drive_repairs(tmp_path, config=repair_config(min_rows=20), fix=("002",), lines=("001", "002", "003"))
    assert len(world.events_of("repair_requested")) == 1
    assert controller.state.counts()["rows"] == {"repaired": 1, "accepted": 3} and len(world.shipped_rows()) == 3


def test_a_route_has_nothing_else_to_do_only_when_no_call_and_no_unchecked_row_of_its_own_is_left(tmp_path):
    world, controller, held = busy_route(tmp_path, min_rows=5, fix=("001",))
    assert controller._route_has_nothing_else_to_do("fake-batch") is False  # a call in flight
    controller.step()
    assert len(controller.state.hand_queue()) == 1 and world.events_of("repair_requested") == []  # one row, 5 wanted, route busy
    held.clear()
    controller.step()  # the held call comes back: nothing else is left for the route, and the one row goes
    assert len(world.events_of("repair_requested")) == 1
    assert controller._route_has_nothing_else_to_do("fake-batch") is False  # its call is due
    world.drive(controller, sends_back("001"))
    assert controller._route_has_nothing_else_to_do("fake-batch") is True


def two_routes(min_rows):
    other = repair_route()
    return repair_config(min_rows=min_rows, routes={"fake-batch": repair_route(), "other-batch": other})


def test_each_route_is_batched_on_its_own(tmp_path):
    held = {"B01"}
    config = two_routes(min_rows=3)
    world = World(tmp_path, config=config, behavior=writes_and_rewrites, writer_class=gated_writer(held), polls_until_ended=0)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003")))  # on fake-batch: two of its rows will be sent back
    controller.add_task(task_spec(("011", "012"), batch="3", route="other-batch"))  # on other-batch: one will be
    for _ in range(10):
        controller.step()
        if len(controller.state.open_packets()) >= 2:
            break
    controller.add_task(task_spec(("B01",), batch="2"))  # keeps fake-batch busy, and nothing keeps other-batch busy
    controller.step()
    world.run_reviewers(sends_back("001", "002", "011"))
    controller.step()
    sent = {e.payload["route"]: e.payload["row_id"] for e in world.events_of("repair_requested")}
    other = next(r for r in controller.state.rows.values() if r.route == "other-batch" and r.line == "011")
    assert sent == {"other-batch": other.row_id}  # one row is fewer than 3, and nothing else is left to do for that route
    assert len([r for r in controller.state.hand_queue() if r.route == "fake-batch"]) == 2  # the busy route keeps gathering
    held.clear()
    world.drive(controller, sends_back("001", "002", "011"))
    assert sorted(e.payload["route"] for e in world.events_of("repair_requested")) == ["fake-batch", "fake-batch", "other-batch"]
    assert {p["route"] for p in payloads(world, purpose="repair")} == {"fake-batch", "other-batch"}
    assert controller.state.spend("other-batch", "repair").calls == 1 and controller.state.spend("fake-batch", "repair").calls == 2


def test_a_route_without_a_repair_cap_is_left_out_while_another_route_is_repaired(tmp_path):
    config = repair_config(routes={"fake-batch": repair_route(), "other-batch": repair_route({k: v for k, v in CAPS.items() if k != "repair"})})
    world = World(tmp_path, config=config, behavior=writes_and_rewrites, polls_until_ended=0)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    controller.add_task(task_spec(("011", "012"), batch="3", route="other-batch"))
    world.drive(controller, sends_back("001", "011"))
    assert [e.payload["route"] for e in world.events_of("repair_requested")] == ["fake-batch"]
    assert [r.line for r in controller.state.hand_queue()] == ["011"] and controller.state.hand_queue()[0].route == "other-batch"


# ------------------------------------------------------------------ spend


def test_a_repair_call_the_cap_cannot_hold_is_refused_and_asked_again_by_the_next_run(tmp_path):
    config = repair_config(caps={**CAPS, "repair": 0.001})  # a call is held at about $0.0046
    world, controller = drive_repairs(tmp_path, config=config)
    assert len(world.events_of("repair_requested")) == 2  # the rows were sent back; the cap decides about the calls
    refused = world.events_of("budget_refused")
    assert len(refused) == 2 and {e.payload["purpose"] for e in refused} == {"repair"}
    assert {e.payload["reason"] for e in refused} == {"purpose_cap"} and refused[0].payload["purpose_cap"] == 0.001
    assert payloads(world, purpose="repair") == [] and controller.state.spend("fake-batch", "repair").calls == 0
    assert controller.state.counts()["rows"] == {"repairing": 2, "accepted": 2} and controller.state.hand_queue() == []
    status = controller.format_status()
    assert controller.is_idle() and "2 by the repair cap of fake-batch ($0.0010)" in status
    assert "rows sent back to their writers: 2 being written again, 0 written again" in status
    # a later run with room for them writes the rows again
    world.config = repair_config()
    second = world.controller()
    world.drive(second, sends_back("001", "003"))
    assert second.state.counts()["rows"] == {"repaired": 2, "accepted": 4} and len(world.shipped_rows()) == 4
    assert second.state.spend("fake-batch", "repair").calls == 2


def test_repairs_draw_on_their_own_cap_and_never_on_rebound(tmp_path):
    # room for one repair call only: the second waits for the first to give back what it holds, then is refused for good
    config = repair_config(caps={**CAPS, "repair": 0.005, "rebound": 0.0})
    world, controller = drive_repairs(tmp_path, config=config)
    state = controller.state
    assert state.spend("fake-batch", "repair").calls == 1 and state.spend("fake-batch", "rebound").calls == 0
    assert sorted(row.status for row in state.rows.values()) == ["accepted"] * 3 + ["repaired", "repairing"]
    (refusal,) = world.events_of("budget_refused")
    assert refusal.payload["purpose"] == "repair" and refusal.payload["purpose_spent"] == pytest.approx(0.0007)
    assert state.spend("fake-batch", "repair").actual == pytest.approx(0.0007) and state.spend("fake-batch").total < 10


# ------------------------------------------------------------------ the repaired row and the checker


def fails_repaired_rows(row, context):
    """A checker that rejects any row that was written again (its thought starts with "repaired")."""
    if row["thoughts"][0].startswith("repaired"):
        return Assessment(passed=False, errors=("thought 1 is too long",))
    return Assessment(passed=True)


def test_a_repaired_row_that_fails_the_checker_is_not_written_a_third_time_and_the_row_goes_back_to_the_owner(tmp_path):
    config = repair_config(max_attempts=3)  # a line's own attempts would allow a retry; a repair gets none
    world, controller = drive_repairs(tmp_path, config=config, assess=fails_repaired_rows)
    state, first = controller.state, originals(controller)
    again = repaired(controller)
    for line in ("001", "003"):
        new = again[first[line].row_id]
        assert new.status == "rejected" and new.rejection == {
            "by": "assessor", "reason": "failed assessment", "errors": ["thought 1 is too long"], "retry": False
        }
        row = first[line]
        assert row.status == "hand" and row.hand["reason"] == CHECKER_REASON and row.repair["failed"] == CHECKER_REASON
        assert row.repair["rows"] == [new.row_id] and row.repair["calls"] == [new.call_id]
    assert len(payloads(world)) == 6 and state.spend("fake-batch", "repair").calls == 2  # four lines, two repairs, nothing more
    assert [p["retry"] for p in payloads(world, "row_rejected", by="assessor")] == [False, False]
    assert len(world.events_of("repair_requested")) == 2 and state.spend("fake-batch", "rebound").calls == 0
    to_hand = world.events_of("row_to_hand")
    assert [e.payload["reason"] for e in to_hand] == [FIX_REASON] * 2 + [CHECKER_REASON] * 2
    assert [e.payload["row_id"] for e in to_hand[2:]] == [e.payload["row_id"] for e in to_hand[:2]]
    assert [e.payload["verdicts"] for e in to_hand[2:]] == [e.payload["verdicts"] for e in to_hand[:2]]  # the verdicts it had
    assert {line: first[line].status for line in LINES} == {"001": "hand", "002": "accepted", "003": "hand", "004": "accepted"}
    # the owner sees the row it was, its reviewer's words, why it is there again, and what the writer wrote
    queue = {entry["row_id"]: entry for entry in hand_queue_file(world)}
    assert set(queue) == {first["001"].row_id, first["003"].row_id}
    entry = queue[first["001"].row_id]
    assert entry["reason"] == CHECKER_REASON and [v["verdict"] for v in entry["verdicts"]] == ["fix"]
    assert entry["repair"] == {"calls": [again[first["001"].row_id].call_id], "rows": [again[first["001"].row_id].row_id]}
    assert "repair_of" not in entry and entry["row"]["row_id"] == first["001"].row_id
    assert {r["row_id"] for r in world.shipped_rows()} == {first["002"].row_id, first["004"].row_id}
    assert controller.is_idle()
    for _ in range(3):
        assert controller.step() == 0  # it is not sent back again
    assert len(world.events_of("repair_requested")) == 2


def test_the_owner_can_still_answer_a_row_whose_repair_failed_the_checker(tmp_path):
    world, controller = drive_repairs(tmp_path, assess=fails_repaired_rows, fix=("001",), lines=("001", "002"))
    first = originals(controller)
    write_hand_decisions(world, [{"row_id": first["001"].row_id, "decision": "accept"}])
    controller.step()
    controller.step()
    assert first["001"].status == "accepted" and first["001"].accepted_by == "hand"
    assert {row["row_id"] for row in world.shipped_rows()} == {first["001"].row_id, first["002"].row_id}


def test_a_repaired_row_that_passes_the_checker_goes_to_review_like_any_row_and_never_shows_what_it_replaces(tmp_path):
    world, controller = drive_repairs(tmp_path, fix=("001",), lines=("001", "002"))
    (new,) = [r for r in controller.state.rows.values() if r.repair_of is not None]
    assessed = [e.payload for e in world.events_of("row_assessed") if e.payload["row_id"] == new.row_id]
    assert [(p["passed"], p["errors"]) for p in assessed] == [(True, [])]
    (verdict,) = new.verdicts
    assert verdict["verdict"] == "keep" and verdict["round"] == 1 and new.status == "accepted"
    # the packet is the blind view of any row: no writer, no link to the row it replaces
    packet = next(e.payload for e in world.events_of("packet_published") if any(i["row_id"] == new.row_id for i in e.payload["items"]))
    text = world.state_store.read_text(layout.packet_json_path(packet["reviewer_id"], packet["packet_id"]))
    entry = next(item for item in json.loads(text)["rows"] if item["row_id"] == new.row_id)
    assert "writer" not in entry["row"] and "repair_of" not in text and new.repair_of not in text
    assert entry["task"]["spec"] == "spec for 001"  # the same spec line, the same task


# ------------------------------------------------------------------ the first review


def test_the_first_review_of_a_row_written_again_goes_to_a_reviewer_that_did_not_review_the_row_it_replaces(tmp_path):
    world, controller = drive_repairs(tmp_path, lines=("001", "002", "003", "004", "005", "006"), fix=("001", "002", "003", "004"))
    again = repaired(controller)
    assert len(again) == 4
    for original_id, new in again.items():
        old = reviewers_of(controller, original_id)
        assert len(old) == 1 and len(reviewers_of(controller, new.row_id)) == 1
        assert reviewers_of(controller, new.row_id)[0] not in old
        assert new.verdicts[0]["family"] != "writer-family" and new.assignments[0].round == 1


def test_the_first_review_goes_to_the_same_reviewer_when_there_is_no_other_outside_the_writers_family(tmp_path):
    only = [THREE[0]]
    world, controller = drive_repairs(tmp_path, config=repair_config(only), fix=("001",), lines=("001", "002"))
    ((original_id, new),) = repaired(controller).items()
    assert reviewers_of(controller, original_id) == ["rev-a"] and reviewers_of(controller, new.row_id) == ["rev-a"]
    # a reviewer of the writer's own family is never given the row, whoever reviewed the one it replaces
    mixed = [{"id": "rev-w", "family": "writer-family"}, THREE[1]]
    world, controller = drive_repairs(tmp_path / "mixed", config=repair_config(mixed), fix=("001",), lines=("001", "002"))
    ((original_id, new),) = repaired(controller).items()
    assert reviewers_of(controller, original_id) == ["rev-b"] and reviewers_of(controller, new.row_id) == ["rev-b"]
    assert {e.payload["reviewer_id"] for e in world.events_of("packet_published")} == {"rev-b"}


# ------------------------------------------------------------------ once only


def test_a_row_that_was_written_again_and_is_sent_back_again_goes_to_the_owner_and_is_never_repaired_again(tmp_path):
    world, controller = drive_repairs(tmp_path, verdict=sends_back("001", "003", repaired="fix"))
    state, first = controller.state, originals(controller)
    again = repaired(controller)
    assert {row.status for row in again.values()} == {"hand"}
    assert {row.hand["reason"] for row in again.values()} == {FIX_REASON}  # it waits for the owner, as any fix does
    assert [first[line].status for line in LINES] == ["repaired", "accepted", "repaired", "accepted"]
    assert len(world.events_of("repair_requested")) == 2 and len(payloads(world, purpose="repair")) == 2
    queue = {entry["row_id"]: entry for entry in hand_queue_file(world)}
    assert set(queue) == {row.row_id for row in again.values()}
    for original_id, new in again.items():
        assert queue[new.row_id]["repair_of"] == original_id and "repair" not in queue[new.row_id]
        assert queue[new.row_id]["reason"] == FIX_REASON
    for _ in range(4):
        assert controller.step() == 0
    assert len(world.events_of("repair_requested")) == 2
    assert sorted(r.row_id for r in state.hand_queue()) == sorted(r.row_id for r in again.values())
    assert len(world.shipped_rows()) == 2  # only the rows that were kept
    # the owner answers the new row like any row in the queue
    new = next(iter(again.values()))
    write_hand_decisions(world, [{"row_id": new.row_id, "decision": "accept"}])
    controller.step()
    controller.step()
    assert new.status == "accepted" and new.accepted_by == "hand" and len(world.events_of("repair_requested")) == 2
    assert new.row_id in {row["row_id"] for row in world.shipped_rows()}


# ------------------------------------------------------------------ a call that fails


def test_a_repair_call_that_failed_for_a_passing_reason_is_made_again_with_the_same_notes_and_row(tmp_path):
    def behavior(payload):
        if payload["repair_of"] and payload["attempt"] == 2:
            return FakeOutcome(error="overloaded", retryable=True)
        return writes_and_rewrites(payload)

    world, controller = drive_repairs(tmp_path, behavior=behavior, fix=("001",), lines=("001", "002"))
    first, second = payloads(world, purpose="repair")
    assert (first["attempt"], first["retry_of"], second["attempt"], second["retry_of"]) == (2, None, 3, first["call_id"])
    assert first["feedback"] == second["feedback"] and second["repair_of"] == first["repair_of"]
    state = controller.state
    assert state.spend("fake-batch", "repair").calls == 2 and state.spend("fake-batch", "rebound").calls == 0  # all of it repair
    assert state.counts()["rows"] == {"repaired": 1, "accepted": 2} and len(world.shipped_rows()) == 2


@pytest.mark.parametrize(
    "outcome, reason",
    [
        pytest.param(
            FakeOutcome(error="declined", retryable=False, kind="refused"),
            "the repair call failed (refused): the row waits for the owner",
            id="refused",
        ),
        pytest.param(
            FakeOutcome(error="overloaded", retryable=True),
            "the repair call failed (provider_error): the row waits for the owner",
            id="always failing",
        ),
        pytest.param(
            FakeOutcome(output=json.dumps({"skip": True, "class": "other", "reason": "cannot be written again", "rows": []})),
            "the repair gave no row, the writer set it aside instead of writing it again: the row waits for the owner",
            id="set aside",
        ),
    ],
)
def test_a_repair_that_comes_to_nothing_sends_the_row_back_to_the_owner_with_the_reason(tmp_path, outcome, reason):
    behavior = lambda payload: outcome if payload["repair_of"] else writes_and_rewrites(payload)
    world, controller = drive_repairs(tmp_path, behavior=behavior, fix=("001",), lines=("001", "002"))
    first = originals(controller)
    assert first["001"].status == "hand" and first["001"].hand["reason"] == reason and first["002"].status == "accepted"
    assert [e.payload["reason"] for e in world.events_of("row_to_hand")] == [FIX_REASON, reason]
    assert controller.state.hand_queue() == [first["001"]] and controller.is_idle()
    assert len(world.events_of("repair_requested")) == 1
    assert {row["row_id"] for row in world.shipped_rows()} == {first["002"].row_id}
    assert [entry["reason"] for entry in hand_queue_file(world)] == [reason]


def test_a_repair_call_that_cannot_be_built_sends_the_row_back_to_the_owner(tmp_path):
    # the picture of the line cannot be read when the repair call is built (it could when the first call was)
    world = World(tmp_path, config=repair_config(), behavior=writes_and_rewrites)
    loads = {"count": 0}

    def loader(source, line):
        loads["count"] += 1
        if loads["count"] > 2:
            raise ImageError("the picture is gone")
        return b"picture", "jpg", 64

    controller = world.controller(image_loader=loader)
    controller.add_task(task_spec(("001", "002"), image="p.jpg"))
    world.drive(controller, sends_back("001"))
    first = originals(controller)
    assert first["001"].status == "hand"
    assert first["001"].hand["reason"] == "the repair call failed (bad_request): the row waits for the owner"
    (prepared,) = payloads(world, purpose="repair")
    assert prepared["est_usd"] == 0.0 and prepared["repair_of"] == first["001"].row_id
    assert first["002"].status == "accepted" and controller.state.spend("fake-batch", "repair").actual == 0


# ------------------------------------------------------------------ the step switched off in between


def test_a_row_sent_back_whose_call_was_never_sent_goes_back_to_the_owner_when_the_step_is_switched_off(tmp_path):
    world = World(tmp_path, config=repair_config(enabled=False), behavior=writes_and_rewrites)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003")))
    world.drive(first, sends_back("001", "003"))
    assert len(first.state.hand_queue()) == 2
    world.config = repair_config()
    crashing = world.controller(on_event=crash_on("repair_requested"))
    with pytest.raises(SimulatedCrash):
        crashing.step()
    sent = world.events_of("repair_requested")
    assert len(sent) == 1  # one row was sent back, and the run died before its call was prepared
    world.config = repair_config(enabled=False)
    second = world.controller()
    for _ in range(3):
        second.step()
    row = second.state.rows[sent[0].payload["row_id"]]
    assert row.status == "hand" and "switched off" in row.hand["reason"] and row.hand["reason"].startswith("the repair was not made")
    assert payloads(world, purpose="repair") == [] and len(second.state.hand_queue()) == 2
    # with the step on again the other row is sent back, and the one that already had its turn is not
    world.config = repair_config()
    third = world.controller()
    world.drive(third, sends_back("001", "003"))
    requested = [e.payload["row_id"] for e in world.events_of("repair_requested")]
    assert len(requested) == 2 and requested[0] == sent[0].payload["row_id"]
    assert third.state.rows[requested[0]].status == "hand"
    assert sorted(r.status for r in third.state.rows.values() if r.repair_of is None) == ["accepted", "hand", "repaired"]


# ------------------------------------------------------------------ what a person sees


def test_the_progress_line_and_the_status_say_how_many_rows_were_sent_back(tmp_path):
    world, controller, held = busy_route(tmp_path, min_rows=2, fix=("001", "003"))
    controller.step()
    progress = controller._progress_line(None, None)
    assert "waiting: " in progress and "2 rows sent back to their writers" in progress
    assert "rows sent back to their writers: 2 being written again, 0 written again" in controller.format_status()
    held.clear()
    world.drive(controller, sends_back("001", "003"))
    assert "sent back to their writers" not in controller._progress_line(None, None)
    assert "rows sent back to their writers: 0 being written again, 2 written again" in controller.format_status()
    assert controller.status()["counts"]["rows"]["repaired"] == 2


def test_a_run_that_never_sent_a_row_back_prints_the_status_it_always_did(tmp_path):
    world, controller = drive_repairs(tmp_path, config=repair_config(enabled=False))
    assert "sent back" not in controller.format_status() and "sent back" not in controller._progress_line(None, None)
