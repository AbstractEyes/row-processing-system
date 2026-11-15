"""A packet's pictures are read several at a time from a store behind a network; nothing else about them changes."""

from __future__ import annotations

import json
import threading
import time

import pytest

from rps import layout
from rps.events import PICTURE_READ_WORKERS, picture_read_workers, read_bytes_many
from rps.review import api_seat, client as client_module
from rps.review.client import ReviewerClient
from rps.store import ActorStore, LocalStore
from rps.store.base import StoreUnavailableError
from rps.store.bucket import BucketStore

from fakes import FakeBucketApi
from seat_helpers import REVIEWER, FakeAnthropic, make_seat, publish


class WatchedStore(LocalStore):
    """A local folder whose reads take a while and are counted: how many, and how many at the same time."""

    def __init__(self, root, delay=0.05, **kwargs):
        super().__init__(root, fsync=False, **kwargs)
        self.delay = delay
        self.read_paths: list[str] = []
        self.most_at_once = 0
        self.broken: dict[str, Exception] = {}
        self.fails_at_once: dict[str, Exception] = {}
        self._now = 0
        self._count_lock = threading.Lock()

    def forget(self):
        """Start counting again, once the reads still under way (those a failed pool did not wait for) have ended."""
        deadline = time.monotonic() + 5.0
        while self._now and time.monotonic() < deadline:
            time.sleep(0.01)
        with self._count_lock:
            self.read_paths = []
            self.most_at_once = 0

    def read_bytes(self, path):
        if path in self.fails_at_once:
            raise self.fails_at_once[path]
        with self._count_lock:
            self.read_paths.append(path)
            self._now += 1
            self.most_at_once = max(self.most_at_once, self._now)
        try:
            time.sleep(self.delay)
            if path in self.broken:
                raise self.broken[path]
            return super().read_bytes(path)
        finally:
            with self._count_lock:
                self._now -= 1


@pytest.fixture
def remote(tmp_path):
    return WatchedStore(tmp_path / "remote")


def pooled(monkeypatch, workers=PICTURE_READ_WORKERS):
    """Make the seat and the client read as they do from a store behind a network."""
    monkeypatch.setattr(api_seat, "picture_read_workers", lambda store: workers)
    monkeypatch.setattr(client_module, "picture_read_workers", lambda store: workers)


def picture_paths(packet, reviewer=REVIEWER):
    return [f"{layout.packet_dir(reviewer, packet.packet_id)}/images/{row.row_id}.jpg" for row in packet.rows]


# ---------------------------------------------------------------------------- the reading itself


def test_several_files_come_back_in_the_order_asked_with_none_for_a_missing_one(remote):
    for number in range(7):
        remote.write_bytes(f"a/{number}.bin", b"file-%d" % number)
    paths = [f"a/{number}.bin" for number in (3, 0, 9, 6, 1)]
    one_by_one = read_bytes_many(remote, paths)
    assert one_by_one == [b"file-3", b"file-0", None, b"file-6", b"file-1"]
    assert remote.most_at_once == 1
    assert read_bytes_many(remote, paths, workers=4) == one_by_one
    assert read_bytes_many(remote, [], workers=4) == []


def test_with_workers_the_reads_overlap_and_never_more_than_the_workers(remote):
    for number in range(12):
        remote.write_bytes(f"a/{number}.bin", b"x")
    started = time.monotonic()
    read_bytes_many(remote, [f"a/{number}.bin" for number in range(12)], workers=4)
    took = time.monotonic() - started
    assert 2 <= remote.most_at_once <= 4
    assert took < 12 * remote.delay  # one after another would take twelve delays; four at a time takes about three
    assert sorted(remote.read_paths) == sorted(f"a/{number}.bin" for number in range(12))  # each file read once


def test_a_read_that_fails_raises_its_error_and_does_not_wait_for_the_reads_under_way(tmp_path):
    store = WatchedStore(tmp_path / "remote", delay=1.5)
    for number in range(4):
        store.write_bytes(f"a/{number}.bin", b"x")
    store.fails_at_once["a/0.bin"] = StoreUnavailableError("the store is unavailable")  # while the others still sleep
    started = time.monotonic()
    with pytest.raises(StoreUnavailableError):
        read_bytes_many(store, [f"a/{number}.bin" for number in range(4)], workers=4)
    assert time.monotonic() - started < 1.0  # the three slow reads were not waited for


