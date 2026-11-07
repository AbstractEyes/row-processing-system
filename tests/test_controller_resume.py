"""A run that is killed at any moment resumes without repeating a call or a row."""

import json
from collections import Counter

import pytest

import rps.controller as controller_module
from rps import layout
from rps.events import replay
from rps.providers import SimulatedCrash
from rps.state import State

from harness import World, task_spec

LINES = ("001", "002", "003", "004")


def crash_after(count):
    seen = [0]

    def hook(event):
        seen[0] += 1
        if seen[0] == count:
            raise SimulatedCrash(f"killed after event {count} ({event.kind})")

    return hook


def crash_on(kind, nth=1):
    seen = [0]

    def hook(event):
        if event.kind == kind:
            seen[0] += 1
            if seen[0] == nth:
                raise SimulatedCrash(f"killed after {kind} #{nth}")

    return hook


def prepared_counts(world):
    return Counter(event.payload["call_id"] for event in world.events_of("call_prepared"))


def check_no_repeats(world):
    assert all(count == 1 for count in prepared_counts(world).values())
    submitted = world.provider.submitted_calls
    assert len(submitted) == len(set(submitted)), "a call was submitted twice"
    returned = Counter(event.payload["call_id"] for event in world.events_of("call_returned"))
    assert all(count == 1 for count in returned.values())
    row_ids = [row["row_id"] for event in world.events_of("call_returned") for row in event.payload["rows"]]
    assert len(row_ids) == len(set(row_ids))


def reference_run(tmp_path):
    world = World(tmp_path / "reference")
    controller = world.controller()
    controller.add_task(task_spec(LINES))
    controller.run()
    return world, len(world.events())


def test_a_run_killed_after_any_event_resumes_to_the_same_result(tmp_path):
    reference, total = reference_run(tmp_path)
    assert total > 15
    expected_rows = {row_id for row_id in reference.state_store.list("")} and {
        r["row_id"] for e in reference.events_of("call_returned") for r in e.payload["rows"]
    }
    for count in range(1, total):
        world = World(tmp_path / f"kill-{count}")
        first = world.controller(on_event=crash_after(count))
        with pytest.raises(SimulatedCrash):
            first.add_task(task_spec(LINES))
            first.run()
        second = world.controller()
        second.add_task(task_spec(LINES))
        second.run()
        check_no_repeats(world)
        assert {r["row_id"] for e in world.events_of("call_returned") for r in e.payload["rows"]} == expected_rows
        assert second.state.counts()["rows"] == reference_counts(reference)
        assert second.state.counts()["packets_open"] == 3
        # the live state is exactly what replaying the files gives
        assert second.state.snapshot() == State.from_events(replay(world.state_store).events).snapshot()
        spend = second.state.spend("fake-batch", "production")
        assert spend.reserved == pytest.approx(0)
        assert spend.calls == 4


def reference_counts(world):
    state = State.from_events(world.events())
    return state.counts()["rows"]


def judged_by_family(reviewer, item):
    """The alpha reviewer rejects everything, so every row is escalated to the other families."""
    if reviewer.family == "alpha":
        return {"verdict": "reject", "reasons": ["needs_work"], "notes": "thought 1 is not about the picture"}
    return {"verdict": "keep"}


def outcome(world):
    """What a finished run leaves behind, ignoring how packets happened to be grouped."""
    state = State.from_events(world.events())
    rows = {
        row_id: (row.status, sorted((v["family"], v["verdict"], v["audit"]) for v in row.verdicts))
        for row_id, row in state.rows.items()
    }
    shipped = sorted(row["row_id"] for row in world.shipped_rows())
    return rows, shipped


def test_a_whole_run_with_escalation_and_audit_killed_after_any_event_ends_in_the_same_place(tmp_path):
    from helpers import make_config

    config = make_config(
        review={
            "packet_size": 2,
            "reviewers": [
                {"id": "rev-a", "family": "alpha"},
                {"id": "rev-b", "family": "beta"},
                {"id": "rev-c", "family": "gamma"},
            ],
            "provider_families": {"fake": "writer-family"},
            "policy": {"audit_fraction": 1.0},
        }
    )
    lines = ("001", "002", "003", "004")
    reference = World(tmp_path / "reference", config=config)
    controller = reference.controller()
    controller.add_task(task_spec(lines, image="picture.jpg"))
    reference.drive(controller, judged_by_family)
    expected = outcome(reference)
    total = len(reference.events())
    assert total > 40 and len(expected[1]) == 4  # every row is settled and shipped in the end
    judged = [v for _status, verdicts in expected[0].values() for v in verdicts]
    assert any(v[1] == "reject" for v in judged)  # escalation happened
    assert any(v[2] for v in judged)  # and so did an audit

    for count in range(1, total):
        world = World(tmp_path / f"kill-{count}", config=config)
        first = world.controller(on_event=crash_after(count))
        with pytest.raises(SimulatedCrash):
            first.add_task(task_spec(lines, image="picture.jpg"))
            world.drive(first, judged_by_family)
        second = world.controller()
        second.add_task(task_spec(lines, image="picture.jpg"))
        world.drive(second, judged_by_family)
        check_no_repeats(world)
        assert outcome(world) == expected, f"a run killed after event {count} ended differently"
        assert second.state.snapshot() == State.from_events(replay(world.state_store).events).snapshot()
        assert not [p for p in world.state_store.list("packets/") if "/images/" in p]


