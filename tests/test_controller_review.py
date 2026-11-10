"""The review protocol, end to end through the controller."""

import json

from rps import ids, layout
from rps.assess import Assessment
from rps.provenance import is_human_row, require_label
from rps.review.policy import audit_selected

from harness import World, keep_all, task_spec
from helpers import make_config

REVIEWERS = [
    {"id": "rev-a", "family": "alpha"},
    {"id": "rev-b", "family": "beta"},
    {"id": "rev-c", "family": "gamma"},
]


def review_config(*, reviewers=REVIEWERS, policy=None, packet_size=3, writer_family="writer-family", **extra):
    return make_config(
        review={
            "packet_size": packet_size,
            "reviewers": reviewers,
            "provider_families": {"fake": writer_family},
            "policy": {"audit_fraction": 0.0, **(policy or {})},
        },
        **extra,
    )


def by_family(mapping):
    """A reviewer function that answers by the reviewer's family."""

    def verdict(reviewer, item):
        word = mapping[reviewer.family]
        if word == "keep":
            return {"verdict": "keep"}
        return {"verdict": word, "reasons": ["needs_work"], "notes": f"{reviewer.family} found a problem in thought 1"}

    return verdict


def flag_everything(row, context):
    return Assessment(passed=True, flags=("needs_a_look",))


def rows_by_line(controller):
    return {row.line: row for row in controller.state.rows.values()}


def setup(tmp_path, config=None, lines=("001", "002", "003", "004", "005", "006"), assess=None, **world_kwargs):
    world = World(tmp_path, config=config or review_config(), **world_kwargs)
    controller = world.controller(assess=assess)
    controller.add_task(task_spec(lines))
    return world, controller


# ------------------------------------------------------------------ the first review


def test_first_reviews_rotate_across_all_reviewers(tmp_path):
    _world, controller = setup(tmp_path)
    controller.run()
    packets = controller.state.packets.values()
    assert sorted(p.reviewer_id for p in packets) == ["rev-a", "rev-b", "rev-c"]
    first = [(item["row_id"], p.reviewer_id) for p in packets for item in p.items]
    by_row = dict(first)
    rows = rows_by_line(controller)
    order = [by_row[rows[line].row_id] for line in ("001", "002", "003", "004", "005", "006")]
    assert order == ["rev-a", "rev-b", "rev-c", "rev-a", "rev-b", "rev-c"]


def test_the_first_review_never_goes_to_the_writers_family(tmp_path):
    world, controller = setup(tmp_path, config=review_config(writer_family="alpha"))
    world.drive(controller, keep_all)
    reviewers_used = {e.payload["reviewer_id"] for e in world.events_of("packet_published")}
    assert reviewers_used == {"rev-b", "rev-c"}
    assert len(world.shipped_rows()) == 6


def test_the_rotation_goes_on_across_a_restart(tmp_path):
    config = review_config(packet_size=1)
    world = World(tmp_path, config=config)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003"), batch="1"))
    first.run()
    second = world.controller()
    second.add_task(task_spec(("004", "005", "006"), batch="2"))
    second.run()
    order = [e.payload["reviewer_id"] for e in world.events_of("packet_published")]
    assert order == ["rev-a", "rev-b", "rev-c", "rev-a", "rev-b", "rev-c"]


# ------------------------------------------------------------------ settling rows


def test_a_keep_with_no_flag_accepts_the_row_at_once(tmp_path):
    world, controller = setup(tmp_path)
    world.drive(controller, keep_all)
    assert len(world.events_of("verdict_received")) == 6
    assert world.events_of("row_escalated") == []
    assert {e.payload["by"] for e in world.events_of("row_accepted")} == {"reviewers"}
    assert controller.state.counts()["rows"] == {"accepted": 6}


def test_a_reject_sends_the_row_to_a_second_family(tmp_path):
    world, controller = setup(tmp_path)
    verdict = by_family({"alpha": "reject", "beta": "keep", "gamma": "keep"})
    world.drive(controller, verdict)
    rows = rows_by_line(controller)
    first = rows["001"]  # the first row went to the alpha reviewer, who rejected it
    assert [v["reviewer_id"] for v in first.verdicts][0] == "rev-a"
    assert first.verdicts[0]["verdict"] == "reject"
    escalations = [e for e in world.events_of("row_escalated") if e.payload["row_id"] == first.row_id]
    assert len(escalations) == 2
    assert escalations[0].payload["families_used"] == ["alpha"]
    second_family = first.verdicts[1]["family"]
    assert second_family in ("beta", "gamma")
    assert escalations[1].payload["families_used"] == sorted(["alpha", second_family])
    # every verdict is kept with its reviewer id and family, each family used once
    assert all(v["reviewer_id"] and v["family"] for v in first.verdicts)
    assert len({v["family"] for v in first.verdicts}) == len(first.verdicts) == 3
    # the rows the other families saw first were accepted on the first keep
    assert len(rows["002"].verdicts) == 1 and rows["002"].status == "accepted"


