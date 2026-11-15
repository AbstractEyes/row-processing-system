"""The start reads event files several at a time and folds them into the state one file at a time."""

from __future__ import annotations

import threading
import time

import pytest

from rps.events import default_read_workers, iter_event_files, replay, replay_into
from rps.layout import events_path, operator_actor
from rps.state import State
from rps.store import ActorStore, LocalStore
from rps.store.base import NotFound, StoreUnavailableError
from rps.store.bucket import BucketStore

from fakes import FakeBucketApi
from helpers import Builder


class SlowStore(LocalStore):
    """A local folder whose reads take a while, as a read through a network does."""

    def __init__(self, root, delay=0.05, **kwargs):
        super().__init__(root, fsync=False, **kwargs)
        self.delay = delay
        self.reads = 0
        self.vanished: set[str] = set()
        self.broken: dict[str, Exception] = {}
        self._count_lock = threading.Lock()

    def read_bytes(self, path):
        with self._count_lock:
            self.reads += 1
        time.sleep(self.delay)
        if path in self.broken:
            raise self.broken[path]
        if path in self.vanished:
            raise NotFound(path)
        return super().read_bytes(path)


def build_events(task_count=3):
    builder = Builder()
    for number in range(task_count):
        task = builder.task(batch=str(number), lines={"001": "a cat", "002": "a dog"})
        for line in ("001", "002"):
            call = builder.prepare(task, line)
            builder.submit([call], handle_id=f"batch-{number}-{line}")
            rows = builder.returned(call, rows=1, handle_id=f"batch-{number}-{line}")
            builder.assessed(rows[0], call)
    return builder.events


