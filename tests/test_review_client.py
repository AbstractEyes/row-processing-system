import io
import json
import sys

import pytest

from rps import ids, layout
from rps.config import Config
from rps.layout import controller_actor
from rps.review.cli import EXIT_FAILED, EXIT_INVALID, EXIT_OK, EXIT_REFUSED, run
from rps.review.client import CommandReviewer, ReviewError, ReviewerClient, ReviewItem
from rps.review.packets import build_packet, publish_packet
from rps.store import ActorStore, LocalStore, OwnershipError

from helpers import Builder

TASK = ids.task_id("photos", "1", {"001": "x"})
CALL = ids.call_id(TASK, "001", 1)
LINE = {"line_id": "001", "item_id": "img-001", "spec": "a cat"}


def publish(remote, reviewer="rev-a", rows=4, with_images=True):
    b = Builder()
    stored = [b.row(CALL, index) for index in range(rows)]
    packet = build_packet(
        packet_id=ids.packet_id([f"{reviewer}:{r['row_id']}" for r in stored]),
        reviewer_id=reviewer,
        brief_version="1",
        created="2026-11-07T09:00:00.000000Z",
        rows=[(row, LINE, b"picture-%d" % n if with_images else None, "jpg") for n, row in enumerate(stored)],
    )
    publish_packet(ActorStore(remote, controller_actor("20261107T090000Z-aaaa")), packet)
    return packet


@pytest.fixture
def remote(tmp_path):
    return LocalStore(tmp_path / "remote", fsync=False)


@pytest.fixture
def client(remote, tmp_path):
    return ReviewerClient("rev-a", remote, tmp_path / "desk", fsync=False)


def keep(item):
    return {"verdict": "keep"}


def test_pull_copies_the_packet_and_its_pictures(remote, client, tmp_path):
    packet = publish(remote)
    local, missing = client.pull()
    assert missing == [] and local.packet_id == packet.packet_id
    assert (local.folder / "packet.json").is_file()
    assert sorted(p.name for p in (local.folder / "images").iterdir()) == sorted(f"{rid}.jpg" for rid in local.row_ids)
    assert local.entry(local.row_ids[0])["task"]["spec"] == "a cat"
    assert (local.folder / "images" / f"{local.row_ids[1]}.jpg").read_bytes() == b"picture-1"


def test_pull_with_nothing_waiting_returns_none(client):
    assert client.pull() == (None, [])


def test_pull_resumes_a_local_packet_before_fetching_a_new_one(remote, client):
    first = publish(remote, rows=2)
    local, _ = client.pull()
    assert local.packet_id == first.packet_id
    second = publish(remote, rows=3)
    again, _ = client.pull()
    assert again.packet_id == first.packet_id  # not finished, so it comes back
    client.record_verdict(first.packet_id, local.row_ids[0], "keep")
    client.record_verdict(first.packet_id, local.row_ids[1], "keep")
    assert client.push(first.packet_id).pushed
    nxt, _ = client.pull()
    assert nxt.packet_id == second.packet_id


def test_pull_skips_packets_that_already_have_pushed_verdicts(remote, client, tmp_path):
    packet = publish(remote, rows=2)
    local, _ = client.pull()
    for row_id in local.row_ids:
        client.record_verdict(packet.packet_id, row_id, "keep")
    client.push(packet.packet_id)
    fresh = ReviewerClient("rev-a", remote, tmp_path / "other-machine", fsync=False)
    assert fresh.pending_packet_ids() == []
    assert fresh.pull() == (None, [])


def test_packets_for_other_reviewers_are_not_listed_or_accepted(remote, client):
    publish(remote, reviewer="rev-b", rows=2)
    assert client.remote_packet_ids() == []
    assert client.pull() == (None, [])
    tampered = publish(remote, reviewer="rev-a", rows=2)
    path = layout.packet_json_path("rev-a", tampered.packet_id)
    document = json.loads(remote.read_text(path))
    document["reviewer_id"] = "rev-b"
    remote.write_text(path, json.dumps(document))
    with pytest.raises(ReviewError, match="does not belong"):
        client.download(tampered.packet_id)


def test_a_missing_picture_is_reported_not_fatal(remote, client):
    packet = publish(remote, rows=2)
    first = f"images/{packet.rows[0].row_id}.jpg"
    remote.delete(f"{layout.packet_dir('rev-a', packet.packet_id)}/{first}")
    local, missing = client.pull()
    assert missing == [first]
    entry = local.entry(packet.rows[0].row_id)
    assert local.image_path(entry) is None


