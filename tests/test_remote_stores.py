import pytest
from huggingface_hub import CommitOperationAdd, CommitOperationDelete

from rps import layout
from rps.layout import controller_actor, reviewer_actor
from rps.store import ActorStore, ConcurrentWriterError, NotFound, OwnershipError, RateLimitedError, StoreError, StoreUnavailableError
from rps.store.bucket import BucketStore
from rps.store.hub import HubStore

from fakes import Clock, EntryNotFoundError, FakeBucketApi, FakeHttpError, FakeHubApi, RepositoryNotFoundError


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def hub_api(clock):
    return FakeHubApi(clock=clock)


def make_hub(hub_api, clock, tmp_path, **kwargs):
    kwargs.setdefault("max_attempts", 6)
    return HubStore("owner/data", api=hub_api, clock=clock, sleep=clock.sleep, rng=lambda: 1.0, cache_dir=tmp_path / "cache", **kwargs)


def make_bucket(api, clock, tmp_path, **kwargs):
    return BucketStore("owner/state", api=api, clock=clock, sleep=clock.sleep, rng=lambda: 1.0, cache_dir=tmp_path / "cache", **kwargs)


# ------------------------------------------------------------------ the dataset repository


def test_hub_writes_wait_for_the_commit_interval_and_go_in_one_commit(hub_api, clock, tmp_path):
    store = make_hub(hub_api, clock, tmp_path)
    for name in ("a", "b", "c"):
        store.write_text(f"accepted/r/{name}.jsonl", name)
    assert hub_api.create_commit_calls == []
    clock.advance(59)
    assert store.maybe_flush() is False
    clock.advance(1)
    assert store.maybe_flush() is True
    assert len(hub_api.create_commit_calls) == 1
    call = hub_api.create_commit_calls[0]
    assert sorted(op.path_in_repo for op in call["operations"]) == ["accepted/r/a.jsonl", "accepted/r/b.jsonl", "accepted/r/c.jsonl"]
    assert all(isinstance(op, CommitOperationAdd) for op in call["operations"])
    assert call["repo_type"] == "dataset" and call["message"].startswith("controller:")
    assert hub_api.files["accepted/r/a.jsonl"] == b"a"


def test_hub_makes_at_most_one_commit_per_interval(hub_api, clock, tmp_path):
    store = make_hub(hub_api, clock, tmp_path)
    store.write_text("a.txt", "1")
    clock.advance(60)
    store.maybe_flush()
    for step in range(5):  # writes keep arriving, but the next commit must wait a full interval
        store.write_text(f"b{step}.txt", "x")
        clock.advance(10)
        store.maybe_flush()
    assert len(hub_api.create_commit_calls) == 1
    clock.advance(10)
    store.maybe_flush()
    assert len(hub_api.create_commit_calls) == 2
    times = [call["at"] for call in hub_api.create_commit_calls]
    assert times[1] - times[0] >= 60
    assert len(hub_api.create_commit_calls[1]["operations"]) == 5


def test_hub_commits_stay_under_one_hundred_files(hub_api, clock, tmp_path):
    store = make_hub(hub_api, clock, tmp_path)
    for index in range(250):
        store.write_text(f"accepted/r/f{index:03d}.jsonl", "x")
    assert store.flush(force=True) is True
    sizes = [len(call["operations"]) for call in hub_api.create_commit_calls]
    assert sum(sizes) == 250
    assert max(sizes) <= 99
    assert len(sizes) == 3
    assert len(hub_api.files) == 250


def test_hub_rejects_a_files_per_commit_of_one_hundred_or_more(hub_api, clock, tmp_path):
    with pytest.raises(ValueError):
        make_hub(hub_api, clock, tmp_path, files_per_commit=100)


def test_hub_interval_and_file_limit_are_configurable(hub_api, clock, tmp_path):
    store = make_hub(hub_api, clock, tmp_path, seconds_per_commit=10, files_per_commit=5)
    for index in range(12):
        store.write_text(f"f{index}.txt", "x")
    store.flush(force=True)
    assert [len(call["operations"]) for call in hub_api.create_commit_calls] == [5, 5, 2]
    store.write_text("late.txt", "x")
    clock.advance(9)
    assert store.maybe_flush() is False
    clock.advance(1)
    assert store.maybe_flush() is True


