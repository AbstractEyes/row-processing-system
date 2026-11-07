"""Shipping through a store that sends one commit a minute, as the dataset repository does.

The ship store holds a write until the next commit is due, so a write is not yet a shipment. A
shipment counts as made when the store has sent it: the controller hands each file over once, and
records it (a ledger file as written, an accepted-row file as `shipped`) on the pass that finds the
store no longer holds it. These tests use the real pacing (60 seconds a commit), a repository in
memory and a clock the test moves.
"""

from __future__ import annotations

import json

import pytest
from huggingface_hub import CommitOperationAdd

from rps import layout
from rps.events import replay
from rps.providers import SimulatedCrash
from rps.state import State
from rps.store import ActorStore, LocalStore
from rps.store.bucket import BucketStore
from rps.store.hub import HubStore

from fakes import Clock, FakeBucketApi, FakeHubApi
from harness import World, keep_all, task_spec
from helpers import make_config

POLL = 30.0
INTERVAL = 60.0  # the dataset repository store's commit interval by default


def hub_store(api, clock, tmp_path, name="hub-cache", **kwargs):
    return HubStore("owner/rows", api=api, clock=clock, sleep=clock.sleep, rng=lambda: 1.0, cache_dir=tmp_path / name, **kwargs)


def paced_world(tmp_path, store_options=None, **overrides):
    """A world whose ship store is a dataset repository in memory with the real pacing, and the clock it runs on."""
    clock = Clock()
    api = FakeHubApi(clock=clock)
    world = World(tmp_path, config=make_config(poll_seconds=POLL, **overrides))
    world.ship_store = hub_store(api, clock, tmp_path, **(store_options or {}))
    return world, api, clock


def controller_on(world, clock, **options):
    return world.controller(clock=clock, sleep=clock.sleep, **options)


def hub_rows(api):
    """The accepted rows the repository holds."""
    return [
        json.loads(line)
        for path, data in sorted(api.files.items())
        if path.startswith("accepted/")
        for line in data.decode("utf-8").splitlines()
        if line.strip()
    ]


def committed(api):
    """Every (path, content) the repository was asked to take, in order."""
    return [
        (operation.path_in_repo, operation.path_or_fileobj)
        for call in api.create_commit_calls
        for operation in call["operations"]
        if isinstance(operation, CommitOperationAdd)
    ]


def paths_of(call):
    return sorted(operation.path_in_repo for operation in call["operations"])


def lines_accepted(controller):
    return controller.state.job_progress(2).get("lines_accepted", 0)


def n_tasks(controller, count):
    """`count` tasks of three lines each: `count` accepted-row files, so that a pass writes several files."""
    for number in range(1, count + 1):
        controller.add_task(task_spec((f"{number}01", f"{number}02", f"{number}03"), batch=str(number)))


def accept_in_one_pass(world, controller, lines):
    """Passes of the controller and the reviewers with no time passing, until one pass has accepted every line.

    That pass writes every accepted-row file and the ledger together, which a store that holds writes back
    keeps until its commit is due.
    """
    for _ in range(12):
        before = lines_accepted(controller)
        controller.step()
        if lines_accepted(controller) == lines:
            assert before == 0
            return
        world.run_reviewers(keep_all)
    raise AssertionError("the lines were not accepted")


def work(world, controller, clock, *, rounds=60, verdict_fn=keep_all):
    """Passes of the controller and the reviewers with a poll of the clock between them, until the run is idle.

    Returns the clock's reading at the first pass that saw every line accepted and at the first that saw
    every accepted file recorded as shipped.
    """
    accepted_at = shipped_at = None
    for _ in range(rounds):
        controller.step()
        world.run_reviewers(verdict_fn)
        progress = controller.state.job_progress(2)
        if accepted_at is None and progress.get("lines_accepted", 0) == progress["lines_total"]:
            accepted_at = clock.now
        if accepted_at is not None and shipped_at is None and not controller.state.files_to_ship():
            shipped_at = clock.now
        if controller.is_idle() and not controller.state.open_packets():
            return accepted_at, shipped_at
        clock.advance(POLL)
    raise AssertionError("the run did not settle")