def test_two_keeps_accept_a_row_that_was_in_question(tmp_path):
    world, controller = setup(tmp_path, assess=flag_everything)
    world.drive(controller, keep_all)
    for row in controller.state.rows.values():
        assert row.status == "accepted"
        assert [v["verdict"] for v in row.verdicts] == ["keep", "keep"]  # a flag needs two keeps
        assert len({v["family"] for v in row.verdicts}) == 2
    assert len(world.events_of("row_escalated")) == 6
    assert len(world.shipped_rows()) == 6


def test_a_reject_then_two_keeps_accepts_on_the_third_reviewer(tmp_path):
    world, controller = setup(tmp_path, lines=("001",))
    verdict = by_family({"alpha": "reject", "beta": "keep", "gamma": "keep"})
    world.drive(controller, verdict)
    (row,) = controller.state.rows.values()
    assert [v["verdict"] for v in row.verdicts] == ["reject", "keep", "keep"]
    assert row.status == "accepted"


def test_two_rejects_discard_the_row_without_asking_a_third_reviewer(tmp_path):
    world, controller = setup(tmp_path, lines=("001",))
    world.drive(controller, by_family({"alpha": "reject", "beta": "reject", "gamma": "reject"}))
    (row,) = controller.state.rows.values()
    assert [v["verdict"] for v in row.verdicts] == ["reject", "reject"]
    assert row.status == "rejected" and row.rejection["by"] == "reviewers" and row.rejection["retry"] is False
    assert len(world.events_of("packet_published")) == 2
    assert world.shipped_rows() == []
    assert controller.state.job_progress(2)["lines_rejected"] == 1
    assert world.provider.submit_count == 1  # a discarded row is not written again


def test_a_discard_records_the_reviewers_reasons(tmp_path):
    world, controller = setup(tmp_path, lines=("001",))
    world.drive(controller, by_family({"alpha": "reject", "beta": "reject", "gamma": "reject"}))
    (event,) = world.events_of("row_rejected")
    assert len(event.payload["errors"]) == 2 and all("needs_work" in text for text in event.payload["errors"])


def test_after_three_reviewers_without_a_settlement_the_row_goes_to_the_hand_queue(tmp_path):
    world, controller = setup(tmp_path, lines=("001", "002"), assess=flag_everything)
    summary = None
    verdict = by_family({"alpha": "fix", "beta": "keep", "gamma": "reject"})
    world.drive(controller, verdict)
    rows = list(controller.state.rows.values())
    assert all(row.status == "hand" for row in rows)
    assert all(len(row.verdicts) == 3 and len({v["family"] for v in row.verdicts}) == 3 for row in rows)
    assert len(world.events_of("row_escalated")) == 4  # two further reviewers for each of two rows
    to_hand = world.events_of("row_to_hand")
    assert len(to_hand) == 2 and all("3 reviewers without a settlement" in e.payload["reason"] for e in to_hand)
    assert world.shipped_rows() == []
    assert controller.is_idle()
    queue = [json.loads(line) for line in world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()]
    assert {entry["row_id"] for entry in queue} == {row.row_id for row in rows}
    entry = queue[0]
    assert {v["family"] for v in entry["verdicts"]} == {"alpha", "beta", "gamma"}
    assert all(v["reviewer_id"] and v["verdict"] for v in entry["verdicts"])
    assert entry["flags"] == ["needs_a_look"]
    assert entry["row"]["writer"]["provider"] == "fake"  # the owner sees who wrote it
    assert entry["task"]["spec"].startswith("spec for")
    summary = controller.run()
    assert summary.hand_queue == 2