def test_hub_backs_off_on_429_and_retries(hub_api, clock, tmp_path):
    hub_api.create_failures = [FakeHttpError(429, "rate limited"), FakeHttpError(429, "rate limited")]
    store = make_hub(hub_api, clock, tmp_path)
    store.write_text("a.txt", "x")
    assert store.flush(force=True) is True
    assert clock.sleeps == [5.0, 10.0]
    assert len(hub_api.create_commit_calls) == 3
    assert hub_api.files["a.txt"] == b"x"


def test_hub_honors_a_retry_after_header(hub_api, clock, tmp_path):
    hub_api.create_failures = [FakeHttpError(429, "slow down", headers={"Retry-After": "42"})]
    store = make_hub(hub_api, clock, tmp_path)
    store.write_text("a.txt", "x")
    store.flush(force=True)
    assert clock.sleeps == [42.0]


def test_hub_gives_up_after_the_attempt_limit_and_keeps_the_pending_writes(hub_api, clock, tmp_path):
    hub_api.create_failures = [FakeHttpError(503)] * 3
    store = make_hub(hub_api, clock, tmp_path, max_attempts=3)
    store.write_text("a.txt", "x")
    with pytest.raises(StoreUnavailableError):
        store.flush(force=True)
    assert len(hub_api.create_commit_calls) == 3
    assert store.pending_count == 1
    assert store.read_text("a.txt") == "x"  # still readable from the pending set
    assert store.flush(force=True) is True  # the repository recovered
    assert hub_api.files["a.txt"] == b"x"


def test_hub_does_not_retry_fatal_errors_and_never_shows_a_token(hub_api, clock, tmp_path):
    secret = "hf_" + "A" * 34
    hub_api.create_failures = [FakeHttpError(403, f"forbidden for token {secret}")]
    store = make_hub(hub_api, clock, tmp_path)
    store.write_text("a.txt", "x")
    with pytest.raises(StoreError) as caught:
        store.flush(force=True)
    assert clock.sleeps == []
    assert len(hub_api.create_commit_calls) == 1
    assert secret not in str(caught.value)
    assert "[redacted]" in str(caught.value)


def test_hub_failed_attempts_count_toward_the_hourly_cap(hub_api, clock, tmp_path):
    hub_api.create_failures = [FakeHttpError(503)] * 20
    store = make_hub(hub_api, clock, tmp_path, commits_per_hour=3, max_attempts=10)
    store.write_text("a.txt", "x")
    with pytest.raises(RateLimitedError):
        store.flush(force=True)
    assert len(hub_api.create_commit_calls) == 3
    # An ordinary write while capped is simply buffered, not an error.
    store.write_text("b.txt", "y")
    assert store.pending_count == 2
    clock.advance(3601)
    hub_api.create_failures.clear()
    assert store.flush(force=True) is True
    assert hub_api.files["b.txt"] == b"y"


def test_hub_passes_the_head_it_last_saw_as_the_parent_commit(hub_api, clock, tmp_path):
    store = make_hub(hub_api, clock, tmp_path)
    store.write_text("a.txt", "1")
    store.flush(force=True)
    store.write_text("b.txt", "2")
    store.flush(force=True)
    first, second = hub_api.create_commit_calls
    assert first["parent"] == "c0000"
    assert second["parent"] == hub_api.commit_log[1]["id"]  # the commit made by the first flush


def test_hub_carries_on_when_another_actor_commits_in_between(hub_api, clock, tmp_path):
    store = make_hub(hub_api, clock, tmp_path)
    store.write_text("a.txt", "1")
    store.flush(force=True)
    hub_api.outside_commit({"review/desk/p1.jsonl": b"v"}, "reviewer-desk: 1 file(s) at 2026-11-07T09:00:00Z")
    store.write_text("b.txt", "2")
    assert store.flush(force=True) is True
    assert hub_api.files["b.txt"] == b"2"
    stale, retried = hub_api.create_commit_calls[-2:]
    assert stale["parent"] != retried["parent"] == hub_api.commit_log[1]["id"]