def test_only_a_store_behind_a_network_reads_pictures_several_at_a_time(tmp_path):
    local = LocalStore(tmp_path / "local", fsync=False)
    assert picture_read_workers(local) == 0
    assert picture_read_workers(ActorStore(local, layout.operator_actor())) == 0
    bucket = BucketStore("owner/bucket", api=FakeBucketApi())
    assert picture_read_workers(bucket) == PICTURE_READ_WORKERS == 4
    assert picture_read_workers(ActorStore(bucket, layout.operator_actor())) == 4


# ---------------------------------------------------------------------------- the seat


def requests_sent(store, tmp_path, name):
    api = FakeAnthropic()
    seat, printed = make_seat(store, tmp_path / name, api)
    seat.step()
    return api.batches.sent, printed


def test_the_seat_sends_the_same_requests_whether_it_reads_the_pictures_one_by_one_or_several_at_a_time(tmp_path, monkeypatch):
    plain, fast = WatchedStore(tmp_path / "plain"), WatchedStore(tmp_path / "fast")
    packets = [(publish(plain, rows=6, pictures=True, line=line), publish(fast, rows=6, pictures=True, line=line)) for line in ("001", "002")]
    before, _ = requests_sent(plain, tmp_path, "one")
    assert plain.most_at_once == 1
    pooled(monkeypatch)
    after, _ = requests_sent(fast, tmp_path, "two")
    assert len(before) == 12
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)  # byte for byte the same requests
    assert 2 <= fast.most_at_once <= PICTURE_READ_WORKERS
    wanted = sorted(path for _, packet in packets for path in picture_paths(packet))
    assert sorted(path for path in fast.read_paths if path.endswith(".jpg")) == wanted  # each picture read once, no more


def test_a_packet_whose_picture_is_missing_is_left_alone_and_said_so_as_before(remote, tmp_path, monkeypatch):
    pooled(monkeypatch)
    api = FakeAnthropic()
    packet = publish(remote, rows=4, pictures=True)
    other = publish(remote, rows=4, pictures=True, line="002")
    gone = picture_paths(packet)[2]
    remote.delete(gone)
    seat, printed = make_seat(remote, tmp_path, api)
    seat.step()
    texts = [request["params"]["messages"][0]["content"][-1]["text"] for request in api.batches.sent]
    asked = [json.loads(text.rsplit("\n\n", 1)[0])["row_id"] for text in texts]
    assert asked == [row.row_id for row in other.rows]  # the other packet went out whole
    said = [line for line in printed if "cannot be read" in line]
    assert len(said) == 1 and packet.packet_id in said[0] and f"{packet.rows[2].row_id}.jpg" in said[0]
    assert seat._pictures == {}  # nothing read ahead is kept past its packet
    assert other.packet_id not in said[0]


def test_a_picture_the_store_refuses_ahead_is_met_again_by_the_one_by_one_read(remote, tmp_path, monkeypatch):
    pooled(monkeypatch)
    api = FakeAnthropic()
    packet = publish(remote, rows=3, pictures=True)
    remote.broken[picture_paths(packet)[1]] = StoreUnavailableError("the store is unavailable")
    seat, printed = make_seat(remote, tmp_path, api)
    with pytest.raises(StoreUnavailableError):
        seat.step()  # as without reading ahead: a store that fails stops the pass, and the next pass tries again
    assert api.batches.sent == []
    remote.broken.clear()
    seat.step()
    assert len(api.batches.sent) == 3
    assert sum("could not be read ahead" in line and "one by one for the next 10 minutes" in line for line in printed) == 1


