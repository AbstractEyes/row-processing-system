import threading

import pytest

from rps import layout
from rps.ids import packet_id, task_id
from rps.layout import PathError, controller_actor, operator_actor, reviewer_actor
from rps.store import ActorStore, LocalStore, NotFound, OwnershipError

TASK = task_id("photos", "1", {"001": "x"})
PACKET = packet_id(["r1"])


def test_local_store_basic_operations(local_store):
    store = local_store
    assert not store.exists("a/b.txt")
    store.write_text("a/b.txt", "hello")
    assert store.exists("a/b.txt")
    assert store.read_text("a/b.txt") == "hello"
    store.write_bytes("a/c.bin", b"\x00\x01")
    assert store.read_bytes("a/c.bin") == b"\x00\x01"
    store.write_text("other/d.txt", "x")
    assert store.list() == ["a/b.txt", "a/c.bin", "other/d.txt"]
    assert store.list("a/") == ["a/b.txt", "a/c.bin"]
    assert store.list("a/b") == ["a/b.txt"]
    assert store.list("zzz/") == []
    store.delete("a/b.txt")
    store.delete("a/b.txt")  # deleting a missing file is not an error
    assert not store.exists("a/b.txt")
    with pytest.raises(NotFound):
        store.read_text("a/b.txt")


def test_local_store_overwrite_is_whole_file(local_store):
    local_store.write_text("f.txt", "long content here")
    local_store.write_text("f.txt", "short")
    assert local_store.read_text("f.txt") == "short"
    assert [p for p in local_store.list() if p.endswith(".tmp")] == []


def test_append_lines_adds_newlines_and_keeps_order(local_store):
    local_store.append_lines("log.jsonl", ["one", "two"])
    local_store.append_lines("log.jsonl", ["three"])
    assert local_store.read_text("log.jsonl") == "one\ntwo\nthree\n"
    local_store.append_lines("log.jsonl", [])
    assert local_store.read_text("log.jsonl") == "one\ntwo\nthree\n"


def test_append_rejects_line_breaks(local_store):
    with pytest.raises(ValueError):
        local_store.append_lines("log.jsonl", ["bad\nline"])
    with pytest.raises(ValueError):
        local_store.append_lines("log.jsonl", ["bad\rline"])
    assert not local_store.exists("log.jsonl")


def test_append_after_a_torn_line_starts_a_new_line(local_store, tmp_path):
    (tmp_path / "store").mkdir(exist_ok=True)
    (tmp_path / "store" / "log.jsonl").write_bytes(b'{"ok":1}\n{"torn":')
    local_store.append_lines("log.jsonl", ['{"ok":2}'])
    text = local_store.read_text("log.jsonl")
    assert text == '{"ok":1}\n{"torn":\n{"ok":2}\n'


def test_concurrent_appends_never_interleave_inside_a_line(local_store):
    def writer(tag):
        for index in range(30):
            local_store.append_lines("shared.jsonl", [f"{tag}-{index}-" + "x" * 200])

    threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = local_store.read_text("shared.jsonl").split("\n")
    assert lines[-1] == ""
    body = lines[:-1]
    assert len(body) == 120
    assert all(line.endswith("x" * 200) for line in body)


@pytest.mark.parametrize("bad", ["", "/abs", "a//b", "a/../b", "../x", "a\\b", "C:/x", "a/./b"])
def test_paths_are_validated(local_store, bad):
    with pytest.raises(PathError):
        local_store.write_text(bad, "x")
    with pytest.raises(PathError):
        local_store.read_text(bad)