def test_hub_stops_when_another_controller_commits(hub_api, clock, tmp_path):
    store = make_hub(hub_api, clock, tmp_path)
    store.write_text("a.txt", "1")
    store.flush(force=True)
    hub_api.outside_commit({"accepted/x.jsonl": b"v"}, "controller: 3 file(s) at 2026-11-07T09:00:00Z")
    store.write_text("b.txt", "2")
    with pytest.raises(ConcurrentWriterError, match="only one controller"):
        store.flush(force=True)
    assert "b.txt" not in hub_api.files


def test_hub_reads_see_pending_writes_and_deletes(hub_api, clock, tmp_path):
    hub_api.files["old/x.txt"] = b"remote"
    store = make_hub(hub_api, clock, tmp_path)
    assert store.read_text("old/x.txt") == "remote"
    store.write_text("old/x.txt", "local")
    store.write_text("new/y.txt", "new")
    assert store.read_text("old/x.txt") == "local"
    assert store.exists("new/y.txt")
    assert store.list("") == ["new/y.txt", "old/x.txt"]
    store.delete("old/x.txt")
    assert not store.exists("old/x.txt")
    with pytest.raises(NotFound):
        store.read_text("old/x.txt")
    assert store.list("") == ["new/y.txt"]
    assert hub_api.files == {"old/x.txt": b"remote"}  # nothing committed yet


def test_hub_listing_and_missing_files(hub_api, clock, tmp_path):
    hub_api.files.update({"accepted/r/a.jsonl": b"1", "accepted/r/b.jsonl": b"2", "ledger/summary.json": b"{}"})
    store = make_hub(hub_api, clock, tmp_path)
    assert store.list("accepted/") == ["accepted/r/a.jsonl", "accepted/r/b.jsonl"]
    assert store.list("accepted/r/a") == ["accepted/r/a.jsonl"]
    assert store.list("nothing/") == []
    with pytest.raises(NotFound):
        store.read_text("nothing.txt")
    assert not store.exists("nothing.txt")
    assert store.exists("ledger/summary.json")


def test_hub_deletes_only_files_that_exist(hub_api, clock, tmp_path):
    hub_api.files["gone.txt"] = b"x"
    store = make_hub(hub_api, clock, tmp_path)
    store.delete("never-existed.txt")
    assert store.flush(force=True) is True
    assert hub_api.create_commit_calls == []  # nothing to do, so no commit
    store.delete("gone.txt")
    store.write_text("kept.txt", "k")
    store.flush(force=True)
    ops = hub_api.create_commit_calls[0]["operations"]
    assert [type(op) for op in ops] == [CommitOperationAdd, CommitOperationDelete] or {type(op) for op in ops} == {CommitOperationAdd, CommitOperationDelete}
    assert "gone.txt" not in hub_api.files and "kept.txt" in hub_api.files


def test_hub_appends_continue_from_what_is_already_there(hub_api, clock, tmp_path):
    hub_api.files["events/x.jsonl"] = b'{"a":1}\n{"torn":'
    store = make_hub(hub_api, clock, tmp_path)
    store.append_lines("events/x.jsonl", ['{"b":2}'])
    store.append_lines("events/x.jsonl", ['{"c":3}'])
    assert store.read_text("events/x.jsonl") == '{"a":1}\n{"torn":\n{"b":2}\n{"c":3}\n'
    store.flush(force=True)
    assert hub_api.files["events/x.jsonl"].endswith(b'{"c":3}\n')


def test_hub_needs_a_token_only_when_it_talks_to_the_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "")
    store = HubStore("owner/data", cache_dir=tmp_path / "cache")
    store.write_text("a.txt", "x")  # buffered, no network, no error
    with pytest.raises(StoreError, match="HF_TOKEN is not set"):
        store.flush(force=True)
    assert store.pending_count == 1


def test_hub_ensure_exists_creates_a_private_dataset_repo(hub_api, clock, tmp_path):
    make_hub(hub_api, clock, tmp_path).ensure_exists()
    assert hub_api.created_repos == [{"repo_id": "owner/data", "private": True, "repo_type": "dataset", "exist_ok": True}]


