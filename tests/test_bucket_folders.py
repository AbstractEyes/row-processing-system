"""A folder inside the storage bucket: every state path lives under it, and two folders of one bucket share nothing."""

from __future__ import annotations

import io
import json

import huggingface_hub
import pytest

from rps import runtime
from rps.config import Config, ConfigError
from rps.events import replay
from rps.layout import STATE_DIRS, PathError, operator_actor, parse_bucket, reviewer_actor
from rps.review import cli as review_cli
from rps.review.client import ReviewerClient
from rps.store import ActorStore, NotFound, OwnershipError
from rps.store.bucket import BucketStore

from fakes import Clock, FakeBatchError, FakeBucketApi
from harness import FakeNow, fake_image_loader
from helpers import make_config


@pytest.fixture
def clock():
    return Clock()


def folder_store(api, clock, tmp_path, bucket, **kwargs):
    return BucketStore(bucket, api=api, clock=clock, sleep=clock.sleep, rng=lambda: 1.0, cache_dir=tmp_path / "cache", **kwargs)


class ListSpy(FakeBucketApi):
    """A fake bucket that remembers what each listing asked for."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def list_bucket_tree(self, bucket_id, prefix=None, *, recursive=None, token=None):
        self.asked.append((bucket_id, prefix))
        return super().list_bucket_tree(bucket_id, prefix, recursive=recursive, token=token)


# ---------------------------------------------------------------------------- the setting


@pytest.mark.parametrize(
    "value, expected",
    [
        ("owner/state", ("owner/state", "")),
        ("owner/state/test", ("owner/state", "test")),
        ("owner/rps-state/prod", ("owner/rps-state", "prod")),
        ("owner/state/runs/2026-11", ("owner/state", "runs/2026-11")),
        ("owner/state/run_1.a-b", ("owner/state", "run_1.a-b")),
        ("owner/state/a/b/c", ("owner/state", "a/b/c")),
    ],
)
def test_a_bucket_setting_is_the_bucket_id_and_an_optional_folder(value, expected):
    assert parse_bucket(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "", "owner", "owner/", "/state", "owner//state", "/owner/state", "own er/state", "owner/sta te",
        "owner/state/", "owner/state//test", "owner/state/test/", "owner/state/test//more",
        "owner/state/.", "owner/state/..", "owner/state/../prod", "owner/state/a/../b", "owner/state/./a",
        "owner/state/.hidden", "owner/state/with space", "owner/state/back\\slash", "owner/state/café",
        "owner/state/" + "x" * 65,
    ],
)
def test_a_bucket_setting_that_is_not_owner_name_and_plain_folder_names_is_refused(value):
    with pytest.raises(PathError):
        parse_bucket(value)


@pytest.mark.parametrize("value", [None, 7, ["owner", "state"]])
def test_a_bucket_setting_must_be_text(value):
    with pytest.raises(PathError):
        parse_bucket(value)


@pytest.mark.parametrize("name", STATE_DIRS)
def test_no_part_of_a_folder_may_be_a_directory_of_the_state_store(name):
    for value in (f"owner/state/{name}", f"owner/state/test/{name}", f"owner/state/{name}/test"):
        with pytest.raises(PathError, match="one of the state store's own directories"):
            parse_bucket(value)
    assert parse_bucket(f"owner/state/{name}s") == ("owner/state", f"{name}s")  # only the exact names are taken


def test_a_config_keeps_the_bucket_as_written_and_a_different_folder_is_a_different_run_setting():
    test_config = make_config(bucket="owner/state/test")
    assert test_config.bucket == "owner/state/test"
    assert Config.from_dict(test_config.to_dict()).bucket == "owner/state/test"
    assert make_config(bucket="owner/state/prod").digest() != test_config.digest()
    assert make_config(bucket="owner/state").bucket == "owner/state"


@pytest.mark.parametrize("bad", ["owner", "owner/state/", "owner/state/..", "owner/state/events", "owner/state/a b", "state"])
def test_a_config_refuses_a_bucket_that_is_not_a_bucket_with_an_optional_folder(bad):
    with pytest.raises(ConfigError, match="^bucket: "):
        make_config(bucket=bad)


# ---------------------------------------------------------------------------- the store


def test_a_store_with_a_folder_keeps_every_path_under_it_and_names_the_bucket_without_it(clock, tmp_path):
    api = FakeBucketApi()
    store = folder_store(api, clock, tmp_path, "owner/state/test")
    assert (store.bucket_id, store.folder) == ("owner/state", "test")
    store.write_text("events/controller-a.0000.jsonl", "e")
    store.write_text("packets/desk/p0123456789ab/packet.json", "{}")
    store.write_bytes("packets/desk/p0123456789ab/images/r1.jpg", b"img")
    store.write_text("hand/queue.jsonl", "h")
    store.write_text("control/tasks.json", "[]")
    store.write_text("control/STOP", "")
    store.flush(force=True)
    assert sorted(api.files) == [
        "test/control/STOP",
        "test/control/tasks.json",
        "test/events/controller-a.0000.jsonl",
        "test/hand/queue.jsonl",
        "test/packets/desk/p0123456789ab/images/r1.jpg",
        "test/packets/desk/p0123456789ab/packet.json",
    ]
    assert {call["bucket"] for call in api.batch_calls} == {"owner/state"}  # the folder is never part of the bucket's name


def test_a_store_without_a_folder_keeps_the_layout_at_the_top_of_the_bucket(clock, tmp_path):
    api = FakeBucketApi()
    store = folder_store(api, clock, tmp_path, "owner/state")
    assert (store.bucket_id, store.folder) == ("owner/state", "")
    store.write_text("events/controller-a.0000.jsonl", "e")
    store.write_text("control/STOP", "")
    store.flush(force=True)
    assert sorted(api.files) == ["control/STOP", "events/controller-a.0000.jsonl"]
    assert store.list("") == ["control/STOP", "events/controller-a.0000.jsonl"]


def test_a_store_refuses_a_bucket_that_is_not_one(clock, tmp_path):
    for bad in ("owner", "owner/state/..", "owner/state/events"):
        with pytest.raises(PathError):
            folder_store(FakeBucketApi(), clock, tmp_path, bad)


def test_two_folders_in_one_bucket_stay_apart(clock, tmp_path):
    api = FakeBucketApi()
    test = folder_store(api, clock, tmp_path, "owner/state/test")
    prod = folder_store(api, clock, tmp_path, "owner/state/prod")
    for store, label in ((test, "test"), (prod, "prod")):
        store.write_text("events/controller-a.0000.jsonl", f"{label} events")
        store.write_text(f"packets/desk/{label}-only/packet.json", label)
        store.write_text("control/STOP", label)
        store.flush(force=True)
    assert api.files["test/events/controller-a.0000.jsonl"] == b"test events"
    assert api.files["prod/events/controller-a.0000.jsonl"] == b"prod events"
    assert test.read_text("events/controller-a.0000.jsonl") == "test events"
    assert prod.read_text("events/controller-a.0000.jsonl") == "prod events"
    assert test.list("") == ["control/STOP", "events/controller-a.0000.jsonl", "packets/desk/test-only/packet.json"]
    assert prod.list("") == ["control/STOP", "events/controller-a.0000.jsonl", "packets/desk/prod-only/packet.json"]
    assert test.list("packets/") == ["packets/desk/test-only/packet.json"]
    assert not test.exists("packets/desk/prod-only/packet.json") and not prod.exists("packets/desk/test-only/packet.json")
    # A delete in one folder leaves the same path in the other alone.
    test.delete("control/STOP")
    test.flush(force=True)
    assert not test.exists("control/STOP") and "test/control/STOP" not in api.files
    assert prod.exists("control/STOP") and prod.read_text("control/STOP") == "prod" and "prod/control/STOP" in api.files


def test_listing_under_one_folder_never_returns_the_files_of_another(clock, tmp_path):
    api = FakeBucketApi()
    api.files.update(
        {
            "test/events/mine.0000.jsonl": b"1",
            "test/packets/desk/p1/packet.json": b"2",
            "prod/events/theirs.0000.jsonl": b"3",
            "prod/packets/desk/p1/packet.json": b"4",
            "test2/events/other.0000.jsonl": b"5",  # a folder whose name only starts like this one's
            "testing/packets/desk/p9/packet.json": b"6",
            "other/test/events/nested.0000.jsonl": b"7",  # this folder's name, but not at the top of the bucket
            "events/root.0000.jsonl": b"8",  # the top of the bucket itself
            "test-notes.txt": b"9",
        }
    )
    test = folder_store(api, clock, tmp_path, "owner/state/test")
    assert test.list("") == ["events/mine.0000.jsonl", "packets/desk/p1/packet.json"]
    assert test.list("events/") == ["events/mine.0000.jsonl"]
    assert test.list("packets/") == ["packets/desk/p1/packet.json"]
    assert test.list("packets/desk/") == ["packets/desk/p1/packet.json"]
    assert test.list("ev") == ["events/mine.0000.jsonl"]  # a part of a name
    assert test.list("events/m") == ["events/mine.0000.jsonl"]
    assert test.list("events/t") == [] and test.list("events/o") == [] and test.list("events/r") == [] and test.list("events/n") == []
    for path in ("events/theirs.0000.jsonl", "events/other.0000.jsonl", "events/root.0000.jsonl", "events/nested.0000.jsonl"):
        assert not test.exists(path)
        with pytest.raises(NotFound):
            test.read_text(path)
    prod = folder_store(api, clock, tmp_path, "owner/state/prod")
    assert prod.list("") == ["events/theirs.0000.jsonl", "packets/desk/p1/packet.json"]
    assert prod.list("events/") == ["events/theirs.0000.jsonl"]
    root = folder_store(api, clock, tmp_path, "owner/state")  # no folder: today's layout, the top of the bucket
    assert root.list("events/") == ["events/root.0000.jsonl"] and root.list("packets/") == []


def test_a_listing_asks_the_bucket_only_about_the_folder(clock, tmp_path):
    api = ListSpy()
    test = folder_store(api, clock, tmp_path, "owner/state/test")
    test.list("events/")
    test.list("packets/desk/")
    test.list("events/m")
    test.list("")
    nested = folder_store(api, clock, tmp_path, "owner/state/runs/2026-11")
    nested.list("review/")
    folder_store(api, clock, tmp_path, "owner/state").list("events/")
    assert api.asked == [
        ("owner/state", "test/events/"),
        ("owner/state", "test/packets/desk/"),
        ("owner/state", "test/events/"),
        ("owner/state", "test/"),
        ("owner/state", "runs/2026-11/review/"),
        ("owner/state", "events/"),
    ]


def test_changes_not_yet_sent_are_seen_by_their_store_path_and_sent_under_the_folder(clock, tmp_path):
    api = FakeBucketApi()
    api.files["test/review/desk/p1.jsonl"] = b"old"
    store = folder_store(api, clock, tmp_path, "owner/state/test")
    store.write_text("events/new.0000.jsonl", "n")
    store.delete("review/desk/p1.jsonl")
    assert store.list("") == ["events/new.0000.jsonl"]
    assert store.exists("events/new.0000.jsonl") and not store.exists("review/desk/p1.jsonl")
    assert store.read_text("events/new.0000.jsonl") == "n"
    assert api.batch_calls == []
    store.flush(force=True)
    assert api.files == {"test/events/new.0000.jsonl": b"n"}
    assert api.batch_calls[0]["delete"] == ["test/review/desk/p1.jsonl"]
    assert [path for _, path in api.batch_calls[0]["add"]] == ["test/events/new.0000.jsonl"]


def test_an_operation_that_fails_is_retried_by_its_own_path_inside_the_folder(clock, tmp_path):
    api = FakeBucketApi()
    api.path_failures = {"test/b.txt": 1}  # the bucket reports the full path
    store = folder_store(api, clock, tmp_path, "owner/state/test")
    store.write_text("a.txt", "1")
    store.write_text("b.txt", "2")
    assert store.flush(force=True) is True
    assert len(api.batch_calls) == 2
    assert [path for _, path in api.batch_calls[1]["add"]] == ["test/b.txt"]
    assert api.files == {"test/a.txt": b"1", "test/b.txt": b"2"}
    assert clock.sleeps == [2.0]
    store.delete("already-gone.txt")  # a file that is gone is the outcome wanted, not a failure to retry
    assert store.flush(force=True) is True
    assert clock.sleeps == [2.0] and api.batch_calls[-1]["delete"] == ["test/already-gone.txt"]


def test_a_failure_that_names_a_file_outside_the_folder_is_not_counted_as_done(clock, tmp_path):
    api = FakeBucketApi()
    api.call_failures = [FakeBatchError([{"path": "elsewhere/c.txt", "error": "temporary failure"}])]
    store = folder_store(api, clock, tmp_path, "owner/state/test")
    store.write_text("a.txt", "1")
    store.write_text("b.txt", "2")
    assert store.flush(force=True) is True
    assert len(api.batch_calls) == 2
    assert sorted(path for _, path in api.batch_calls[1]["add"]) == ["test/a.txt", "test/b.txt"]  # every operation goes again
    assert api.files == {"test/a.txt": b"1", "test/b.txt": b"2"} and clock.sleeps == [2.0]


def test_creating_the_bucket_creates_only_the_bucket_and_never_the_folder(clock, tmp_path):
    api = FakeBucketApi()
    folder_store(api, clock, tmp_path, "owner/state/test").ensure_exists()
    assert api.created == [{"bucket_id": "owner/state", "private": True, "exist_ok": True}]


def test_an_append_continues_from_the_same_folders_file_and_not_from_another_folders(clock, tmp_path):
    api = FakeBucketApi()
    api.files["prod/events/e.0000.jsonl"] = b"prod line\n"
    test = folder_store(api, clock, tmp_path, "owner/state/test")
    test.append_lines("events/e.0000.jsonl", ["one"])
    test.flush(force=True)
    assert api.files["test/events/e.0000.jsonl"] == b"one\n" and api.files["prod/events/e.0000.jsonl"] == b"prod line\n"
    again = folder_store(api, clock, tmp_path, "owner/state/test")  # a fresh process continues from the stored file
    again.append_lines("events/e.0000.jsonl", ["two"])
    again.flush(force=True)
    assert api.files["test/events/e.0000.jsonl"] == b"one\ntwo\n" and api.files["prod/events/e.0000.jsonl"] == b"prod line\n"


def test_a_path_cannot_lead_out_of_the_folder(clock, tmp_path):
    api = FakeBucketApi()
    api.files["prod/events/theirs.0000.jsonl"] = b"theirs"
    test = folder_store(api, clock, tmp_path, "owner/state/test")
    for path in ("../prod/events/theirs.0000.jsonl", "events/../../prod/events/theirs.0000.jsonl", "/prod/events/theirs.0000.jsonl"):
        with pytest.raises(PathError):
            test.read_bytes(path)
        with pytest.raises(PathError):
            test.exists(path)
        with pytest.raises(PathError):
            test.write_text(path, "x")
        with pytest.raises(PathError):
            test.delete(path)
    assert test.list("../prod/") == [] and test.list("../") == []  # a listing that tries is answered with nothing
    assert api.files == {"prod/events/theirs.0000.jsonl": b"theirs"} and api.batch_calls == []


def test_the_one_writer_rules_apply_to_store_paths_inside_a_folder(clock, tmp_path):
    api = FakeBucketApi()
    bucket = folder_store(api, clock, tmp_path, "owner/state/test")
    desk = ActorStore(bucket, reviewer_actor("desk"))
    desk.write_text("review/desk/p1.jsonl", "mine")
    with pytest.raises(OwnershipError):
        desk.write_text("review/mac/p1.jsonl", "theirs")
    with pytest.raises(OwnershipError):
        desk.write_text("prod/review/desk/p1.jsonl", "a sibling folder is not a path of the layout")
    ActorStore(bucket, operator_actor()).write_text("control/STOP", "")
    bucket.flush(force=True)
    assert sorted(api.files) == ["test/control/STOP", "test/review/desk/p1.jsonl"]


# ---------------------------------------------------------------------------- a whole run, in two folders


@pytest.fixture
def shared_bucket(monkeypatch):
    """One fake bucket behind every store a configuration builds, as the hub's client would reach it."""
    api = FakeBucketApi()
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda token=None: api)
    monkeypatch.setenv("HF_TOKEN", "a-token-for-this-test")
    return api