def test_a_row_goes_to_hand_when_no_unused_family_is_left(tmp_path):
    config = review_config(reviewers=REVIEWERS[:2])
    world, controller = setup(tmp_path, config=config, lines=("001",), assess=flag_everything)
    world.drive(controller, by_family({"alpha": "reject", "beta": "keep"}))
    (row,) = controller.state.rows.values()
    assert row.status == "hand" and len(row.verdicts) == 2
    (event,) = world.events_of("row_to_hand")
    assert "no reviewer from an unused family" in event.payload["reason"]


def test_a_row_goes_to_hand_when_every_reviewer_shares_the_writers_family(tmp_path):
    config = review_config(reviewers=[{"id": "same-1", "family": "alpha"}], writer_family="alpha")
    world, controller = setup(tmp_path, config=config, lines=("001",))
    controller.run()
    (row,) = controller.state.rows.values()
    assert row.status == "hand"
    (event,) = world.events_of("row_to_hand")
    assert "another family than the writer" in event.payload["reason"]
    assert controller.state.packets == {}


def test_rows_wait_when_no_reviewer_is_configured(tmp_path):
    _world, controller = setup(tmp_path, config=review_config(reviewers=[]), lines=("001", "002"))
    summary = controller.run()
    assert summary.reason == "idle"
    assert controller.state.counts()["rows"] == {"passed": 2}
    assert controller.state.packets == {}


# ------------------------------------------------------------------ the owner's hand queue


def write_hand_decisions(world, decisions, name="decisions.jsonl"):
    from rps.layout import operator_actor
    from rps.store import ActorStore

    text = "".join(json.dumps(item) + "\n" for item in decisions)
    ActorStore(world.state_store, operator_actor()).write_text(layout.CONTROL_HAND_PREFIX + name, text)


def hand_queue_world(tmp_path, lines=("001", "002", "003")):
    world, controller = setup(tmp_path, lines=lines, assess=flag_everything)
    world.drive(controller, by_family({"alpha": "fix", "beta": "keep", "gamma": "reject"}))
    assert len(controller.state.hand_queue()) == len(lines)
    return world, controller


def test_the_owner_can_accept_discard_or_rewrite_a_row_by_hand(tmp_path):
    world, controller = hand_queue_world(tmp_path)
    rows = rows_by_line(controller)
    write_hand_decisions(
        world,
        [
            {"row_id": rows["001"].row_id, "decision": "accept"},
            {"row_id": rows["002"].row_id, "decision": "discard"},
            {"row_id": rows["003"].row_id, "decision": "replace", "row": {"id": "003-by-hand", "thoughts": ["the owner's own thought"]}},
        ],
    )
    controller.step()
    controller.step()
    assert controller.state.hand_queue() == []
    assert rows["001"].status == "accepted" and rows["001"].accepted_by == "hand"
    assert rows["002"].status == "rejected" and rows["002"].rejection["by"] == "hand"
    assert rows["003"].status == "rejected" and rows["003"].replaced_by == rows["003"].row_id + "-h1"
    shipped = {row["row_id"]: row for row in world.shipped_rows()}
    assert set(shipped) == {rows["001"].row_id, rows["003"].row_id + "-h1"}
    assert world.state_store.read_text(layout.HAND_QUEUE_PATH) == ""
    assert controller.state.job_progress(2)["lines_accepted"] == 2


def test_a_human_written_row_carries_the_human_label(tmp_path):
    world, controller = hand_queue_world(tmp_path, lines=("001",))
    (row,) = controller.state.rows.values()
    write_hand_decisions(
        world, [{"row_id": row.row_id, "decision": "replace", "row": {"id": "001-by-hand", "thoughts": ["written by hand"], "writer": {"provider": "claude"}}}]
    )
    controller.step()
    controller.step()
    (shipped,) = world.shipped_rows()
    assert shipped["writer"]["provider"] == "human"
    assert shipped["writer"]["model"] == "phil"
    assert shipped["writer"]["route"] == "hand"
    assert shipped["writer"]["image_size"] is None
    assert "package_version" not in shipped["writer"]  # a person wrote it, not a request this package built
    assert shipped["thoughts"] == ["written by hand"]
    assert is_human_row(shipped)
    require_label(shipped)
    assert shipped["source_label"] == row.record["source_label"]  # where the subject came from is kept
    assert shipped["row_id"] == row.row_id + "-h1"


