"""The planner reads and parses the tasks file only when the store says it changed."""

from __future__ import annotations

import json

import pytest

from rps.layout import operator_actor
from rps.store import ActorStore, LocalStore
from rps.store.base import NotFound, Store
from rps.store.bucket import BucketStore
from rps.store.hub import HubStore
from rps.tasks import StorePlanner

from fakes import FakeBucketApi, FakeHubApi
from helpers import make_config  # noqa: F401  (keeps the test folder's path set up)

PATH = "control/tasks.json"


def task(batch, lines=("001", "002")):
    return {
        "source": "photos", "batch": batch, "route": "fake-batch",
        "lines": [{"line_id": line, "spec": f"spec for {line}", "item_id": f"img-{line}"} for line in lines],
    }


class Counting(LocalStore):
    def __init__(self, root):
        super().__init__(root, fsync=False)
        self.reads = []

    def read_bytes(self, path):
        self.reads.append(path)
        return super().read_bytes(path)


def put(store, tasks):
    ActorStore(store, operator_actor()).write_text(PATH, json.dumps(tasks))


class Clocked:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_a_local_folder_is_not_read_again_while_the_file_is_unchanged(tmp_path):
    store = Counting(tmp_path / "state")
    planner = StorePlanner(store, PATH)  # an interval of zero: asked on every pass
    put(store, [task("1")])
    assert [t.batch for t in planner(None)] == ["1"] and len(store.reads) == 1
    for _ in range(5):
        assert [t.batch for t in planner(None)] == ["1"]
    assert len(store.reads) == 1
    put(store, [task("1"), task("2")])  # a task appended from outside
    assert [t.batch for t in planner(None)] == ["1", "2"] and len(store.reads) == 2
    assert [t.batch for t in planner(None)] == ["1", "2"] and len(store.reads) == 2


def test_a_file_that_is_missing_stays_missing_without_a_read_and_a_new_file_is_read(tmp_path):
    store = Counting(tmp_path / "state")
    planner = StorePlanner(store, PATH)
    assert planner(None) == [] and planner(None) == [] and store.reads == []
    put(store, [task("1")])
    assert [t.batch for t in planner(None)] == ["1"]
    store.delete(PATH)
    assert planner(None) == []
    put(store, [task("1")])
    assert [t.batch for t in planner(None)] == ["1"]


def test_a_changed_file_is_picked_up_within_one_interval(tmp_path):
    store = Counting(tmp_path / "state")
    clock = Clocked()
    planner = StorePlanner(store, PATH, min_interval_seconds=15.0, clock=clock)
    put(store, [task("1")])
    assert [t.batch for t in planner(None)] == ["1"]
    put(store, [task("1"), task("2")])
    clock.now += 5
    assert [t.batch for t in planner(None)] == ["1"]  # inside the interval: what was read last
    clock.now += 11
    assert [t.batch for t in planner(None)] == ["1", "2"]


def test_a_store_that_cannot_say_whether_a_file_changed_is_read_every_time(tmp_path):
    class Silent(Counting):
        def file_stamp(self, path):
            return None

    store = Silent(tmp_path / "state")
    planner = StorePlanner(store, PATH)
    put(store, [task("1")])
    for _ in range(3):
        planner(None)
    assert len(store.reads) == 3


def test_a_stamp_that_fails_falls_back_to_reading(tmp_path):
    class Failing(Counting):
        def file_stamp(self, path):
            raise OSError("cannot stat")

    store = Failing(tmp_path / "state")
    planner = StorePlanner(store, PATH)
    put(store, [task("1")])
    planner(None)
    planner(None)
    assert len(store.reads) == 2


class CountingBucketApi(FakeBucketApi):
    def __init__(self):
        super().__init__()
        self.downloads = 0

    def download_bucket_files(self, bucket_id, files, **kwargs):
        self.downloads += 1
        return super().download_bucket_files(bucket_id, files, **kwargs)


STORES = (
    (CountingBucketApi, lambda api: BucketStore("owner/name", api=api)),
    (FakeHubApi, lambda api: HubStore("owner/name", api=api)),
)


@pytest.mark.parametrize("make_api,make_store", STORES)
def test_a_file_in_a_fake_remote_store_is_not_read_again_while_its_stamp_holds(make_api, make_store):
    api = make_api()
    store = make_store(api)
    planner = StorePlanner(store, PATH)
    assert planner(None) == [] and api.downloads == 0
    store.write_bytes(PATH, json.dumps([task("1")]).encode())
    store.flush(force=True)
    assert [t.batch for t in planner(None)] == ["1"] and api.downloads == 1
    for _ in range(4):
        assert [t.batch for t in planner(None)] == ["1"]
    assert api.downloads == 1
    # a change that leaves the size as it was is still a change: the stamp carries the content's identity
    store.write_bytes(PATH, json.dumps([task("2")]).encode())
    store.flush(force=True)
    assert [t.batch for t in planner(None)] == ["2"] and api.downloads == 2


def test_the_bucket_and_the_hub_stamps_follow_the_content_and_a_missing_file_is_not_found():
    for make_api, make_store in STORES:
        store = make_store(make_api())
        with pytest.raises(NotFound):
            store.file_stamp(PATH)
        store.write_bytes(PATH, b"[]")
        assert store.file_stamp(PATH) is None  # a write not sent yet: the store cannot say
        store.flush(force=True)
        first = store.file_stamp(PATH)
        assert first is not None and store.file_stamp(PATH) == first
        store.write_bytes(PATH, b"[1]")
        store.flush(force=True)
        assert store.file_stamp(PATH) != first
        store.delete(PATH)
        store.flush(force=True)
        with pytest.raises(NotFound):
            store.file_stamp(PATH)


def test_an_actor_store_passes_the_question_on(tmp_path):
    inner = LocalStore(tmp_path / "state", fsync=False)
    view = ActorStore(inner, operator_actor())
    with pytest.raises(NotFound):
        view.file_stamp(PATH)
    view.write_text(PATH, "[]")
    assert view.file_stamp(PATH) == inner.file_stamp(PATH) is not None


def test_the_base_store_cannot_say():
    assert Store.file_stamp(object(), "x") is None