# ---------------------------------------------------------------------------- a run that only waits for review


def test_a_run_whose_only_work_left_is_review_ends_idle_within_a_commit_interval_and_a_poll(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 2)
    started = clock.now
    summary = controller.run(time_budget_seconds=10 * INTERVAL)
    assert summary.reason == "idle"
    assert clock.now - started <= INTERVAL + POLL
    assert controller.state.counts()["packets_open"] > 0  # the rows wait for reviewers: all that is left is theirs
    assert sorted(api.files) == [layout.LEDGER_SUMMARY_PATH, layout.LEDGER_TASKS_PATH]
    assert len(api.create_commit_calls) == 1  # both ledger files in one commit, once


def test_each_ledger_file_is_sent_again_only_when_it_has_changed(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 1)
    controller.run(time_budget_seconds=10 * INTERVAL)
    assert len(api.create_commit_calls) == 1
    for _ in range(5):  # passes and polls with nothing new commit nothing
        controller.step()
        clock.advance(POLL)
    assert len(api.create_commit_calls) == 1 and controller.is_idle()
    world.run_reviewers(keep_all)  # the verdicts come in: the rows are accepted and shipped
    work(world, controller, clock)
    versions = lambda path: [data for name, data in committed(api) if name == path]  # noqa: E731
    assert len(versions(layout.LEDGER_TASKS_PATH)) == 2  # the rows accepted changed the per-task lines ...
    assert len(versions(layout.LEDGER_SUMMARY_PATH)) == 1  # ... and not the spend, so that file did not go out again
    contents = committed(api)
    assert len(contents) == len(set(contents)) and len(world.events_of("shipped")) == 1


# ---------------------------------------------------------------------------- accepted files and the ledger in one pass


def test_six_accepted_files_and_the_ledger_written_in_one_pass_go_out_in_one_commit_and_are_recorded(tmp_path):
    # The pattern seen in a live run: one pass accepts the lines of six tasks and writes six accepted-row files
    # and the two ledger files; the commit falls due on a later pass. They all went out in one commit, and then
    # nothing was recorded, the files were written again and the same commit went out every interval.
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 6)
    accept_in_one_pass(world, controller, 18)
    assert api.files == {} and world.events_of("shipped") == []
    assert world.ship_store.pending_count == 8  # six accepted-row files and two ledger files, none sent
    assert "waiting: 8 files for the store's next commit" in controller.format_status()
    assert not controller.is_idle()
    clock.advance(INTERVAL + 1)
    controller.step()
    assert len(api.create_commit_calls) == 1 and len(paths_of(api.create_commit_calls[0])) == 8
    assert len(hub_rows(api)) == 18
    assert len(world.events_of("shipped")) == 6 and controller.state.files_to_ship() == []
    assert world.ship_store.pending_count == 0 and controller.is_idle()
    for _ in range(4):  # and nothing is written or committed again
        clock.advance(INTERVAL)
        controller.step()
    assert len(api.create_commit_calls) == 1 and len(world.events_of("shipped")) == 6 and controller.is_idle()