def test_record_verdict_validates_and_appends_to_the_local_verdict_file(remote, client):
    packet = publish(remote, rows=3)
    local, _ = client.pull()
    first, second, third = local.row_ids
    record = client.record_verdict(packet.packet_id, first, "keep", notes="fine")
    assert record == {"row_id": first, "verdict": "keep", "reasons": [], "notes": "fine"}
    client.record_verdict(packet.packet_id, second, "reject", ["person_visible"], "a person stands in the doorway, thought 2")
    lines = local.verdicts_file.read_text().strip().split("\n")
    assert [json.loads(line)["row_id"] for line in lines] == [first, second]
    assert set(client.judged_rows(local)) == {first, second}
    assert third not in client.judged_rows(local)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"verdict": "maybe"}, "verdict must be one of"),
        ({"verdict": "fix"}, "needs at least one reason"),
        ({"verdict": "reject", "reasons": ["Not A Code"]}, "not a short code"),
        ({"verdict": "keep", "notes": "x" * 2001}, "at most 2000"),
        ({"verdict": "keep", "row_id": "nope"}, "not in the packet"),
    ],
)
def test_bad_verdicts_are_refused_and_nothing_is_written(remote, client, kwargs, message):
    packet = publish(remote, rows=2)
    local, _ = client.pull()
    args = {"row_id": local.row_ids[0], "reasons": [], "notes": ""}
    args.update(kwargs)
    with pytest.raises(ReviewError, match=message):
        client.record_verdict(packet.packet_id, args["row_id"], args["verdict"], args["reasons"], args["notes"])
    assert not local.verdicts_file.exists()


def test_a_second_verdict_for_a_row_needs_replace(remote, client):
    packet = publish(remote, rows=2)
    local, _ = client.pull()
    row = local.row_ids[0]
    client.record_verdict(packet.packet_id, row, "keep")
    with pytest.raises(ReviewError, match="already has a verdict"):
        client.record_verdict(packet.packet_id, row, "reject", ["person_visible"])
    client.record_verdict(packet.packet_id, row, "reject", ["person_visible"], replace=True)
    verdicts = [json.loads(line) for line in local.verdicts_file.read_text().strip().split("\n")]
    assert [(v["row_id"], v["verdict"]) for v in verdicts] == [(row, "reject")]


def test_check_reports_problems_duplicates_and_missing_rows(remote, client, tmp_path):
    packet = publish(remote, rows=3)
    local, _ = client.pull()
    a, b, c = local.row_ids
    hand = tmp_path / "hand.jsonl"
    hand.write_text(
        "\n".join(
            [
                json.dumps({"row_id": a, "verdict": "keep"}),
                json.dumps({"row_id": a, "verdict": "keep"}),
                json.dumps({"row_id": b, "verdict": "reject"}),
                json.dumps({"row_id": "stranger", "verdict": "keep"}),
                json.dumps({"row_id": c, "verdict": "keep", "row": {"thoughts": ["edited"]}}),
                '{"row_id": "',
            ]
        ),
        encoding="utf-8",
    )
    report = client.check(packet.packet_id, hand)
    assert report.judged == [a]
    assert report.duplicates == [a]
    assert set(report.missing) == {b, c}
    assert not report.ok and not report.complete
    text = " | ".join(report.problems)
    for expected in ("more than one verdict", "needs at least one reason", "not in the packet", "unexpected field 'row'", "not valid JSON"):
        assert expected in text
    assert client.check(packet.packet_id).problems == [f"no verdict file at {local.verdicts_file}"]


def test_push_refuses_a_packet_with_rows_left(remote, client):
    packet = publish(remote, rows=3)
    local, _ = client.pull()
    client.record_verdict(packet.packet_id, local.row_ids[0], "keep")
    result = client.push(packet.packet_id)
    assert result.pushed is False
    assert sum("has no verdict yet" in reason for reason in result.reasons) == 2
    assert remote.list("review/") == []
    assert len(list((local.folder / "images").iterdir())) == 3  # nothing burned