def test_bad_hand_decisions_are_reported_once_and_ignored(tmp_path):
    world, controller = hand_queue_world(tmp_path, lines=("001",))
    (row,) = controller.state.rows.values()
    write_hand_decisions(
        world,
        [
            {"row_id": row.row_id, "decision": "maybe"},
            {"row_id": row.row_id, "decision": "replace"},
            {"row_id": 5, "decision": "accept"},
            {"row_id": "not-in-the-queue", "decision": "accept"},
        ],
    )
    for _ in range(3):
        controller.step()
    assert row.status == "hand"
    text = " | ".join(controller.problems)
    assert text.count("decision must be one of") == 1 and text.count("needs a non-empty 'row'") == 1


# ------------------------------------------------------------------ the audit


def audit_config(**policy):
    return review_config(policy={"audit_fraction": 1.0, **policy})


def test_an_audit_sends_an_accepted_row_to_another_family_and_changes_nothing_on_keep(tmp_path):
    world, controller = setup(tmp_path, config=audit_config(), lines=("001", "002", "003"))
    world.drive(controller, keep_all)
    for row in controller.state.rows.values():
        assert row.status == "accepted"
        assert len(row.verdicts) == 2
        first, audit = row.verdicts
        assert audit["audit"] is True and first["audit"] is False
        assert audit["family"] != first["family"]
    assert len(world.shipped_rows()) == 3
    assert world.events_of("row_escalated") == []


def test_an_audit_that_rejects_puts_the_row_back_in_question(tmp_path):
    world, controller = setup(tmp_path, config=audit_config(), lines=("001",))

    def verdict(reviewer, item):
        # the audit reviewer is the second to see the row; the third decides
        seen = verdict.calls.setdefault(item.row_id, [])
        seen.append(reviewer.family)
        if len(seen) == 2:
            return {"verdict": "reject", "reasons": ["person_visible"], "notes": "a person stands in the doorway"}
        return {"verdict": "keep"}

    verdict.calls = {}
    world.drive(controller, verdict)
    (row,) = controller.state.rows.values()
    assert [(v["verdict"], v["audit"]) for v in row.verdicts] == [("keep", False), ("reject", True), ("keep", False)]
    assert row.status == "accepted" and row.reopened is True
    (event,) = world.events_of("row_escalated")
    assert event.payload["reason"] == "audit_reject"
    assert len({v["family"] for v in row.verdicts}) == 3
    assert len(world.shipped_rows()) == 1


def test_an_audit_reject_followed_by_a_reject_discards_a_row_that_was_already_shipped(tmp_path):
    world, controller = setup(tmp_path, config=audit_config(), lines=("001", "002"))

    def verdict(reviewer, item):
        seen = verdict.calls.setdefault(item.row_id, [])
        seen.append(reviewer.family)
        if item.row_id.startswith(verdict.bad_prefix) and len(seen) >= 2:
            return {"verdict": "reject", "reasons": ["person_visible"], "notes": "a person in the background"}
        return {"verdict": "keep"}

    verdict.calls = {}
    task_id = next(iter(controller.state.tasks))
    verdict.bad_prefix = ids.row_id(ids.call_id(task_id, "001", 1), 0)
    world.drive(controller, verdict)
    rows = rows_by_line(controller)
    assert rows["001"].status == "rejected" and rows["002"].status == "accepted"
    shipped = world.shipped_rows()
    assert [row["row_id"] for row in shipped] == [rows["002"].row_id]
    ships = world.events_of("shipped")
    assert len(ships) >= 2 and rows["001"].row_id in ships[0].payload["row_ids"]
    assert rows["001"].row_id not in ships[-1].payload["row_ids"]  # withdrawn from the shipped file


def test_the_audit_sample_is_the_reproducible_hash_sample(tmp_path):
    config = review_config(policy={"audit_fraction": 0.25, "audit_seed": "fixed-seed"}, packet_size=5)
    lines = tuple(f"{n:03d}" for n in range(1, 41))
    world, controller = setup(tmp_path, config=config, lines=lines)
    world.drive(controller, keep_all)
    audited = {item["row_id"] for p in controller.state.packets.values() for item in p.items if item["audit"]}
    expected = {row_id for row_id in controller.state.rows if audit_selected(row_id, 0.25, "fixed-seed")}
    assert audited == expected and 0 < len(audited) < 40
    # a restart does not audit anything twice
    again = world.controller()
    world.drive(again, keep_all)
    audited_again = [item["row_id"] for p in again.state.packets.values() for item in p.items if item["audit"]]
    assert len(audited_again) == len(set(audited_again)) == len(audited)