def test_hub_one_writer_per_file_is_enforced_by_the_actor_wrapper(hub_api, clock, tmp_path):
    hub = make_hub(hub_api, clock, tmp_path)
    controller = ActorStore(hub, controller_actor("20261107T090000Z-aaaa"))
    controller.write_text(layout.LEDGER_SUMMARY_PATH, "{}")
    with pytest.raises(OwnershipError):
        ActorStore(hub, reviewer_actor("desk")).write_text(layout.LEDGER_SUMMARY_PATH, "forged")
    assert hub.read_text(layout.LEDGER_SUMMARY_PATH) == "{}"


# ------------------------------------------------------------------ the storage bucket


def test_bucket_flushes_by_count_in_one_batch_call(clock, tmp_path):
    api = FakeBucketApi()
    store = make_bucket(api, clock, tmp_path, events_per_flush=3)
    store.write_text("events/a.0000.jsonl", "1")
    store.write_text("events/b.0000.jsonl", "2")
    assert api.batch_calls == []
    store.write_text("events/c.0000.jsonl", "3")
    assert len(api.batch_calls) == 1
    assert sorted(path for _, path in api.batch_calls[0]["add"]) == ["events/a.0000.jsonl", "events/b.0000.jsonl", "events/c.0000.jsonl"]
    assert api.files["events/c.0000.jsonl"] == b"3"


def test_bucket_flushes_by_age_and_respects_a_minimum_gap(clock, tmp_path):
    api = FakeBucketApi()
    store = make_bucket(api, clock, tmp_path, seconds_per_flush=5, min_interval_seconds=2)
    store.write_text("a.txt", "1")
    clock.advance(4)
    assert store.maybe_flush() is False
    clock.advance(1)
    assert store.maybe_flush() is True
    store.write_text("b.txt", "2")
    clock.advance(5)
    assert store.maybe_flush() is True
    assert len(api.batch_calls) == 2


def test_bucket_sends_adds_and_deletes_together_and_deletes_are_real(clock, tmp_path):
    api = FakeBucketApi()
    api.files["packets/desk/p1/images/r1.jpg"] = b"picture"
    store = make_bucket(api, clock, tmp_path)
    store.delete("packets/desk/p1/images/r1.jpg")
    store.write_text("events/e.0000.jsonl", "x")
    assert store.flush(force=True) is True
    assert len(api.batch_calls) == 1
    assert api.batch_calls[0]["delete"] == ["packets/desk/p1/images/r1.jpg"]
    assert "packets/desk/p1/images/r1.jpg" not in api.files
    assert not store.exists("packets/desk/p1/images/r1.jpg")


def test_bucket_retries_only_the_operations_that_failed(clock, tmp_path):
    api = FakeBucketApi()
    api.path_failures = {"b.txt": 1}
    store = make_bucket(api, clock, tmp_path)
    store.write_text("a.txt", "1")
    store.write_text("b.txt", "2")
    assert store.flush(force=True) is True
    assert len(api.batch_calls) == 2
    assert [path for _, path in api.batch_calls[1]["add"]] == ["b.txt"]
    assert api.files == {"a.txt": b"1", "b.txt": b"2"}
    assert clock.sleeps == [2.0]


def test_bucket_treats_deleting_a_missing_file_as_done(clock, tmp_path):
    api = FakeBucketApi()
    store = make_bucket(api, clock, tmp_path)
    store.delete("already-gone.txt")
    assert store.flush(force=True) is True
    assert len(api.batch_calls) == 1
    assert clock.sleeps == []


def test_bucket_backs_off_when_the_whole_call_is_rate_limited(clock, tmp_path):
    api = FakeBucketApi()
    api.call_failures = [FakeHttpError(429), FakeHttpError(503)]
    store = make_bucket(api, clock, tmp_path)
    store.write_text("a.txt", "1")
    assert store.flush(force=True) is True
    assert clock.sleeps == [2.0, 4.0]
    assert api.files["a.txt"] == b"1"