# Reviewers from families whose packets may carry pictures (the routing table decides).
REVIEWERS = [
    {"id": "rev-o", "family": "openai"},
    {"id": "rev-g", "family": "google"},
    {"id": "rev-d", "family": "deepseek"},
]


def run_config(tmp_path, name, bucket):
    return make_config(
        work_dir=str(tmp_path / name),
        bucket=bucket,
        tasks_file="store:control/tasks.json",
        review={  # one row to a packet, so a folder with more rows has more packets
            "packet_size": 1, "reviewers": REVIEWERS, "provider_families": {"fake": "writer-family"},
            "policy": {"audit_fraction": 0.0},
        },
    )


def task(lines):
    return {
        "source": "photos", "batch": "1", "route": "fake-batch",
        "lines": [{"line_id": line, "spec": f"spec for {line}", "item_id": f"img-{line}", "image": f"{line}.jpg"} for line in lines],
    }


def put_tasks(config, tasks):
    """The operator puts the tasks file into the state store, as someone outside the run would."""
    operator = runtime.build_operator_store(config)
    operator.write_text("control/tasks.json", json.dumps(tasks))
    operator.flush(force=True)


def controller_for(config, printed, instance):
    return runtime.build_controller(
        config, printer=printed.append, image_loader=fake_image_loader, now=FakeNow(), sleep=lambda seconds: None,
        instance=instance, tasks_check_seconds=0,
    )