# ------------------------------------------------------------------ what reviewers see and keep


def test_packets_never_show_the_writer(tmp_path):
    forged_source = {"kind": "seeded", "model": "model-plugh-9", "seed_dataset": "seeds", "seed_id": "s-1", "license": "MIT"}

    def behavior(payload):
        from rps.providers import FakeOutcome

        row = {"id": payload["line"], "thoughts": ["a thought"], "source": dict(forged_source)}
        return FakeOutcome(output=json.dumps(row))

    config = make_config(
        routes={
            "fake-batch": {
                "provider": "provider-xyzzy", "model": "model-plugh-9", "kind": "batch", "budget_usd": 10,
                "purpose_caps": {"production": 10},
            }
        },
        review={"packet_size": 3, "reviewers": REVIEWERS, "policy": {"audit_fraction": 0.0}},
    )
    from rps.ledger import PriceTable

    prices = PriceTable.from_dict({"date": "2026-11-06", "models": {"model-plugh-9": {"input": 1.0, "output": 2.0}}})
    world = World(tmp_path, config=config, behavior=behavior, prices=prices)
    controller = world.controller()
    # The made-up provider is not in the routing table, so this task carries no picture.
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller, keep_all)
    paths = [p for p in world.state_store.list("packets/") if p.endswith("packet.json")]
    assert len(paths) == 3
    for path in paths:
        text = world.state_store.read_text(path)
        assert "provider-xyzzy" not in text and "model-plugh-9" not in text and '"writer"' not in text
        document = json.loads(text)
        entry = document["rows"][0]
        assert entry["row"]["source"]["model"] == "" and entry["row"]["source"]["seed_id"] == "s-1"
        assert entry["task"]["spec"].startswith("spec for") and entry["task"]["item_id"].startswith("img-")
    # the controller still knows who wrote every row
    assert {row["writer"]["provider"] for row in world.shipped_rows()} == {"provider-xyzzy"}
    assert {row["writer"]["model"] for row in world.shipped_rows()} == {"model-plugh-9"}


def briefed_config(brief_version):
    return make_config(
        review={
            "packet_size": 3,
            "reviewers": REVIEWERS,
            "brief_version": brief_version,
            "provider_families": {"fake": "writer-family"},
            "policy": {"audit_fraction": 0.0},
        }
    )


def packet_versions(world):
    paths = [p for p in world.state_store.list("packets/") if p.endswith("packet.json")]
    assert paths
    return {json.loads(world.state_store.read_text(p))["brief_version"] for p in paths}


def test_without_rules_a_packet_carries_the_configured_brief_version(tmp_path):
    world = World(tmp_path, config=briefed_config("7"))
    controller = world.controller()
    assert controller.rules is None
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller, keep_all)
    assert packet_versions(world) == {"7"}
    assert {e.payload["brief_version"] for e in world.events_of("packet_published")} == {"7"}


def test_loaded_rules_win_over_the_configured_brief_version(tmp_path):
    from rps.prompting import make_rules

    world = World(tmp_path, config=briefed_config("7"))
    controller = world.controller(rules=make_rules("Answer in json.", {"type": "object"}, "rules-9"))
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller, keep_all)
    assert packet_versions(world) == {"rules-9"}
    assert {e.payload["brief_version"] for e in world.events_of("packet_published")} == {"rules-9"}


PICTURE_REVIEWERS = [
    {"id": "rev-o", "family": "openai"},
    {"id": "rev-g", "family": "google"},
    {"id": "rev-d", "family": "deepseek"},
]


def test_pictures_are_burned_in_the_shared_store_after_the_verdicts_are_read(tmp_path):
    world = World(tmp_path, config=review_config(reviewers=PICTURE_REVIEWERS))
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003"), image="p.jpg"))
    controller.step()
    controller.step()
    store_images = lambda: [p for p in world.state_store.list("packets/") if "/images/" in p]  # noqa: E731
    assert len(store_images()) == 3
    world.run_reviewers()
    # the reviewers burned their own copies; the shared copies belong to the controller
    assert len(store_images()) == 3
    assert not list((world.tmp_path / "reviewers").rglob("*.jpg"))
    controller.step()
    assert store_images() == []
    burned = world.events_of("packet_burned")
    assert sum(e.payload["deleted"] for e in burned) == 3
    assert {row_id for e in burned for row_id in e.payload["row_ids"]} == set(controller.state.rows)


