import json

import pytest

from rps import ids, layout
from rps.review.packets import (
    VERDICT_SCHEMA,
    PacketError,
    build_packet,
    normalize_verdict,
    packet_files,
    parse_packet,
    parse_verdict_lines,
    publish_packet,
    validate_verdict,
)

from helpers import Builder

TASK = ids.task_id("photos", "1", {"001": "x"})
CALL = ids.call_id(TASK, "001", 1)


def labeled_rows(n=3):
    b = Builder()
    return [b.row(CALL, index) for index in range(n)]


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


def test_build_publish_and_parse_a_packet(local_store):
    rows = labeled_rows(3)
    packet = build_packet(
        packet_id=ids.packet_id(["rev-a:" + r["row_id"] for r in rows]),
        reviewer_id="rev-a",
        brief_version="7",
        created="2026-11-07T09:00:00.000000Z",
        rows=[(rows[0], b"jpegbytes", "jpg"), (rows[1], None, None), (rows[2], b"pngbytes", "png")],
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
    # Every row in the document still carries its writer label.
    assert all(entry["row"]["writer"]["provider"] == "fake" for entry in document["rows"])


def test_a_packet_refuses_rows_without_a_writer_label():
    rows = labeled_rows(2)
    unlabeled = {"row_id": "r-bare", "thoughts": ["x"]}
    with pytest.raises(Exception, match="writer|label"):
        build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t",
                     rows=[(rows[0], None, None), (unlabeled, None, None)])
    with pytest.raises(PacketError):
        build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t",
                     rows=[(rows[0], None, None), (rows[0], None, None)])
    with pytest.raises(PacketError):
        build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t", rows=[])


def test_packet_files_list_pictures_before_the_packet_document():
    rows = labeled_rows(2)
    packet = build_packet(packet_id="p0123456789ab", reviewer_id="rev-a", brief_version="1", created="t",
                          rows=[(rows[0], b"a", "jpg"), (rows[1], b"b", "jpg")])
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