def test_a_batch_id_recorded_before_a_crash_is_polled_not_resubmitted(tmp_path):
    world = World(tmp_path, polls_until_ended=2)
    first = world.controller(on_event=crash_on("batch_submitted"))
    with pytest.raises(SimulatedCrash):
        first.add_task(task_spec(LINES))
        first.run()
    assert world.provider.submit_count == 1
    assert world.provider.poll_counts == {}
    second = world.controller()
    second.run()
    assert world.provider.submit_count == 1  # polled, not resubmitted
    assert sum(world.provider.poll_counts.values()) >= 3
    assert second.state.counts()["batches_waiting"] == 0
    assert len(second.state.rows) == 4
    check_no_repeats(world)


def test_a_batch_the_provider_accepted_just_before_the_crash_is_found_again(tmp_path):
    world = World(tmp_path)
    world.provider.crash_after_submit = True
    first = world.controller()
    with pytest.raises(SimulatedCrash):
        first.add_task(task_spec(LINES))
        first.run()
    assert world.provider.submit_count == 1
    assert world.events_of("batch_submitted") == []  # the crash came before the id could be recorded
    world.provider.crash_after_submit = False
    second = world.controller()
    second.run()
    assert world.provider.submit_count == 1  # adopted, not submitted again
    (event,) = world.events_of("batch_submitted")
    assert event.payload["recovered"] is True
    assert len(second.state.rows) == 4
    check_no_repeats(world)


def test_calls_prepared_but_never_submitted_are_submitted_by_the_next_run(tmp_path):
    world = World(tmp_path)
    first = world.controller(on_event=crash_on("call_prepared", nth=2))
    with pytest.raises(SimulatedCrash):
        first.add_task(task_spec(LINES))
        first.run()
    assert world.provider.submit_count == 0
    second = world.controller()
    second.run()
    check_no_repeats(world)
    assert prepared_counts(world) == Counter({call_id: 1 for call_id in prepared_counts(world)})
    assert len(prepared_counts(world)) == 4
    assert len(second.state.rows) == 4


def test_each_run_writes_its_own_event_file(tmp_path):
    world = World(tmp_path)
    first = world.controller(on_event=crash_on("batch_submitted"))
    with pytest.raises(SimulatedCrash):
        first.add_task(task_spec(LINES))
        first.run()
    world.controller().run()
    world.controller().run()
    files = layout.sort_event_paths(world.state_store.list("events/"))
    runs = [layout.parse_events_path(path)[0] for path in files]
    assert runs == sorted(set(runs)) and len(runs) == 3


@pytest.mark.parametrize(
    "kind, nth",
    [
        ("packet_published", 1),
        ("verdict_received", 1),
        ("verdict_received", 3),
        ("row_accepted", 1),
        ("packet_burned", 1),
        ("shipped", 1),
    ],
)
def test_a_run_killed_during_review_or_shipping_finishes_after_a_restart(tmp_path, kind, nth):
    from helpers import make_config

    # Reviewers from families the routing table lets see public pictures, so there is a burn to kill.
    config = make_config(
        review={
            "packet_size": 3,
            "reviewers": [
                {"id": "rev-a", "family": "openai"},
                {"id": "rev-b", "family": "google"},
                {"id": "rev-c", "family": "deepseek"},
            ],
            "provider_families": {"fake": "writer-family"},
            "policy": {"audit_fraction": 0.0},
        }
    )
    world = World(tmp_path, config=config)
    first = world.controller(on_event=crash_on(kind, nth))
    first.add_task(task_spec(("001", "002", "003", "004", "005", "006"), image="picture.jpg"))
    with pytest.raises(SimulatedCrash):
        world.drive(first)
    second = world.controller()
    world.drive(second)
    check_no_repeats(world)
    rows = world.shipped_rows()
    assert len(rows) == 6 and len({row["row_id"] for row in rows}) == 6
    assert second.state.job_progress(2)["lines_accepted"] == 6
    packets = world.state_store.list("packets/")
    assert len([p for p in packets if p.endswith("packet.json")]) == len(world.events_of("packet_published"))
    assert not [p for p in packets if "/images/" in p]


def test_a_crash_between_writing_a_packet_and_announcing_it_publishes_it_once(tmp_path, monkeypatch):
    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003")))
    original = controller_module.publish_packet
    calls = []

    def crashing(store, packet):
        original(store, packet)
        calls.append(packet.packet_id)
        raise SimulatedCrash("killed after the packet files were written")

    monkeypatch.setattr(controller_module, "publish_packet", crashing)
    with pytest.raises(SimulatedCrash):
        first.run()
    monkeypatch.setattr(controller_module, "publish_packet", original)
    assert world.events_of("packet_published") == []
    assert world.state_store.list("packets/")  # the files are there, unannounced
    second = world.controller()
    world.drive(second)
    packet_files = [p for p in world.state_store.list("packets/") if p.endswith("packet.json")]
    announced = {e.payload["packet_id"] for e in world.events_of("packet_published")}
    assert len(packet_files) == len(announced) == 3
    assert calls[0] in announced  # the same packet id came out again
    assert len(world.shipped_rows()) == 3


def test_a_run_resumes_after_a_whole_new_machine_with_only_the_stores(tmp_path):
    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003")))
    first.run()
    world.run_reviewers()
    # a new process: nothing but the stores and the provider survive
    fresh_world = World(tmp_path / "ignored", polls_until_ended=0)
    fresh_world.state_store = world.state_store
    fresh_world.ship_store = world.ship_store
    fresh_world.provider = world.provider
    second = fresh_world.controller()
    second.run()
    assert len(fresh_world.shipped_rows()) == 3
    assert world.provider.submit_count == 1
    ledger = json.loads(world.ship_store.read_text("ledger/summary.json"))
    assert ledger["routes"]["fake-batch"]["purposes"]["production"]["calls"] == 3