def test_a_symlink_cannot_escape_the_root(local_store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = local_store.root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    with pytest.raises(PathError):
        local_store.write_text("link/file.txt", "x")


def test_events_are_owned_by_the_run_named_in_the_path():
    run_a = controller_actor("20261107T090000Z-aaaa")
    run_b = controller_actor("20261107T100000Z-bbbb")
    path_a = layout.events_path(run_a.name, 0)
    assert layout.may_write(run_a, path_a)
    assert not layout.may_write(run_b, path_a)
    assert not layout.may_write(reviewer_actor("desk"), path_a)
    assert layout.parse_events_path(path_a) == (run_a.name, 0)


def test_event_paths_sort_by_run_then_part():
    paths = [
        layout.events_path("20261107T100000Z-bbbb", 0),
        layout.events_path("20261107T090000Z-aaaa", 10),
        layout.events_path("20261107T090000Z-aaaa", 2),
        "events/readme.txt",
    ]
    ordered = layout.sort_event_paths(paths)
    assert ordered == [
        layout.events_path("20261107T090000Z-aaaa", 2),
        layout.events_path("20261107T090000Z-aaaa", 10),
        layout.events_path("20261107T100000Z-bbbb", 0),
    ]


def test_controller_may_write_its_files_but_not_reviewer_files(local_store):
    run = controller_actor("20261107T090000Z-aaaa")
    store = ActorStore(local_store, run)
    store.append_lines(layout.events_path(run.name), ["e1"])
    store.write_text(layout.packet_json_path(PACKET), "{}")
    store.write_bytes(layout.packet_image_path(PACKET, "row1", "jpg"), b"img")
    store.write_text(layout.accepted_path("route-a", TASK), "{}\n")
    store.write_text(layout.LEDGER_SUMMARY_PATH, "{}")
    with pytest.raises(OwnershipError):
        store.write_text(layout.verdicts_path("desk", PACKET), "forged")
    with pytest.raises(OwnershipError):
        store.delete(layout.verdicts_path("desk", PACKET))
    with pytest.raises(OwnershipError):
        store.write_text(layout.events_path("20261107T100000Z-bbbb"), "x")
    with pytest.raises(OwnershipError):
        store.write_text("control/STOP", "x")
    with pytest.raises(OwnershipError):
        store.write_text("README.md", "unowned")


def test_a_reviewer_writes_only_under_its_own_folder(local_store):
    reviewer = reviewer_actor("desk")
    store = ActorStore(local_store, reviewer)
    store.write_text(layout.verdicts_path("desk", PACKET), "{}\n")
    store.append_lines(layout.verdicts_path("desk", PACKET), ["{}"])
    with pytest.raises(OwnershipError):
        store.write_text(layout.verdicts_path("mac", PACKET), "x")
    with pytest.raises(OwnershipError):
        store.write_text(layout.events_path("20261107T090000Z-aaaa"), "x")
    with pytest.raises(OwnershipError):
        store.delete(layout.packet_json_path(PACKET))
    with pytest.raises(OwnershipError):
        store.write_text("review/desk", "x")  # no file name under the reviewer folder
    with pytest.raises(PathError):
        store.write_text("review/desk/../mac/x.jsonl", "x")


def test_reads_are_open_to_every_actor(local_store):
    ActorStore(local_store, controller_actor("20261107T090000Z-aaaa")).write_text(layout.packet_json_path(PACKET), "rows")
    reader = ActorStore(local_store, reviewer_actor("desk"))
    assert reader.read_text(layout.packet_json_path(PACKET)) == "rows"
    assert reader.list("packets/") == [layout.packet_json_path(PACKET)]
    assert reader.exists(layout.packet_json_path(PACKET))


def test_the_operator_owns_the_control_folder(local_store):
    ActorStore(local_store, operator_actor()).write_text(layout.CONTROL_STOP_PATH, "stop")
    assert local_store.exists("control/STOP")
    with pytest.raises(OwnershipError):
        ActorStore(local_store, controller_actor("20261107T090000Z-aaaa")).delete(layout.CONTROL_STOP_PATH)


def test_delete_prefix_respects_ownership(local_store):
    controller = ActorStore(local_store, controller_actor("20261107T090000Z-aaaa"))
    controller.write_bytes(layout.packet_image_path(PACKET, "r1", "jpg"), b"1")
    controller.write_bytes(layout.packet_image_path(PACKET, "r2", "png"), b"2")
    controller.write_text(layout.packet_json_path(PACKET), "{}")
    assert controller.delete_prefix(layout.packet_images_prefix(PACKET)) == 2
    assert controller.list(layout.packet_dir(PACKET) + "/") == [layout.packet_json_path(PACKET)]