def test_a_commit_that_falls_due_inside_a_write_sends_what_was_pending_and_the_rest_goes_with_the_next(tmp_path):
    # Here the commit falls due in the middle of the pass that writes the six accepted-row files and the ledger: it
    # goes out with the first write and takes what was waiting (the ledger from the early passes); the rest of the
    # pass's writes wait for the next commit. Each file is recorded when its own commit goes out.
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 6)
    for _ in range(4):
        controller.step()  # the rows are written and assessed, the packets are published, the ledger waits in the store
    assert world.events_of("packet_published") and api.files == {}
    world.run_reviewers(keep_all)  # every verdict is in
    clock.advance(INTERVAL + 1)  # the ledger has waited a whole interval: the first write of the next pass finds a commit due
    controller.step()  # accepts every row, writes six accepted-row files and the changed ledger
    assert lines_accepted(controller) == 18
    assert len(api.create_commit_calls) == 1
    first = paths_of(api.create_commit_calls[0])
    assert len(first) == 3 and layout.LEDGER_SUMMARY_PATH in first and layout.LEDGER_TASKS_PATH in first  # the ledger, and the file whose write it was
    assert len(world.events_of("shipped")) == 1  # that file is recorded now; the other five are not sent yet
    assert world.ship_store.pending_count == 6 and not controller.is_idle()
    clock.advance(INTERVAL)
    controller.step()
    assert len(api.create_commit_calls) == 2
    assert len(hub_rows(api)) == 18 and len(world.events_of("shipped")) == 6
    assert controller.state.files_to_ship() == [] and world.ship_store.pending_count == 0 and controller.is_idle()
    contents = committed(api)
    assert len(contents) == len(set(contents))


# ---------------------------------------------------------------------------- accepted rows


def test_accepted_rows_are_recorded_as_shipped_within_a_commit_interval(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 2)
    accepted_at, shipped_at = work(world, controller, clock)
    assert accepted_at is not None and shipped_at is not None
    assert shipped_at - accepted_at <= INTERVAL + POLL
    assert len(hub_rows(api)) == 6 and len({row["row_id"] for row in hub_rows(api)}) == 6
    assert len(controller.state.shipped_files) == 2 and controller.state.files_to_ship() == []
    assert len(world.events_of("shipped")) == 2  # each file once, after the repository had it
    assert controller.is_idle()


def test_a_shipped_event_comes_only_after_the_repository_has_the_file(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 2)
    accept_in_one_pass(world, controller, 6)  # no time has passed: nothing has been sent
    assert api.files == {} and world.events_of("shipped") == []
    wait_for = [layout.accepted_path("fake-batch", task) for task in sorted(controller.state.tasks)]
    assert all(controller.ship_store.is_pending(path) for path in wait_for)
    clock.advance(INTERVAL)
    controller.step()
    assert len(world.events_of("shipped")) == 2 and len(hub_rows(api)) == 6
    assert not any(controller.ship_store.is_pending(path) for path in wait_for + [layout.LEDGER_SUMMARY_PATH])


def test_identical_content_is_never_committed_twice(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 2)
    work(world, controller, clock)
    contents = committed(api)
    assert contents and len(contents) == len(set(contents))
    # And every commit carries something that changed: no path goes out with the content it already had.
    held = {}
    for call in api.create_commit_calls:
        for operation in call["operations"]:
            assert held.get(operation.path_in_repo) != operation.path_or_fileobj
            held[operation.path_in_repo] = operation.path_or_fileobj


def test_commits_keep_to_one_a_minute_however_many_passes_there_are(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 2)
    work(world, controller, clock)
    times = [call["at"] for call in api.create_commit_calls]
    assert times and all(later - earlier >= INTERVAL for earlier, later in zip(times, times[1:]))


def test_more_files_than_one_commit_takes_go_out_over_several_commits_each_recorded_when_it_goes(tmp_path):
    world, api, clock = paced_world(tmp_path, store_options={"files_per_commit": 3})
    controller = controller_on(world, clock)
    n_tasks(controller, 6)
    work(world, controller, clock, rounds=120)
    sizes = [len(call["operations"]) for call in api.create_commit_calls]
    assert len(sizes) >= 3 and max(sizes) <= 3  # eight files and three to a commit
    assert len(hub_rows(api)) == 18 and len(world.events_of("shipped")) == 6
    assert controller.state.files_to_ship() == [] and controller.is_idle()
    contents = committed(api)
    assert len(contents) == len(set(contents))
    times = [call["at"] for call in api.create_commit_calls]
    assert all(later - earlier >= INTERVAL for earlier, later in zip(times, times[1:]))