def keys_under(api, folder):
    return sorted(key for key in api.files if key.startswith(folder + "/"))


def packet_ids(api, folder, reviewer):
    prefix = f"{folder}/packets/{reviewer}/"
    return sorted(key.split("/")[3] for key in api.files if key.startswith(prefix) and key.endswith("/packet.json"))


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = review_cli.run([str(item) for item in argv], out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_a_test_run_and_a_production_run_keep_their_state_apart_in_one_bucket(tmp_path, shared_bucket):
    api = shared_bucket
    test_config = run_config(tmp_path, "work-test", "owner/state/test")
    prod_config = run_config(tmp_path, "work-prod", "owner/state/prod")
    test_lines, prod_lines = ("tst1", "tst2"), ("prd1", "prd2", "prd3", "prd4", "prd5", "prd6", "prd7")
    put_tasks(test_config, [task(test_lines)])
    put_tasks(prod_config, [task(prod_lines)])
    assert sorted(api.files) == ["prod/control/tasks.json", "test/control/tasks.json"]  # each run's own tasks file, in its own folder

    printed = []
    for name, config in (("test", test_config), ("prod", prod_config)):
        summary = controller_for(config, printed, f"20261107T091000Z-{name}").run()
        assert summary.reason == "idle" and not summary.problems

    # Everything the two runs wrote is under one folder or the other, and each has the whole layout.
    assert api.files and all(key.startswith(("test/", "prod/")) for key in api.files)
    for folder in ("test", "prod"):
        tops = {key.split("/")[1] for key in keys_under(api, folder)}
        assert {"control", "events", "packets"} <= tops
        assert f"{folder}/events/controller-20261107T091000Z-{folder}.0000.jsonl" in api.files
    # No file of one folder says anything about the other's lines.
    for key in keys_under(api, "test"):
        assert b"prd" not in api.files[key], key
    for key in keys_under(api, "prod"):
        assert b"tst" not in api.files[key], key

    def lines_of(config):
        events = replay(runtime.build_state_store(config)).events
        return {line["line_id"] for event in events if event.kind == "task_created" for line in event.payload["lines"]}

    assert lines_of(test_config) == set(test_lines) and lines_of(prod_config) == set(prod_lines)


def test_a_stop_file_in_one_folder_stops_only_the_run_of_that_folder(tmp_path, shared_bucket):
    api = shared_bucket
    test_config = run_config(tmp_path, "work-test", "owner/state/test")
    prod_config = run_config(tmp_path, "work-prod", "owner/state/prod")
    put_tasks(test_config, [task(("tst1", "tst2"))])
    put_tasks(prod_config, [task(("prd1", "prd2"))])
    operator = runtime.build_operator_store(test_config)
    operator.write_text(test_config.remote_stop_path, "")
    operator.flush(force=True)
    assert "test/control/STOP" in api.files and "prod/control/STOP" not in api.files

    printed = []
    stopped = controller_for(test_config, printed, "20261107T091000Z-test").run()
    assert stopped.reason == "stop_file"
    working = controller_for(prod_config, printed, "20261107T091000Z-prod")
    summary = working.run()
    assert summary.reason == "idle" and working.state.counts()["packets_open"] > 0  # the production run went through its passes
    assert not any(key.startswith("test/packets/") for key in api.files)  # and the stopped run published nothing


def test_the_reviewer_commands_read_and_write_only_the_folder_their_config_names(tmp_path, shared_bucket):
    api = shared_bucket
    configs, paths = {}, {}
    for name, lines in (("test", ("tst1", "tst2")), ("prod", tuple(f"prd{number}" for number in range(1, 8)))):
        config = run_config(tmp_path, f"work-{name}", f"owner/state/{name}")
        put_tasks(config, [task(lines)])
        assert controller_for(config, [], f"20261107T091000Z-{name}").run().reason == "idle"
        configs[name], paths[name] = config, tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(config.to_dict()), encoding="utf-8")

    reviewer = next(item["id"] for item in REVIEWERS if packet_ids(api, "test", item["id"]) and packet_ids(api, "prod", item["id"]))
    waiting = {name: packet_ids(api, name, reviewer) for name in ("test", "prod")}
    assert waiting["test"] and waiting["prod"] and not set(waiting["test"]) & set(waiting["prod"])  # packet ids come from the rows in them

    def waiting_in_status(name, into):
        code, out, err = cli("status", "--reviewer", reviewer, "--config", paths[name], "--into", into)
        assert code == 0 and not err
        return int(out.split("waiting in the shared store, not pulled yet: ")[1].split()[0])

    assert waiting_in_status("test", tmp_path / "desk-test") == len(waiting["test"])
    assert waiting_in_status("prod", tmp_path / "desk-prod") == len(waiting["prod"])

    # Pulling takes a packet of the folder in the config, and its pictures come from there too.
    into = tmp_path / "desk-test"
    code, out, err = cli("pull", "--reviewer", reviewer, "--config", paths["test"], "--into", into)
    assert code == 0 and not err and "are not in the shared store" not in out
    local = ReviewerClient(reviewer, None, into).local_packets()
    assert len(local) == 1 and local[0].packet_id in waiting["test"]
    packet = local[0]
    assert len(list((into / packet.packet_id / "images").iterdir())) > 0

    # Pushing the verdicts writes under the same folder, and only that folder's controller hears of them.
    for row_id in packet.row_ids:
        code, out, err = cli("verdict", "--packet", packet.packet_id, "--row", row_id, "--verdict", "keep", "--into", into)
        assert code == 0, err
    code, out, err = cli("push", "--packet", packet.packet_id, "--config", paths["test"], "--into", into)
    assert code == 0 and not err
    assert f"test/review/{reviewer}/{packet.packet_id}.jsonl" in api.files
    assert not any(key.startswith("prod/review/") for key in api.files) and not any(key.startswith("review/") for key in api.files)
    assert waiting_in_status("test", into) == len(waiting["test"]) - 1
    assert waiting_in_status("prod", tmp_path / "desk-prod") == len(waiting["prod"])

    heard = {}
    for name in ("test", "prod"):
        listener = controller_for(configs[name], [], f"20261107T092000Z-{name}")
        listener.step()
        heard[name] = sum(len(row.verdicts) for row in listener.state.rows.values())
    assert heard["test"] == len(packet.row_ids) and heard["prod"] == 0