def test_push_uploads_the_verdicts_then_burns_the_local_pictures(remote, client):
    packet = publish(remote, rows=3)
    local, _ = client.pull()
    for index, row_id in enumerate(local.row_ids):
        client.record_verdict(packet.packet_id, row_id, "keep", notes=f"note {index}")
    result = client.push(packet.packet_id)
    assert result.pushed and result.burned == 3
    pushed = remote.read_text(layout.verdicts_path("rev-a", packet.packet_id)).strip().split("\n")
    assert [json.loads(line)["row_id"] for line in pushed] == local.row_ids
    assert not (local.folder / "images").exists()
    assert (local.folder / "packet.json").exists() and local.verdicts_file.exists()
    # The controller owns the shared copies of the pictures and removes them itself.
    assert remote.list(layout.packet_images_prefix("rev-a", packet.packet_id))


def test_pictures_stay_when_the_push_fails_and_are_burned_on_the_retry(remote, tmp_path):
    packet = publish(remote, rows=2)

    class Flaky:
        """A store that fails its first write, like a dropped connection."""

        def __init__(self, inner):
            self.inner, self.failures = inner, 1

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def write_bytes(self, path, data):
            if self.failures:
                self.failures -= 1
                raise ConnectionError("dropped")
            self.inner.write_bytes(path, data)

        def write_text(self, path, text):
            self.write_bytes(path, text.encode("utf-8"))

    client = ReviewerClient("rev-a", Flaky(remote), tmp_path / "desk", fsync=False)
    local, _ = client.pull()
    for row_id in local.row_ids:
        client.record_verdict(packet.packet_id, row_id, "keep")
    with pytest.raises(ConnectionError):
        client.push(packet.packet_id)
    assert len(list((local.folder / "images").iterdir())) == 2
    assert remote.list("review/") == []
    result = client.push(packet.packet_id)
    assert result.pushed and result.burned == 2
    assert len(remote.list("review/")) == 1


def test_a_crash_after_the_push_but_before_the_burn_is_repaired_by_pushing_again(remote, client):
    packet = publish(remote, rows=2)
    local, _ = client.pull()
    for row_id in local.row_ids:
        client.record_verdict(packet.packet_id, row_id, "keep")
    client.remote.write_text(layout.verdicts_path("rev-a", packet.packet_id), local.verdicts_file.read_text())
    assert (local.folder / "images").exists()  # the burn never ran
    result = client.push(packet.packet_id)
    assert result.pushed and result.burned == 2


def test_a_scripted_run_resumes_from_its_own_verdict_file(remote, tmp_path):
    packet = publish(remote, rows=5)
    asked = []

    def flaky(item: ReviewItem):
        asked.append(item.row_id)
        if len(asked) == 3:
            raise RuntimeError("the reviewer's machine went to sleep")
        return {"verdict": "keep"}

    first = ReviewerClient("rev-a", remote, tmp_path / "desk", fsync=False)
    local, _ = first.pull()
    with pytest.raises(RuntimeError):
        first.run_packet(packet.packet_id, flaky)
    assert len(first.judged_rows(local)) == 2
    assert remote.list("review/") == []

    asked.clear()
    second = ReviewerClient("rev-a", remote, tmp_path / "desk", fsync=False)
    result = second.run_packet(packet.packet_id, lambda item: (asked.append(item.row_id), {"verdict": "keep"})[1])
    assert result.pushed and result.judged == 3 and result.burned == 5
    assert asked == local.row_ids[2:]  # the first two rows were not asked about again
    assert len(remote.read_text(layout.verdicts_path("rev-a", packet.packet_id)).strip().split("\n")) == 5


def test_scripted_review_hands_the_function_the_row_task_and_picture(remote, client):
    packet = publish(remote, rows=1)
    seen = {}

    def inspect(item: ReviewItem):
        seen["row_id"], seen["task"], seen["brief"] = item.row_id, item.task, item.brief_version
        seen["picture"] = item.image_path.read_bytes() if item.image_path else None
        seen["has_writer"] = "writer" in item.row
        return {"verdict": "keep"}

    client.pull()
    client.run_packet(packet.packet_id, inspect)
    assert seen["task"]["spec"] == "a cat" and seen["brief"] == "1"
    assert seen["picture"] == b"picture-0"
    assert seen["has_writer"] is False


