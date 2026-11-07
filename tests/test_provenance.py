import json

import pytest

from rps import ids
from rps.provenance import (
    HUMAN_MODEL,
    HUMAN_PROVIDER,
    HUMAN_ROUTE,
    ProvenanceError,
    SourceLabel,
    WriterLabel,
    has_label,
    human_writer_label,
    is_human_row,
    require_label,
    reserved_keys_in,
    rows_to_lines,
    stamp_human_row,
    stamp_row,
)

TASK = ids.task_id("photos", "1", {"001": "x"})
SOURCE = SourceLabel(dataset="photos", item_id="img-0001", license="CC BY-NC")


def writer(**overrides):
    fields = dict(
        provider="fake", model="fake-model", route="batch", effort="low", date="2026-11-07",
        task_id=TASK, pool_version="1", image_size=512,
    )
    fields.update(overrides)
    return WriterLabel(**fields)


def test_stamp_adds_the_writer_and_source_labels():
    row = stamp_row({"id": "a1", "thoughts": ["x"]}, row_id="r1", writer=writer(), source=SOURCE)
    assert row["id"] == "a1"
    assert row["row_id"] == "r1"
    assert row["writer"] == {
        "provider": "fake", "model": "fake-model", "route": "batch", "effort": "low", "date": "2026-11-07",
        "task_id": TASK, "pool_version": "1", "image_size": 512,
    }
    assert row["source_label"] == {"dataset": "photos", "item_id": "img-0001", "license": "CC BY-NC"}
    require_label(row)


def test_a_model_cannot_supply_the_labels():
    forged = {
        "id": "a1",
        "writer": {"provider": "human", "model": "phil", "route": "hand"},
        "row_id": "forged",
        "source_label": {"dataset": "x"},
    }
    assert reserved_keys_in(forged) == ["row_id", "writer", "source_label"]
    row = stamp_row(forged, row_id="r1", writer=writer(), source=SOURCE)
    assert row["writer"]["provider"] == "fake"
    assert row["row_id"] == "r1"
    assert row["source_label"]["dataset"] == "photos"
    assert not is_human_row(row)


def test_image_size_and_effort_may_be_none():
    label = writer(image_size=None, effort=None)
    assert label.to_dict()["image_size"] is None
    assert label.to_dict()["effort"] is None
    assert WriterLabel.from_dict(label.to_dict()) == label


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": ""},
        {"model": " "},
        {"route": "carrier-pigeon"},
        {"date": "7 Nov 2026"},
        {"task_id": "nope"},
        {"pool_version": ""},
        {"image_size": 0},
        {"image_size": True},
        {"effort": ""},
    ],
)
def test_incomplete_writer_labels_are_refused(overrides):
    with pytest.raises(ProvenanceError):
        writer(**overrides)


@pytest.mark.parametrize("missing", ["dataset", "item_id", "license"])
def test_incomplete_source_labels_are_refused(missing):
    fields = {"dataset": "photos", "item_id": "i1", "license": "MIT", missing: ""}
    with pytest.raises(ProvenanceError):
        SourceLabel(**fields)


def test_require_label_refuses_rows_without_a_complete_label():
    good = stamp_row({"id": 1}, row_id="r1", writer=writer(), source=SOURCE)
    require_label(good)
    assert has_label(good)
    for broken in (
        {"id": 1},
        {**good, "writer": None},
        {**good, "writer": {k: v for k, v in good["writer"].items() if k != "model"}},
        {**good, "writer": {**good["writer"], "extra": 1}},
        {**good, "source_label": {"dataset": "x"}},
        {k: v for k, v in good.items() if k != "row_id"},
        "not a row",
    ):
        assert not has_label(broken)
        with pytest.raises(ProvenanceError):
            require_label(broken)


def test_rows_to_lines_refuses_unlabeled_rows_and_writes_json():
    good = stamp_row({"id": 1, "text": "café"}, row_id="r1", writer=writer(), source=SOURCE)
    lines = rows_to_lines([good])
    assert len(lines) == 1 and "\n" not in lines[0]
    assert json.loads(lines[0]) == good
    with pytest.raises(ProvenanceError):
        rows_to_lines([good, {"id": 2}])


def test_a_human_written_row_carries_the_human_label():
    row = stamp_human_row(
        {"id": "h1", "thoughts": ["written by hand"]},
        row_id="r1-h1",
        task_id=TASK,
        source=SOURCE,
        date="2026-11-07",
        pool_version="1",
    )
    assert row["writer"]["provider"] == HUMAN_PROVIDER == "human"
    assert row["writer"]["model"] == HUMAN_MODEL == "phil"
    assert row["writer"]["route"] == HUMAN_ROUTE == "hand"
    assert row["writer"]["image_size"] is None
    assert is_human_row(row)
    require_label(row)
    assert human_writer_label(task_id=TASK, date="2026-11-07", pool_version="1").route == "hand"


def test_a_model_row_is_not_a_human_row():
    row = stamp_row({"id": 1}, row_id="r1", writer=writer(), source=SOURCE)
    assert not is_human_row(row)