def test_after_a_read_ahead_that_failed_the_seat_reads_one_by_one_for_ten_minutes(remote, tmp_path, monkeypatch):
    pooled(monkeypatch)
    api = FakeAnthropic()
    first = publish(remote, rows=4, pictures=True)
    remote.broken[picture_paths(first)[0]] = StoreUnavailableError("HTTP 429, rate limited")
    seat, printed = make_seat(remote, tmp_path, api)
    with pytest.raises(StoreUnavailableError):
        seat.step()
    remote.broken.clear()
    remote.forget()
    seat.step()  # the store answers again, and the seat still rests: its pictures come one at a time
    assert len(api.batches.sent) == 4 and remote.most_at_once == 1
    publish(remote, rows=4, pictures=True, line="002")
    api.clock.advance(minutes=9)
    remote.forget()
    seat.step()
    assert len(api.batches.sent) == 8 and remote.most_at_once == 1  # nine minutes on: still one by one
    publish(remote, rows=4, pictures=True, line="003")
    api.clock.advance(minutes=2)
    remote.forget()
    seat.step()
    assert len(api.batches.sent) == 12 and 2 <= remote.most_at_once <= PICTURE_READ_WORKERS  # the rest is over
    assert sum("could not be read ahead" in line for line in printed) == 1  # said once, when the rest began


def test_a_packet_with_one_picture_or_with_none_is_read_as_it_always_was(remote, tmp_path, monkeypatch):
    pooled(monkeypatch)
    api = FakeAnthropic()
    one = publish(remote, rows=1, pictures=True)
    publish(remote, rows=3, line="002")  # text only
    seat, printed = make_seat(remote, tmp_path, api)
    seat.step()
    assert len(api.batches.sent) == 4
    assert [path for path in remote.read_paths if path.endswith(".jpg")] == picture_paths(one)  # one read, no pool
    assert remote.most_at_once == 1
    assert not any("read ahead" in line for line in printed)


# ---------------------------------------------------------------------------- the reviewer's copy of a packet


def test_the_copy_of_a_packet_holds_the_same_files_and_names_the_missing_pictures_in_order(tmp_path, monkeypatch):
    plain, fast = WatchedStore(tmp_path / "plain"), WatchedStore(tmp_path / "fast")
    results = []
    for store, name, several in ((plain, "one", False), (fast, "two", True)):
        packet = publish(store, rows=7, pictures=True)
        paths = picture_paths(packet)
        store.delete(paths[1])
        store.delete(paths[5])
        if several:
            pooled(monkeypatch)
        desk = ReviewerClient(REVIEWER, store, tmp_path / name, fsync=False)
        local, missing = desk.download(packet.packet_id)
        files = {path.name: path.read_bytes() for path in sorted((local.folder / "images").iterdir())}
        results.append((missing, files, (local.folder / "packet.json").read_bytes()))
    assert results[0] == results[1]
    assert results[0][0] == [f"images/{packet.rows[1].row_id}.jpg", f"images/{packet.rows[5].row_id}.jpg"]
    assert len(results[0][1]) == 5
    assert plain.most_at_once == 1 and 2 <= fast.most_at_once <= PICTURE_READ_WORKERS


def test_after_a_copy_that_failed_the_reviewer_client_reads_one_by_one_for_a_while(remote, tmp_path, monkeypatch):
    pooled(monkeypatch)
    first = publish(remote, rows=5, pictures=True)
    second = publish(remote, rows=5, pictures=True, line="002")
    desk = ReviewerClient(REVIEWER, remote, tmp_path / "desk", fsync=False)
    remote.broken[picture_paths(first)[3]] = StoreUnavailableError("HTTP 429, rate limited")
    with pytest.raises(StoreUnavailableError):
        desk.download(first.packet_id)  # the failure reaches the caller, as a single read's failure does
    assert not list((tmp_path / "desk").rglob("*.jpg"))  # and nothing of the packet was written
    assert desk._pictures_rest_until > time.monotonic() + client_module.PICTURE_REST_SECONDS - 5
    remote.broken.clear()
    remote.forget()
    _local, missing = desk.download(first.packet_id)
    assert missing == [] and remote.most_at_once == 1  # resting: one at a time
    desk._pictures_rest_until = 0.0  # the rest is over
    remote.forget()
    _local, missing = desk.download(second.packet_id)
    assert missing == [] and 2 <= remote.most_at_once <= PICTURE_READ_WORKERS