# ---------------------------------------------------------------------------- a store that holds back or fails


def test_a_commit_that_fails_leaves_the_files_staged_and_they_go_out_on_a_later_pass(tmp_path):
    world, api, clock = paced_world(tmp_path)
    api.create_failures = [PermissionError("denied")]  # not retried by the store: the flush raises StoreError
    controller = controller_on(world, clock)
    n_tasks(controller, 1)
    controller.step()
    world.run_reviewers(keep_all)
    controller.step()
    clock.advance(INTERVAL)
    controller.step()  # the commit is due and fails
    assert world.events_of("shipped") == [] and api.files == {}
    assert any("shipping failed" in line for line in world.printed)
    clock.advance(INTERVAL)
    work(world, controller, clock)
    assert len(hub_rows(api)) == 3 and len(world.events_of("shipped")) == 1


def test_a_commit_that_fails_inside_a_write_is_counted_like_one_that_fails_in_the_flush(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 1)
    for _ in range(4):
        controller.step()  # the ledger waits in the store
    world.run_reviewers(keep_all)
    clock.advance(INTERVAL + 1)  # the commit falls due on the first write of the next pass ...
    api.create_failures = [PermissionError("denied")]  # ... and fails
    controller.step()  # does not raise
    assert any("shipping failed (1)" in line for line in world.printed)
    assert api.files == {} and world.events_of("shipped") == []
    work(world, controller, clock)
    assert len(hub_rows(api)) == 3 and len(world.events_of("shipped")) == 1
    assert sum("shipping failed" in line for line in world.printed) == 1  # the one failure; the run went on after it


def test_a_ship_store_that_keeps_failing_ends_the_run_with_its_reason(tmp_path):
    world, api, clock = paced_world(tmp_path)
    api.create_failures = [PermissionError("denied")] * 20
    controller = controller_on(world, clock)
    n_tasks(controller, 1)
    summary = controller.run(time_budget_seconds=60 * INTERVAL)
    assert summary.reason == "ship_failing"
    assert api.files == {} and world.events_of("shipped") == []  # nothing went out, and nothing was recorded as shipped
    assert sum("shipping failed" in line for line in world.printed) == 5  # five passes in a row, then the run stopped


def test_a_commit_the_hourly_cap_holds_back_stays_staged_and_goes_out_when_the_hour_allows(tmp_path):
    world, api, clock = paced_world(tmp_path, store_options={"commits_per_hour": 1})
    controller = controller_on(world, clock)
    n_tasks(controller, 1)
    controller.run(time_budget_seconds=10 * INTERVAL)  # the ledger goes out: the one commit this hour
    assert len(api.create_commit_calls) == 1
    world.run_reviewers(keep_all)
    for _ in range(6):  # the accepted file and the changed ledger wait: the cap holds the commit back
        controller.step()
        clock.advance(INTERVAL)
    assert len(api.create_commit_calls) == 1 and world.events_of("shipped") == []
    assert any("commit attempts in the last hour" in line for line in world.printed)
    assert not controller.is_idle() and world.ship_store.pending_count == 2
    clock.advance(3600)
    controller.step()
    assert len(api.create_commit_calls) == 2 and len(world.events_of("shipped")) == 1
    assert len(hub_rows(api)) == 3 and controller.is_idle()


# ---------------------------------------------------------------------------- the end of a run


def test_a_run_that_ends_for_another_reason_sends_what_the_store_holds_and_records_it(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 2)
    accept_in_one_pass(world, controller, 6)
    assert api.files == {} and world.events_of("shipped") == []
    summary = controller.run(max_steps=0)  # the run is cut short: it sends what the store still holds
    assert summary.reason == "max_steps"
    assert len(hub_rows(api)) == 6 and len(api.create_commit_calls) == 1
    assert len(world.events_of("shipped")) == 2 and controller.state.files_to_ship() == []
    # The next run has nothing left to send: it ends at once, and no commit goes out.
    second = controller_on(world, clock)
    summary = second.run(time_budget_seconds=10 * INTERVAL)
    assert summary.reason == "idle" and summary.seconds == 0
    assert len(api.create_commit_calls) == 1


