"""A review packet that holds the picture of a row: the client writes the file beside packet.json, the packet check
passes with a picture present, and the clean-up removes it. A row whose picture was withheld has no file."""

from __future__ import annotations

import json

from rps import ids, layout
from rps.layout import controller_actor
from rps.review.client import ReviewerClient
from rps.review.packets import build_packet, publish_packet
from rps.store import ActorStore, LocalStore

from helpers import Builder

TASK = ids.task_id("photos", "1", {"001": "x"})
CALL = ids.call_id(TASK, "001", 1)
LINE = {"line_id": "001", "item_id": "img-001", "spec": "a cat"}


def test_the_client_writes_the_picture_beside_packet_json_checks_and_burns_it(tmp_path):
    remote = LocalStore(tmp_path / "remote", fsync=False)
    builder = Builder()
    stored = [builder.row(CALL, index) for index in range(2)]
    packet = build_packet(
        packet_id=ids.packet_id([f"rev-a:{row['row_id']}" for row in stored]),
        reviewer_id="rev-a",
        brief_version="1",
        created="2026-11-10T09:00:00.000000Z",
        rows=[(stored[0], LINE, b"picture-bytes", "jpg"), (stored[1], LINE, None, None)],
        withheld=[stored[1]["row_id"]],
    )
    publish_packet(ActorStore(remote, controller_actor("20261110T090000Z-aaaa")), packet)
    client = ReviewerClient("rev-a", remote, tmp_path / "desk", fsync=False)
    local, missing = client.pull()
    assert missing == []
    first, second = (local.entry(row_id) for row_id in local.row_ids)
    assert first["image"] == f"images/{stored[0]['row_id']}.jpg" and first["image_withheld"] is False
    assert second["image"] is None and second["image_withheld"] is True
    path = local.image_path(first)
    assert path == local.folder / first["image"] and path.read_bytes() == b"picture-bytes"
    assert local.image_path(second) is None
    for row_id in local.row_ids:
        client.record_verdict(packet.packet_id, row_id, "keep")
    assert client.check(packet.packet_id).ok  # the check passes with a picture present
    result = client.push(packet.packet_id)
    assert result.pushed and result.burned == 1  # the clean-up removes the local copy
    assert not path.exists() and not (local.folder / "images").exists()
    assert json.loads((local.folder / "packet.json").read_text(encoding="utf-8"))["rows"][0]["image"] == first["image"]
    # the controller's shared copy stays until the controller has read the verdicts and burns it itself
    assert remote.list(layout.packet_images_prefix("rev-a", packet.packet_id))