def test_bucket_gives_up_after_repeated_partial_failures(clock, tmp_path):
    api = FakeBucketApi()
    api.path_failures = {"a.txt": 99}
    store = make_bucket(api, clock, tmp_path, max_attempts=3)
    store.write_text("a.txt", "1")
    with pytest.raises(StoreUnavailableError):
        store.flush(force=True)
    assert store.pending_count == 1


def test_bucket_reads_lists_and_reports_missing_files(clock, tmp_path):
    api = FakeBucketApi()
    api.files.update({"packets/desk/p1/packet.json": b"{}", "packets/desk/p1/images/r1.jpg": b"img", "review/desk/p1.jsonl": b"v"})
    store = make_bucket(api, clock, tmp_path)
    assert store.read_bytes("packets/desk/p1/images/r1.jpg") == b"img"
    assert store.read_text("review/desk/p1.jsonl") == "v"
    assert store.list("packets/desk/") == ["packets/desk/p1/images/r1.jpg", "packets/desk/p1/packet.json"]
    assert store.list("packets/desk/p1/pack") == ["packets/desk/p1/packet.json"]
    assert store.exists("review/desk/p1.jsonl") and not store.exists("review/desk/p2.jsonl")
    with pytest.raises(NotFound):
        store.read_bytes("review/desk/p2.jsonl")
    store.write_text("review/desk/p2.jsonl", "new")
    assert store.list("review/") == ["review/desk/p1.jsonl", "review/desk/p2.jsonl"]
    assert store.read_text("review/desk/p2.jsonl") == "new"  # read from the pending set


def test_bucket_appends_make_whole_file_writes(clock, tmp_path):
    api = FakeBucketApi()
    store = make_bucket(api, clock, tmp_path)
    store.append_lines("events/e.0000.jsonl", ["one"])
    store.flush(force=True)
    store.append_lines("events/e.0000.jsonl", ["two", "three"])
    store.flush(force=True)
    assert api.files["events/e.0000.jsonl"] == b"one\ntwo\nthree\n"
    other = make_bucket(api, clock, tmp_path)  # a fresh process continues from the stored file
    other.append_lines("events/e.0000.jsonl", ["four"])
    other.flush(force=True)
    assert api.files["events/e.0000.jsonl"].endswith(b"three\nfour\n")


def test_bucket_ensure_exists_creates_a_private_bucket(clock, tmp_path):
    api = FakeBucketApi()
    make_bucket(api, clock, tmp_path).ensure_exists()
    assert api.created == [{"bucket_id": "owner/state", "private": True, "exist_ok": True}]


def test_bucket_one_writer_per_file_is_enforced_by_the_actor_wrapper(clock, tmp_path):
    api = FakeBucketApi()
    bucket = make_bucket(api, clock, tmp_path)
    desk = ActorStore(bucket, reviewer_actor("desk"))
    desk.write_text("review/desk/p1.jsonl", "mine")
    with pytest.raises(OwnershipError):
        desk.write_text("review/mac/p1.jsonl", "theirs")
    with pytest.raises(OwnershipError):
        desk.delete("packets/desk/p1/images/r1.jpg")
    controller = ActorStore(bucket, controller_actor("20261107T090000Z-aaaa"))
    controller.write_text(layout.events_path("20261107T090000Z-aaaa"), "e")
    with pytest.raises(OwnershipError):
        controller.write_text(layout.events_path("20261107T100000Z-bbbb"), "e")
    with pytest.raises(OwnershipError):
        controller.write_text("review/desk/p1.jsonl", "forged")


def test_bucket_needs_a_token_only_when_it_talks_to_the_bucket(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "")
    store = BucketStore("owner/state", cache_dir=tmp_path / "cache")
    store.write_text("a.txt", "x")
    with pytest.raises(StoreError, match="HF_TOKEN is not set"):
        store.flush(force=True)


def test_missing_file_errors_are_told_apart_from_a_missing_repository(hub_api, clock, tmp_path):
    hub_api.read_failures = [RepositoryNotFoundError()]
    store = make_hub(hub_api, clock, tmp_path)
    with pytest.raises(StoreError):
        store.read_text("anything.txt")
    hub_api.read_failures = [EntryNotFoundError()]
    with pytest.raises(NotFound):
        store.read_text("anything.txt")