def test_a_restart_that_finds_nothing_changed_ends_idle_at_once_without_a_commit(tmp_path):
    world, api, clock = paced_world(tmp_path)
    controller = controller_on(world, clock)
    n_tasks(controller, 2)
    work(world, controller, clock)
    commits = len(api.create_commit_calls)
    world.ship_store = hub_store(api, clock, tmp_path, name="hub-cache-2")  # a new process: a new store, nothing pending
    second = controller_on(world, clock)
    n_tasks(second, 2)
    summary = second.run(time_budget_seconds=10 * INTERVAL)
    assert summary.reason == "idle" and summary.seconds == 0
    assert len(api.create_commit_calls) == commits and len(world.events_of("shipped")) == 2


def test_a_restart_hands_over_a_ledger_file_the_repository_does_not_hold_as_it_is_now(tmp_path):
    world, api, clock = paced_world(tmp_path)
    first = controller_on(world, clock)
    n_tasks(first, 1)
    first.run(time_budget_seconds=10 * INTERVAL)  # the ledger as it was when the rows were still unreviewed is in the repository
    world.run_reviewers(keep_all)  # the verdicts come in after that run has ended
    world.ship_store = hub_store(api, clock, tmp_path, name="hub-cache-2")
    second = controller_on(world, clock)
    work(world, second, clock)
    assert len(hub_rows(api)) == 3 and len(world.events_of("shipped")) == 1 and second.is_idle()
    held = {}
    for call in api.create_commit_calls:
        for operation in call["operations"]:
            assert held.get(operation.path_in_repo) != operation.path_or_fileobj
            held[operation.path_in_repo] = operation.path_or_fileobj
    assert json.loads(api.files[layout.LEDGER_SUMMARY_PATH].decode("utf-8"))["routes"]  # the ledger is there


# ---------------------------------------------------------------------------- a run that is killed


def test_a_run_killed_with_files_staged_but_not_sent_ships_them_after_a_restart(tmp_path):
    world, api, clock = paced_world(tmp_path)
    first = controller_on(world, clock)
    n_tasks(first, 2)
    accept_in_one_pass(world, first, 6)
    assert api.files == {}
    submitted = world.provider.submit_count
    for writer in first.writers.values():
        writer.close()
    # The process dies here: what the store was holding goes with it.
    world.ship_store = hub_store(api, clock, tmp_path, name="hub-cache-2")
    second = controller_on(world, clock)
    work(world, second, clock)
    assert len(hub_rows(api)) == 6 and len(world.events_of("shipped")) == 2
    assert second.state.snapshot() == State.from_events(replay(world.state_store).events).snapshot()
    contents = committed(api)
    assert len(contents) == len(set(contents))
    assert world.provider.submit_count == submitted  # nothing was written again


def test_a_run_killed_after_any_event_resumes_to_the_same_result_through_a_paced_store(tmp_path):
    reference, api, clock = paced_world(tmp_path / "reference")
    controller = controller_on(reference, clock)
    n_tasks(controller, 2)
    work(reference, controller, clock)
    total = len(reference.events())
    expected = sorted(row["row_id"] for row in hub_rows(api))
    assert total > 20 and len(expected) == 6
    for count in range(1, total, 3):
        world, api, clock = paced_world(tmp_path / f"kill-{count}")
        seen = [0]

        def hook(event, count=count, seen=seen):
            seen[0] += 1
            if seen[0] == count:
                raise SimulatedCrash(f"killed after event {count} ({event.kind})")

        first = controller_on(world, clock, on_event=hook)
        with pytest.raises(SimulatedCrash):
            n_tasks(first, 2)
            work(world, first, clock)
        for writer in first.writers.values():
            writer.close()
        world.ship_store = hub_store(api, clock, tmp_path / f"kill-{count}", name="hub-cache-2")  # the process is gone
        second = controller_on(world, clock)
        n_tasks(second, 2)
        work(world, second, clock)
        assert sorted(row["row_id"] for row in hub_rows(api)) == expected
        assert second.state.snapshot() == State.from_events(replay(world.state_store).events).snapshot()
        assert second.state.files_to_ship() == [] and second.is_idle()


