import json

import pytest

from rps import ids, layout
from rps.provenance import ProvenanceError
from rps.review.packets import (
    VERDICT_SCHEMA,
    PacketError,
    build_packet,
    normalize_verdict,
    packet_files,
    parse_packet,
    parse_verdict_lines,
    publish_packet,
    review_view,
    validate_verdict,
)

from helpers import Builder

TASK = ids.task_id("photos", "1", {"001": "x"})
CALL = ids.call_id(TASK, "001", 1)
LINE = {"line_id": "001", "item_id": "img-001", "spec": {"text": "a cat", "seed_id": "seed-9", "hint": "near a window"}}
SOURCE_BLOCK = {"kind": "seeded", "model": "model-plugh-9", "seed_dataset": "seeds", "seed_id": "seed-9", "license": "MIT"}


def labeled_rows(n=3, **kwargs):
    b = Builder()
    return [b.row(CALL, index, **kwargs) for index in range(n)]


def test_a_valid_verdict_passes():
    assert validate_verdict({"row_id": "r1", "verdict": "keep"}) == []
    assert validate_verdict({"row_id": "r1", "verdict": "keep", "reasons": [], "notes": "fine"}) == []
    assert validate_verdict({"row_id": "r1", "verdict": "reject", "reasons": ["person_visible"], "notes": ""}) == []
    assert normalize_verdict({"row_id": "r1", "verdict": "keep"}) == {
        "row_id": "r1", "verdict": "keep", "reasons": [], "notes": "",
    }


@pytest.mark.parametrize(
    "bad",
    [
        "keep",
        {"verdict": "keep"},
        {"row_id": "", "verdict": "keep"},
        {"row_id": "r1", "verdict": "maybe"},
        {"row_id": "r1", "verdict": "fix"},  # needs a reason
        {"row_id": "r1", "verdict": "reject", "reasons": []},
        {"row_id": "r1", "verdict": "reject", "reasons": ["Not A Code"]},
        {"row_id": "r1", "verdict": "reject", "reasons": ["x" * 41]},
        {"row_id": "r1", "verdict": "keep", "reasons": "text"},
        {"row_id": "r1", "verdict": "keep", "reasons": ["a"] * 11},
        {"row_id": "r1", "verdict": "keep", "notes": 5},
        {"row_id": "r1", "verdict": "keep", "notes": "x" * 2001},
    ],
)
def test_malformed_verdicts_are_refused(bad):
    assert validate_verdict(bad)
    with pytest.raises(PacketError):
        normalize_verdict(bad)


def test_a_note_at_the_limit_is_accepted_and_one_over_is_refused():
    assert validate_verdict({"row_id": "r1", "verdict": "keep", "notes": "x" * 2000}) == []
    assert validate_verdict({"row_id": "r1", "verdict": "keep", "notes": "x" * 2001})


def test_reviewers_cannot_return_edited_rows():
    problems = validate_verdict({"row_id": "r1", "verdict": "keep", "row": {"thoughts": ["rewritten"]}})
    assert any("unexpected field 'row'" in problem for problem in problems)
    assert validate_verdict({"row_id": "r1", "verdict": "fix", "reasons": ["x"], "edited_row": {}})


def test_a_verdict_for_a_row_outside_the_packet_is_refused():
    assert validate_verdict({"row_id": "r9", "verdict": "keep"}, allowed_row_ids=["r1", "r2"])
    assert validate_verdict({"row_id": "r1", "verdict": "keep"}, allowed_row_ids=["r1", "r2"]) == []


def test_the_schema_names_the_same_fields_as_the_validator():
    assert set(VERDICT_SCHEMA["properties"]) == {"row_id", "verdict", "reasons", "notes"}
    assert VERDICT_SCHEMA["additionalProperties"] is False
    assert VERDICT_SCHEMA["properties"]["verdict"]["enum"] == ["keep", "fix", "reject"]
    assert "where it is" in VERDICT_SCHEMA["properties"]["notes"]["description"]


def test_the_review_view_hides_the_writer_and_blanks_source_model():
    (row,) = labeled_rows(1, provider="provider-xyzzy", model="model-plugh-9", source_block=SOURCE_BLOCK)
    view = review_view(row, LINE)
    assert "writer" not in view["row"]
    assert view["row"]["source"] == {**SOURCE_BLOCK, "model": ""}
    assert view["row"]["thoughts"] == row["thoughts"]  # the content is untouched
    assert view["row"]["source_label"] == row["source_label"]
    assert view["row_id"] == row["row_id"]
    # the stored row is not changed by making a view
    assert row["writer"]["provider"] == "provider-xyzzy"
    assert row["source"]["model"] == "model-plugh-9"


def test_the_review_view_carries_the_task_spec_line_with_seed_and_hint():
    (row,) = labeled_rows(1)
    view = review_view(row, LINE)
    assert view["task"] == {"line_id": "001", "item_id": "img-001", "spec": LINE["spec"]}
    assert view["task"]["spec"]["seed_id"] == "seed-9" and view["task"]["spec"]["hint"] == "near a window"
    assert review_view(row)["task"] is None
    # the view owns its data
    view["task"]["spec"]["hint"] = "changed"
    assert LINE["spec"]["hint"] == "near a window"