def test_a_packet_carries_a_picture_only_to_a_family_the_routing_table_allows(tmp_path):
    # The pictures of a public dataset go to Claude and OpenAI reviewers; a family the table does not know gets none.
    reviewers = [
        {"id": "rev-claude", "family": "anthropic"},
        {"id": "rev-o", "family": "openai"},
        {"id": "rev-x", "family": "alpha"},
    ]
    world = World(tmp_path, config=review_config(reviewers=reviewers, packet_size=1))
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003", "004", "005", "006"), image="p.jpg"))
    controller.step()
    controller.step()
    published = world.events_of("packet_published")
    assert {e.payload["reviewer_id"] for e in published} == {"rev-claude", "rev-o", "rev-x"}
    for event in published:
        packet_id, reviewer_id = event.payload["packet_id"], event.payload["reviewer_id"]
        document = json.loads(world.state_store.read_text(layout.packet_json_path(reviewer_id, packet_id)))
        images = [p for p in world.state_store.list(f"{layout.packet_dir(reviewer_id, packet_id)}/") if "/images/" in p]
        (entry,) = document["rows"]
        if reviewer_id in ("rev-o", "rev-claude"):
            assert entry["image"] and not entry["image_withheld"] and len(images) == 1, reviewer_id
        else:
            assert entry["image"] is None and entry["image_withheld"] and images == []


def test_each_verdict_is_matched_back_by_row_id_and_keeps_reviewer_and_family(tmp_path):
    world, controller = setup(tmp_path)
    world.drive(controller, keep_all)
    for event in world.events_of("verdict_received"):
        assert event.payload["reviewer_id"] in {"rev-a", "rev-b", "rev-c"}
        assert event.payload["family"] == {"rev-a": "alpha", "rev-b": "beta", "rev-c": "gamma"}[event.payload["reviewer_id"]]
        assert event.payload["row_id"] in controller.state.rows


def test_invalid_verdict_lines_are_reported_once_and_do_not_block_valid_ones(tmp_path):
    world, controller = setup(tmp_path, lines=("001",))
    controller.step()
    controller.step()
    (packet,) = controller.state.packets.values()
    row_id = packet.items[0]["row_id"]
    path = layout.verdicts_path(packet.reviewer_id, packet.packet_id)
    lines = [
        json.dumps({"row_id": row_id, "verdict": "reject", "reasons": []}),  # invalid: no reason
        json.dumps({"row_id": row_id, "verdict": "keep", "row": {"thoughts": ["edited"]}}),  # invalid: an edited row
        "not json at all",
        json.dumps({"row_id": "someone-else", "verdict": "keep"}),  # not in the packet
    ]
    world.state_store.write_text(path, "".join(line + "\n" for line in lines))
    for _ in range(3):
        controller.step()
    assert controller.state.rows[row_id].verdicts == []
    assert len(controller.problems) == 4
    world.state_store.write_text(path, "".join(line + "\n" for line in lines) + json.dumps({"row_id": row_id, "verdict": "keep"}) + "\n")
    controller.step()
    assert controller.state.rows[row_id].status == "accepted"


def test_only_the_assigned_reviewers_file_is_read(tmp_path):
    world, controller = setup(tmp_path, lines=("001",))
    controller.step()
    controller.step()
    (packet,) = controller.state.packets.values()
    other = "rev-b" if packet.reviewer_id != "rev-b" else "rev-c"
    # another reviewer writes a verdict file under its own name for the same packet id
    world.state_store.write_text(
        layout.verdicts_path(other, packet.packet_id), json.dumps({"row_id": packet.items[0]["row_id"], "verdict": "keep"}) + "\n"
    )
    controller.step()
    assert controller.state.rows[packet.items[0]["row_id"]].verdicts == []


# ------------------------------------------------------------------ the owner sends a row back to be reviewed again


def fix_to_hand_world(tmp_path):
    """One flagged row that its first reviewer (rev-a) sent to the owner with a fix; the other families are unused."""
    world, controller = setup(tmp_path, config=review_config(policy={"fix_to_hand": True}), lines=("001",), assess=flag_everything)
    world.drive(controller, by_family({"alpha": "fix", "beta": "keep", "gamma": "keep"}))
    assert [row.status for row in controller.state.rows.values()] == ["hand"]
    return world, controller