def test_a_wrong_answer_leaves_the_row_for_the_next_run_and_blocks_the_push(remote, client):
    packet = publish(remote, rows=2)
    client.pull()

    def sloppy(item: ReviewItem):
        if item.row_id.endswith("_0"):
            return {"verdict": "keep"}
        return {"verdict": "reject"}  # no reasons

    result = client.run_packet(packet.packet_id, sloppy)
    assert result.judged == 1 and len(result.errors) == 1 and not result.pushed
    assert "needs at least one reason" in result.errors[0]
    retry = client.run_packet(packet.packet_id, lambda item: {"verdict": "reject", "reasons": ["too_vague"]})
    assert retry.pushed and retry.judged == 1


def test_a_review_function_cannot_answer_for_another_row(remote, client):
    packet = publish(remote, rows=1)
    client.pull()
    result = client.run_packet(packet.packet_id, lambda item: {"row_id": "someone-else", "verdict": "keep"})
    assert not result.pushed and "answered for" in result.errors[0]


def test_a_command_line_reviewer_gets_the_item_on_stdin_and_prints_a_verdict(remote, client):
    packet = publish(remote, rows=2)
    client.pull()
    script = (
        "import json, sys; item = json.load(sys.stdin); "
        "print(json.dumps({'verdict': 'keep', 'notes': 'seen ' + item['row_id'][-1]}))"
    )
    result = client.run_packet(packet.packet_id, [sys.executable, "-c", script])
    assert result.pushed and result.judged == 2
    lines = remote.read_text(layout.verdicts_path("rev-a", packet.packet_id)).strip().split("\n")
    assert [json.loads(line)["notes"] for line in lines] == ["seen 0", "seen 1"]


def test_a_failing_command_is_an_error_on_that_row(remote, client):
    packet = publish(remote, rows=1)
    client.pull()
    result = client.run_packet(packet.packet_id, [sys.executable, "-c", "import sys; sys.exit(3)"])
    assert not result.pushed and "exited with 3" in result.errors[0]
    garbage = client.run_packet(packet.packet_id, [sys.executable, "-c", "print('not json')"])
    assert "did not print a verdict" in garbage.errors[0]
    with pytest.raises(ReviewError):
        CommandReviewer("")


def test_run_works_through_every_waiting_packet(remote, client):
    publish(remote, rows=2)
    publish(remote, rows=3)
    results = client.run(keep)
    assert [r.pushed for r in results] == [True, True]
    assert sorted(r.judged for r in results) == [2, 3]
    assert client.run(keep) == []


def test_the_reviewer_can_write_only_its_own_verdict_file(remote, client):
    with pytest.raises(OwnershipError):
        client.remote.write_text("review/rev-b/p0123456789ab.jsonl", "forged")
    with pytest.raises(OwnershipError):
        client.remote.write_text(layout.events_path("20261107T090000Z-aaaa"), "x")
    with pytest.raises(OwnershipError):
        client.remote.delete(layout.packet_json_path("rev-a", "p0123456789ab"))


def test_status_shows_rows_left_and_pictures_still_here(remote, client):
    packet = publish(remote, rows=3)
    local, _ = client.pull()
    client.record_verdict(packet.packet_id, local.row_ids[0], "keep")
    (status,) = client.status()
    assert (status.total, status.judged, status.left, status.pushed, status.images_left) == (3, 1, 2, False, 3)


# ------------------------------------------------------------------ the command line