# ---------------------------------------------------------------------------- a row taken back while its file waits


def test_a_row_taken_back_before_its_file_was_sent_is_not_sent(tmp_path):
    # Two reviewers, an audit of every accepted row: the first keeps and so accepts it, the other (the audit) rejects it.
    world, api, clock = paced_world(
        tmp_path,
        review={
            "packet_size": 1,
            "reviewers": [{"id": "rev-a", "family": "alpha"}, {"id": "rev-b", "family": "beta"}],
            "provider_families": {"fake": "writer-family"},
            "policy": {"audit_fraction": 1.0},
        },
    )
    controller = controller_on(world, clock)
    controller.add_task(task_spec(("001",)))
    for _ in range(6):
        controller.step()
        if world.events_of("packet_published"):
            break
    first = world.events_of("packet_published")[0].payload["reviewer_id"]

    def verdict(reviewer, item):
        return {"verdict": "keep"} if reviewer.id == first else {"verdict": "reject", "reasons": ["needs_work"], "notes": "thought 1 is wrong"}

    world.run_reviewers(verdict, only=first)
    controller.step()  # the row is accepted and its file is handed to the store, which has not sent it
    assert lines_accepted(controller) == 1 and world.events_of("shipped") == []
    world.run_reviewers(verdict)
    controller.step()  # the audit reviewer rejects it: it is no longer accepted
    assert lines_accepted(controller) == 0
    clock.advance(INTERVAL)
    controller.step()
    assert [path for path in api.files if path.startswith("accepted/")] == []  # the file that was waiting did not go out
    assert hub_rows(api) == []


# ---------------------------------------------------------------------------- what the stores say about their pending writes


def test_a_remote_store_says_which_paths_it_has_not_sent_yet(tmp_path):
    clock = Clock()
    hub = hub_store(FakeHubApi(clock=clock), clock, tmp_path)
    bucket = BucketStore("owner/state", api=FakeBucketApi(), clock=clock, sleep=clock.sleep, rng=lambda: 1.0, cache_dir=tmp_path / "cache")
    for store in (hub, bucket):
        assert not store.is_pending("a.txt")
        store.write_text("a.txt", "1")
        store.delete("b.txt")
        assert store.is_pending("a.txt") and store.is_pending("b.txt") and not store.is_pending("c.txt")
        store.flush(force=True)
        assert not store.is_pending("a.txt") and not store.is_pending("b.txt")


def test_a_store_that_writes_through_has_nothing_pending(tmp_path):
    local = LocalStore(tmp_path / "local", fsync=False)
    local.write_text("a.txt", "1")
    assert not local.is_pending("a.txt") and not local.is_pending("missing.txt")


def test_the_wrapper_for_an_actor_passes_the_question_on(tmp_path):
    clock = Clock()
    hub = hub_store(FakeHubApi(clock=clock), clock, tmp_path)
    wrapped = ActorStore(hub, layout.controller_actor("20261107T090000Z-aaaa"))
    wrapped.write_text(layout.LEDGER_SUMMARY_PATH, "{}")
    assert wrapped.is_pending(layout.LEDGER_SUMMARY_PATH) and not wrapped.is_pending(layout.LEDGER_TASKS_PATH)
    wrapped.flush(force=True)
    assert not wrapped.is_pending(layout.LEDGER_SUMMARY_PATH)
    assert not ActorStore(LocalStore(tmp_path / "local", fsync=False), layout.operator_actor()).is_pending("control/STOP")