def test_a_review_decision_needs_reviewers_with_a_verdict_on_the_row(tmp_path):
    world, controller = fix_to_hand_world(tmp_path)
    (row,) = controller.state.rows.values()
    write_hand_decisions(
        world,
        [
            {"row_id": row.row_id, "decision": "review"},
            {"row_id": row.row_id, "decision": "review", "set_aside": []},
            {"row_id": row.row_id, "decision": "review", "set_aside": "rev-a"},
            {"row_id": row.row_id, "decision": "review", "set_aside": ["rev-c"]},  # rev-c gave no verdict on the row
            {"row_id": row.row_id, "decision": "review", "set_aside": ["rev-a", "rev-c"]},
        ],
    )
    for _ in range(3):
        controller.step()
    assert row.status == "hand" and world.events_of("hand_resolved") == []
    text = " | ".join(controller.problems)
    assert text.count("a review needs 'set_aside'") == 1 and text.count("no verdict of its own reviews") == 1  # each said once
    assert row.row_id in text and "decisions.jsonl" in text


def test_the_owner_sets_a_reviewers_verdict_aside_and_a_reviewer_of_another_family_looks_again(tmp_path):
    world, controller = fix_to_hand_world(tmp_path)
    (row,) = controller.state.rows.values()
    assert [v["reviewer_id"] for v in row.verdicts] == ["rev-a"] and row.verdicts[0]["verdict"] == "fix"
    write_hand_decisions(world, [{"row_id": row.row_id, "decision": "review", "set_aside": ["rev-a"]}])
    controller.step()
    controller.step()
    (event,) = world.events_of("hand_resolved")
    assert event.payload["decision"] == "review" and event.payload["set_aside"] == ["rev-a"]
    assert row.verdicts[0].get("set_aside") is True and controller.state.hand_queue() == []
    further = [p for p in controller.state.packets.values() if p.reviewer_id != "rev-a"]
    assert len(further) == 1 and further[0].family in ("beta", "gamma")
    assert [item["row_id"] for item in further[0].items] == [row.row_id] and further[0].items[0]["round"] == 2
    assert world.state_store.read_text(layout.HAND_QUEUE_PATH) == ""
    # the line stays where it is: one event, however many passes follow
    for _ in range(3):
        controller.step()
    assert len(world.events_of("hand_resolved")) == 1


def test_a_review_line_that_was_carried_out_is_silent_when_the_row_comes_back_to_the_owner(tmp_path):
    world, controller = fix_to_hand_world(tmp_path)
    (row,) = controller.state.rows.values()
    write_hand_decisions(world, [{"row_id": row.row_id, "decision": "review", "set_aside": ["rev-a"]}])
    world.drive(controller, by_family({"alpha": "fix", "beta": "fix", "gamma": "fix"}))  # the next reviewer sends it back too
    assert row.status == "hand" and [v.get("set_aside") for v in row.verdicts] == [True, None]
    for _ in range(3):
        controller.step()
    assert len(world.events_of("hand_resolved")) == 1  # the old line does not send the row round again
    assert not any(row.row_id in problem for problem in controller.problems)  # and it is not a mistake to report


# ------------------------------------------------------------------ no reviewer reads the same row twice


def test_no_reviewer_is_sent_the_same_row_twice_when_its_family_was_renamed(tmp_path):
    alone = [{"id": "rev-a", "family": "alpha"}]
    world, controller = setup(tmp_path, config=review_config(reviewers=alone), assess=flag_everything)
    world.drive(controller, keep_all)  # a flagged row needs two keeps, and there is no second family
    assert {row.status for row in controller.state.rows.values()} == {"hand"}
    first_packets = len(world.events_of("packet_published"))
    # the same reviewer under a new family name is not a second reader
    renamed = {"id": "rev-a", "family": "renamed"}
    world.config = review_config(reviewers=[renamed])
    again = world.controller(assess=flag_everything)
    world.drive(again, keep_all)
    assert {row.status for row in again.state.rows.values()} == {"hand"}
    assert len(world.events_of("packet_published")) == first_packets and world.events_of("row_reopened") == []
    # with a reviewer of another family the rows go to that one, never back to the first
    world.config = review_config(reviewers=[renamed, {"id": "rev-b", "family": "beta"}])
    last = world.controller(assess=flag_everything)
    world.drive(last, keep_all)
    for row in last.state.rows.values():
        assert row.status == "accepted" and [v["reviewer_id"] for v in row.verdicts] == ["rev-a", "rev-b"]
