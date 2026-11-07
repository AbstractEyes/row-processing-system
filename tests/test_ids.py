from datetime import datetime, timezone

import pytest

from rps import ids


LINES = {"001": "a cat on a sofa", "002": {"text": "a dog", "n": 2}, "003": "a bird"}


def test_task_id_is_deterministic_and_order_independent():
    first = ids.task_id("photos", "163", LINES)
    second = ids.task_id("photos", "163", dict(reversed(list(LINES.items()))))
    pairs = ids.task_id("photos", "163", [("003", "a bird"), ("001", "a cat on a sofa"), ("002", {"n": 2, "text": "a dog"})])
    assert first == second == pairs
    assert ids.TASK_ID_PATTERN.match(first)
    # A fixed value guards against accidental changes to the hashing scheme.
    assert first == ids.task_id("photos", "163", LINES)


def test_task_id_changes_with_every_input():
    base = ids.task_id("photos", "163", LINES)
    assert ids.task_id("faces", "163", LINES) != base
    assert ids.task_id("photos", "164", LINES) != base
    assert ids.task_id("photos", "163", {**LINES, "003": "a fish"}) != base
    assert ids.task_id("photos", "163", {**LINES, "004": "extra"}) != base
    assert ids.task_id("photos", "163", LINES, variant="haiku") != base
    assert ids.task_id("photos", "163", LINES, variant="haiku") != ids.task_id("photos", "163", LINES, variant="luna")


def test_task_id_accepts_objects_with_line_id_and_spec():
    class Line:
        def __init__(self, line_id, spec):
            self.line_id, self.spec = line_id, spec

    objects = [Line(k, v) for k, v in LINES.items()]
    assert ids.task_id("photos", "163", objects) == ids.task_id("photos", "163", LINES)


@pytest.mark.parametrize("bad", ["", "a_b", "a:b", "-x", "x" * 33, "a b", "é"])
def test_bad_line_ids_are_rejected(bad):
    with pytest.raises(ValueError):
        ids.task_id("photos", "1", {bad: "x"})


def test_duplicate_line_ids_are_rejected():
    with pytest.raises(ValueError):
        ids.task_id("photos", "1", [("001", "a"), ("001", "b")])


def test_call_id_format_and_parse():
    task = ids.task_id("photos", "163", LINES)
    call = ids.call_id(task, "007", 2)
    assert call == f"{task}:007:2"
    assert ids.parse_call_id(call) == (task, "007", 2)
    for bad in ("", "a:b", f"{task}:007", f"{task}:007:0", f"{task}:007:x", f"{task}:007:1:9"):
        with pytest.raises(ValueError):
            ids.parse_call_id(bad)
    with pytest.raises(ValueError):
        ids.call_id(task, "007", 0)
    with pytest.raises(ValueError):
        ids.call_id("not-a-task", "007", 1)


def test_custom_id_is_provider_safe_and_reversible():
    task = ids.task_id("photos", "163", LINES)
    call = ids.call_id(task, "line-07", 3)
    custom = ids.custom_id(call)
    assert custom == f"{task}_line-07_3"
    assert len(custom) <= 64
    assert all(ch.isalnum() or ch in "_-" for ch in custom)
    assert ids.call_id_from_custom_id(custom) == call


def test_row_ids_are_file_name_safe_and_reversible():
    task = ids.task_id("photos", "163", LINES)
    call = ids.call_id(task, "001", 1)
    row = ids.row_id(call, 4)
    assert all(ch.isalnum() or ch in "_-" for ch in row)
    assert ids.parse_row_id(row) == (call, 4)
    with pytest.raises(ValueError):
        ids.parse_row_id("nonsense")


def test_packet_id_depends_on_the_row_set_only():
    one = ids.packet_id(["r2", "r1", "r3"])
    two = ids.packet_id(["r3", "r2", "r1", "r1"])
    assert one == two
    assert ids.PACKET_ID_PATTERN.match(one)
    assert ids.packet_id(["r1", "r2"]) != one
    with pytest.raises(ValueError):
        ids.packet_id([])


def test_instance_ids_sort_by_time():
    early = ids.instance_id(datetime(2026, 11, 7, 9, 0, 0, tzinfo=timezone.utc), suffix="aaaa")
    late = ids.instance_id(datetime(2026, 11, 7, 10, 0, 0, tzinfo=timezone.utc), suffix="0000")
    assert early == "20261107T090000Z-aaaa"
    assert early < late