def write_files(store, events, files, *, torn_in=None, garbage_in=None):
    """Spread the events over `files` event files (parts of one run), in order."""
    per = max(1, len(events) // files)
    for part in range(files):
        chunk = events[part * per : (part + 1) * per if part < files - 1 else len(events)]
        lines = [event.to_line() for event in chunk]
        text = "\n".join(lines) + "\n"
        if garbage_in == part:
            text = lines[0] + "\n" + "this is not json\n" + "\n".join(lines[1:]) + "\n"
        if torn_in == part:
            text = text + '{"ts": "2026-11-07T09:00:00.000000Z", "actor": "x", "ki'  # no newline: an interrupted append
        path = events_path("20261107T090000Z-aaaa", part)
        store.write_bytes(path, text.encode("utf-8"))


def test_a_pooled_read_gives_the_events_and_problems_of_a_plain_one_on_a_mixed_fixture(tmp_path):
    store = SlowStore(tmp_path / "state", delay=0.0)
    events = build_events(4)
    write_files(store, events, 9, torn_in=7, garbage_in=2)
    plain = replay(store, workers=0)
    pooled = replay(store, workers=5)
    assert [e.to_line() for e in pooled.events] == [e.to_line() for e in plain.events] == [e.to_line() for e in events]
    assert pooled.problems == plain.problems and len(plain.problems) == 2
    assert [p.torn for p in plain.problems] == [False, True]
    assert pooled.paths == plain.paths and len(plain.paths) == 9
    assert pooled.report() == plain.report()


def test_forty_files_read_four_times_faster_in_a_pool_with_the_order_kept(tmp_path):
    store = SlowStore(tmp_path / "state", delay=0.05)
    write_files(store, build_events(6), 40)
    started = time.perf_counter()
    plain = replay(store, workers=0)
    one_after_another = time.perf_counter() - started
    started = time.perf_counter()
    pooled = replay(store, workers=8)
    in_a_pool = time.perf_counter() - started
    assert [e.to_line() for e in pooled.events] == [e.to_line() for e in plain.events]
    assert pooled.paths == plain.paths
    assert one_after_another >= 4 * in_a_pool, (one_after_another, in_a_pool)


def test_a_read_that_fails_surfaces_when_its_file_comes_up_and_the_files_before_it_were_handed_on(tmp_path):
    store = SlowStore(tmp_path / "state", delay=0.0)
    write_files(store, build_events(4), 8)
    bad = events_path("20261107T090000Z-aaaa", 5)
    store.broken[bad] = StoreUnavailableError("remote read kept failing after 6 attempts")
    for workers in (0, 4):
        seen = []
        with pytest.raises(StoreUnavailableError, match="kept failing"):
            for path, _events, _problems in iter_event_files(store, workers=workers):
                seen.append(path)
        assert seen == [events_path("20261107T090000Z-aaaa", part) for part in range(5)]
        with pytest.raises(StoreUnavailableError):
            replay(store, workers=workers)
        with pytest.raises(StoreUnavailableError):
            replay_into(store, lambda event: None, workers=workers)


def test_a_file_that_is_listed_and_gone_gives_no_events_in_a_pool_as_without_one(tmp_path):
    store = SlowStore(tmp_path / "state", delay=0.0)
    write_files(store, build_events(3), 6)
    store.vanished.add(events_path("20261107T090000Z-aaaa", 3))
    plain = replay(store, workers=0)
    pooled = replay(store, workers=3)
    assert len(plain.events) < len(build_events(3))
    assert [e.to_line() for e in pooled.events] == [e.to_line() for e in plain.events]
    assert pooled.paths == plain.paths


def test_the_look_ahead_is_bounded(tmp_path):
    store = SlowStore(tmp_path / "state", delay=0.0)
    write_files(store, build_events(6), 30)
    files = iter_event_files(store, workers=2, lookahead=3)
    next(files)
    time.sleep(0.2)
    assert store.reads <= 4  # the file handed on and at most three more, however many files there are
    files.close()


def test_streaming_a_replay_into_a_state_gives_the_state_count_and_problems_of_the_whole_list(tmp_path):
    store = SlowStore(tmp_path / "state", delay=0.0)
    events = build_events(5)
    write_files(store, events, 7, torn_in=6, garbage_in=1)
    whole = replay(store, workers=0)
    expected = State.from_events(whole.events)
    for workers in (0, 3):
        built = State()
        streamed = replay_into(store, built.apply, workers=workers)
        assert streamed.events == []  # nothing is kept but the count, the problems and the paths
        assert streamed.count == len(whole.events) == built.events_applied
        assert streamed.problems == whole.problems and streamed.paths == whole.paths
        assert streamed.report() == whole.report()
        assert built.tasks.keys() == expected.tasks.keys()
        assert built.calls.keys() == expected.calls.keys()
        assert {k: (r.status, r.call_id) for k, r in built.rows.items()} == {k: (r.status, r.call_id) for k, r in expected.rows.items()}
        assert built.job_progress(3) == expected.job_progress(3)


def test_the_number_of_workers_has_a_default_for_the_kind_of_store(tmp_path):
    local = LocalStore(tmp_path / "state", fsync=False)
    assert default_read_workers(local) == 0
    assert default_read_workers(ActorStore(local, operator_actor())) == 0
    bucket = BucketStore("owner/name", api=FakeBucketApi())
    assert 4 <= default_read_workers(bucket) <= 8
    assert 4 <= default_read_workers(ActorStore(bucket, operator_actor())) <= 8


def test_a_bucket_store_is_read_from_several_threads_with_one_client(tmp_path):
    api = FakeBucketApi()
    store = BucketStore("owner/name", api=api, cache_dir=tmp_path / "cache")
    events = build_events(4)
    for part in range(10):
        store.write_bytes(events_path("20261107T090000Z-aaaa", part), (events[part].to_line() + "\n").encode("utf-8"))
    store.flush(force=True)
    pooled = replay(store, workers=6)
    plain = replay(store, workers=0)
    assert [e.to_line() for e in pooled.events] == [e.to_line() for e in plain.events] == [e.to_line() for e in events[:10]]
    # every download used a name of its own and none is left behind
    assert not list((tmp_path / "cache").glob("*.download"))


def test_the_client_of_a_store_is_made_once_when_several_reads_start_together(tmp_path, monkeypatch):
    import huggingface_hub

    made = []

    class Api:
        def __init__(self, token=None):
            time.sleep(0.05)
            made.append(token)

    monkeypatch.setenv("HF_TOKEN", "test-token-value")
    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    store = BucketStore("owner/name", cache_dir=tmp_path / "cache")
    threads = [threading.Thread(target=store._client) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(made) == 1


def test_a_controller_started_with_a_pool_has_the_state_of_one_started_without(tmp_path):
    from harness import World, task_spec

    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003")))
    first.run()
    before = replay(world.state_store).count
    plain = world.controller(read_workers=0)
    plain.start()
    pooled = world.controller(read_workers=3)
    pooled.start()
    started = [e for e in replay(world.state_store).events if e.kind == "controller_started"]
    assert started[-2].payload["replayed_events"] == before and started[-1].payload["replayed_events"] == before + 1
    assert {k: (r.status, r.call_id) for k, r in plain.state.rows.items()} == {k: (r.status, r.call_id) for k, r in pooled.state.rows.items()}
    assert plain.state.job_progress(3) == pooled.state.job_progress(3) and plain.state.calls.keys() == pooled.state.calls.keys()