def test_the_review_view_needs_a_labeled_row():
    with pytest.raises(ProvenanceError):
        review_view({"row_id": "r-bare", "thoughts": ["x"]}, LINE)


def test_build_publish_and_parse_a_packet(local_store):
    rows = labeled_rows(3, source_block=SOURCE_BLOCK)
    packet = build_packet(
        packet_id=ids.packet_id(["rev-a:" + r["row_id"] for r in rows]),
        reviewer_id="rev-a",
        brief_version="7",
        created="2026-11-07T09:00:00.000000Z",
        rows=[(rows[0], LINE, b"jpegbytes", "jpg"), (rows[1], LINE, None, None), (rows[2], None, b"pngbytes", "png")],
    )
    paths = publish_packet(local_store, packet)
    assert paths[-1] == layout.packet_json_path("rev-a", packet.packet_id)  # packet.json last
    assert all(p.startswith(f"packets/rev-a/{packet.packet_id}/") for p in paths)
    document = parse_packet(local_store.read_text(layout.packet_json_path("rev-a", packet.packet_id)))
    assert document["brief_version"] == "7" and document["reviewer_id"] == "rev-a"
    assert document["verdict_schema"] == VERDICT_SCHEMA
    assert [entry["row_id"] for entry in document["rows"]] == [r["row_id"] for r in rows]
    images = [entry["image"] for entry in document["rows"]]
    assert images == [f"images/{rows[0]['row_id']}.jpg", None, f"images/{rows[2]['row_id']}.png"]
    assert local_store.read_bytes(layout.packet_image_path("rev-a", packet.packet_id, rows[0]["row_id"], "jpg")) == b"jpegbytes"
    assert packet.items() == [
        {"row_id": rows[0]["row_id"], "image": images[0]},
        {"row_id": rows[1]["row_id"], "image": None},
        {"row_id": rows[2]["row_id"], "image": images[2]},
    ]
    assert document["rows"][0]["task"]["spec"]["seed_id"] == "seed-9"
    assert document["rows"][2]["task"] is None


def test_no_writer_provider_or_model_string_appears_anywhere_in_packet_json(local_store):
    rows = labeled_rows(3, provider="provider-xyzzy", model="model-plugh-9", source_block=SOURCE_BLOCK)
    packet = build_packet(
        packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="2026-11-07T09:00:00.000000Z",
        rows=[(row, LINE, b"img", "jpg") for row in rows],
    )
    publish_packet(local_store, packet)
    text = local_store.read_text(layout.packet_json_path("rev-a", packet.packet_id))
    assert "provider-xyzzy" not in text
    assert "model-plugh-9" not in text
    assert '"writer"' not in text
    assert json.loads(text)["rows"][0]["row"]["source"]["model"] == ""
    # The row ids, the spec and the content are still there.
    assert rows[0]["row_id"] in text and "a cat" in text and "near a window" in text


def test_a_packet_refuses_rows_without_a_writer_label():
    rows = labeled_rows(2)
    unlabeled = {"row_id": "r-bare", "thoughts": ["x"]}
    with pytest.raises(ProvenanceError):
        build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t",
                     rows=[(rows[0], LINE, None, None), (unlabeled, LINE, None, None)])
    with pytest.raises(PacketError):
        build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t",
                     rows=[(rows[0], LINE, None, None), (rows[0], LINE, None, None)])
    with pytest.raises(PacketError):
        build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t", rows=[])


def test_packet_files_list_pictures_before_the_packet_document():
    rows = labeled_rows(2)
    packet = build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t",
                          rows=[(rows[0], LINE, b"a", "jpg"), (rows[1], LINE, b"b", "jpg")])
    paths = list(packet_files(packet))
    assert paths[-1].endswith("packet.json")
    assert all("/images/" in path for path in paths[:-1])


def test_parse_packet_rejects_broken_documents():
    for bad in ("{", "[]", json.dumps({"format": 99}), json.dumps({"format": 1, "packet_id": "x"}),
                json.dumps({"format": 1, "packet_id": "p", "reviewer_id": "r", "brief_version": "1", "rows": []}),
                json.dumps({"format": 1, "packet_id": "p", "reviewer_id": "r", "brief_version": "1", "rows": [{}]})):
        with pytest.raises(PacketError):
            parse_packet(bad)


def test_verdict_lines_skip_a_torn_last_line_and_report_it():
    text = '{"row_id": "r1", "verdict": "keep"}\n{"row_id": "r2", "verd'
    verdicts, problems = parse_verdict_lines(text)
    assert verdicts == [{"row_id": "r1", "verdict": "keep"}]
    assert len(problems) == 1 and "line 2" in problems[0]
    verdicts, problems = parse_verdict_lines('[1]\n\n{"row_id": "r1", "verdict": "keep"}\n')
    assert len(verdicts) == 1 and len(problems) == 1
