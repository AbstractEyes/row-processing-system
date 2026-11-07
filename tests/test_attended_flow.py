"""An attended reviewer works through the command line against a real controller run."""

import io
import json

from rps import runtime
from rps.config import Config
from rps.events import replay
from rps.review.cli import EXIT_OK, run
from rps.review.client import ReviewerClient
from rps.store.local import LocalStore

from harness import fake_image_loader, task_spec
from helpers import make_config

REVIEWERS = ("rev-a", "rev-b", "rev-c")
# Families the routing table lets see public pictures, so the packets carry them.
REVIEW = {
    "packet_size": 3,
    "reviewers": [
        {"id": "rev-a", "family": "openai"},
        {"id": "rev-b", "family": "google"},
        {"id": "rev-c", "family": "deepseek"},
    ],
    "provider_families": {"fake": "writer-family"},
    "policy": {"audit_fraction": 0.0},
}


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = run(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def build(tmp_path):
    """A controller on local folders, and the config file the reviewers' machines would load."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(make_config(work_dir=str(tmp_path / "work"), review=REVIEW).to_dict()), encoding="utf-8")
    config = Config.load(config_path)
    controller = runtime.build_controller(
        config, printer=lambda text: None, image_loader=fake_image_loader, sleep=lambda seconds: None
    )
    return config, config_path, controller


def work_through_the_cli(reviewer, config_path, desk, verdict_for):
    """Pull, judge every row, check and push one reviewer's waiting packet. Returns the pulled packet.json texts."""
    into = str(desk / reviewer)
    code, out, err = cli("pull", "--reviewer", reviewer, "--config", str(config_path), "--into", into)
    assert code == EXIT_OK, err
    client = ReviewerClient(reviewer, None, into)
    texts = []
    for packet in client.local_packets():
        texts.append((packet.folder / "packet.json").read_text(encoding="utf-8"))
        for row_id in packet.row_ids:
            verdict, reasons, notes = verdict_for(row_id)
            code, out, err = cli(
                "verdict", "--packet", packet.packet_id, "--row", row_id, "--verdict", verdict,
                "--reasons", reasons, "--notes", notes, "--into", into,
            )
            assert code == EXIT_OK, err
        code, out, err = cli("check", "--packet", packet.packet_id, "--into", into)
        assert code == EXIT_OK and "0 rows still without a verdict" in out, err
        code, out, err = cli("push", "--packet", packet.packet_id, "--config", str(config_path), "--into", into)
        assert code == EXIT_OK and "burned 1 local pictures" in out, err
    return texts


def test_attended_reviewers_settle_a_whole_run_through_the_command_line(tmp_path):
    config, config_path, controller = build(tmp_path)
    controller.add_task(task_spec(("001", "002", "003"), image="picture.jpg"))
    assert controller.run().reason == "idle"
    assert controller.state.counts()["packets_open"] == 3
    state_store = LocalStore(config.work_path("state"), fsync=False)
    assert len([p for p in state_store.list("packets/") if "/images/" in p]) == 3

    desk = tmp_path / "desk"
    packet_texts = []
    for reviewer in REVIEWERS:
        packet_texts += work_through_the_cli(reviewer, config_path, desk, lambda row_id: ("keep", "", ""))

    # the reviewers saw the rows blind, and burned their own copies of the pictures
    assert len(packet_texts) == 3
    for text in packet_texts:
        assert '"writer"' not in text and "fake-model" not in text
        assert "spec for" in text and "images/" in text
    assert list(desk.rglob("*.jpg")) == []

    # the controller reads the verdicts, accepts the rows and burns the shared pictures
    assert controller.run().reason == "idle"
    assert controller.state.counts()["rows"] == {"accepted": 3}
    assert [p for p in state_store.list("packets/") if "/images/" in p] == []
    ship_store = LocalStore(config.work_path("ship"), fsync=False)
    shipped = [json.loads(line) for path in ship_store.list("accepted/") for line in ship_store.read_text(path).splitlines()]
    assert len(shipped) == 3 and all(row["writer"]["provider"] == "fake" for row in shipped)
    kinds = [event.kind for event in replay(state_store).events]
    assert kinds.count("verdict_received") == 3 and kinds.count("packet_burned") == 3


def test_a_reject_from_an_attended_reviewer_sends_the_row_to_a_second_family(tmp_path):
    _config, config_path, controller = build(tmp_path)
    controller.add_task(task_spec(("001",), image="picture.jpg"))
    controller.run()
    desk = tmp_path / "desk"

    def reject(row_id):
        return "reject", "bad_thought,off_topic", "thought 2 describes the picture's file name, not what is in it"

    (first_packet,) = [p for r in REVIEWERS for p in controller.state.packets.values() if p.reviewer_id == r]
    work_through_the_cli(first_packet.reviewer_id, config_path, desk, reject)
    controller.run()
    assert controller.state.counts()["packets_open"] == 1  # a second reviewer now holds the row
    (second_packet,) = controller.state.open_packets()
    assert second_packet.reviewer_id != first_packet.reviewer_id and second_packet.family != first_packet.family
    work_through_the_cli(second_packet.reviewer_id, config_path, desk, lambda row_id: ("keep", "", ""))
    controller.run()
    # one reject and one keep settle nothing: the third family is asked, and two keeps then accept the row
    (third_packet,) = controller.state.open_packets()
    assert third_packet.family not in (first_packet.family, second_packet.family)
    work_through_the_cli(third_packet.reviewer_id, config_path, desk, lambda row_id: ("keep", "", ""))
    controller.run()
    (row,) = controller.state.rows.values()
    assert [v["verdict"] for v in row.verdicts] == ["reject", "keep", "keep"] and row.status == "accepted"
    assert row.verdicts[0]["notes"].startswith("thought 2 describes")  # the note that named the problem is kept
    assert row.verdicts[0]["reasons"] == ["bad_thought", "off_topic"]