@pytest.fixture
def cli_env(tmp_path):
    config_path = tmp_path / "config.json"
    config = {"work_dir": str(tmp_path / "work"), "routes": {}}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    state = LocalStore(Config.from_dict(config).work_path("state"), fsync=False)
    return config_path, state, tmp_path / "packets"


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = run(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_the_cli_walks_a_packet_from_pull_to_push(cli_env):
    config_path, state, into = cli_env
    packet = publish(state, rows=2)
    base = ["--config", str(config_path), "--into", str(into)]

    code, out, _ = cli("pull", "--reviewer", "rev-a", *base)
    assert code == EXIT_OK and packet.packet_id in out and "2 rows (0 judged, 2 left)" in out
    assert "verdict --packet" in out and "name the problem and where it is" in out

    code, out, _ = cli("status", "--reviewer", "rev-a", *base)
    assert code == EXIT_OK and "2 rows, 0 judged, 2 left, not pushed, 2 pictures here" in out

    first, second = [row.row_id for row in packet.rows]
    code, out, _ = cli("verdict", "--packet", packet.packet_id, "--row", first, "--verdict", "keep", "--into", str(into))
    assert code == EXIT_OK and "(1 of 2 judged)" in out

    code, out, err = cli("push", "--packet", packet.packet_id, *base)
    assert code == EXIT_REFUSED and second in err

    code, out, err = cli(
        "verdict", "--packet", packet.packet_id, "--row", second, "--verdict", "fix",
        "--reasons", "vague_thought, wrong_place", "--notes", "thought 3 names no place", "--into", str(into),
    )
    assert code == EXIT_OK, err

    code, out, err = cli("check", "--packet", packet.packet_id, "--into", str(into))
    assert code == EXIT_OK and "2 valid verdicts, 0 rows still without a verdict" in out

    code, out, err = cli("push", "--packet", packet.packet_id, *base)
    assert code == EXIT_OK and "burned 2 local pictures" in out, err
    lines = state.read_text(layout.verdicts_path("rev-a", packet.packet_id)).strip().split("\n")
    assert [json.loads(line)["verdict"] for line in lines] == ["keep", "fix"]
    assert json.loads(lines[1])["reasons"] == ["vague_thought", "wrong_place"]
    assert not (into / packet.packet_id / "images").exists()

    code, out, _ = cli("pull", "--reviewer", "rev-a", *base)
    assert code == EXIT_OK and "no packets are waiting for rev-a" in out


def test_the_cli_refuses_a_note_over_the_limit_and_edited_rows(cli_env):
    config_path, state, into = cli_env
    packet = publish(state, rows=1)
    cli("pull", "--reviewer", "rev-a", "--config", str(config_path), "--into", str(into))
    row = packet.rows[0].row_id
    code, _, err = cli("verdict", "--packet", packet.packet_id, "--row", row, "--verdict", "keep", "--notes", "x" * 2001, "--into", str(into))
    assert code == EXIT_INVALID and "at most 2000" in err
    code, _, err = cli("verdict", "--packet", packet.packet_id, "--row", "nope", "--verdict", "keep", "--into", str(into))
    assert code == EXIT_INVALID and "not in the packet" in err
    code, _, err = cli("verdict", "--packet", packet.packet_id, "--row", row, "--verdict", "reject", "--into", str(into))
    assert code == EXIT_INVALID and "needs at least one reason" in err


def test_the_cli_check_validates_a_hand_written_file(cli_env, tmp_path):
    config_path, state, into = cli_env
    packet = publish(state, rows=2)
    cli("pull", "--reviewer", "rev-a", "--config", str(config_path), "--into", str(into))
    good = tmp_path / "good.jsonl"
    good.write_text("".join(json.dumps({"row_id": row.row_id, "verdict": "keep"}) + "\n" for row in packet.rows), encoding="utf-8")
    code, out, _ = cli("check", "--packet", packet.packet_id, "--file", str(good), "--into", str(into))
    assert code == EXIT_OK and "2 valid verdicts" in out
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"row_id": packet.rows[0].row_id, "verdict": "keep", "row": {}}) + "\n", encoding="utf-8")
    code, out, err = cli("check", "--packet", packet.packet_id, "--file", str(bad), "--into", str(into))
    assert code == EXIT_INVALID and "unexpected field 'row'" in err
    assert "0 valid verdicts, 2 rows still without a verdict" in out


def test_the_cli_reports_a_missing_packet_and_a_bad_config(cli_env, tmp_path):
    _config_path, _state, into = cli_env
    code, _, err = cli("verdict", "--packet", "p0123456789ab", "--row", "r", "--verdict", "keep", "--into", str(into))
    assert code == EXIT_INVALID and "run pull first" in err
    code, _, err = cli("pull", "--reviewer", "rev-a", "--config", str(tmp_path / "missing.json"), "--into", str(into))
    assert code == EXIT_FAILED and "cannot read config" in err


def test_the_cli_push_checks_the_reviewer_named_on_the_command_line(cli_env):
    config_path, state, into = cli_env
    packet = publish(state, rows=1)
    cli("pull", "--reviewer", "rev-a", "--config", str(config_path), "--into", str(into))
    code, _, err = cli("push", "--packet", packet.packet_id, "--reviewer", "rev-b", "--config", str(config_path), "--into", str(into))
    assert code == EXIT_INVALID and "addressed to rev-a" in err
