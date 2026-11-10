"""A reviewer who may correct the light with a fix verdict (`rps.amend`), the repair that follows, and the rule texts
about a person who is not the picture's subject."""

import json
from pathlib import Path

import pytest

from rps import ids, layout
from rps.amend import NONE_VALUE, amend_problems, amend_values, amended_item
from rps.assess import Assessment
from rps.assessor import validator
from rps.config import Config, ConfigError, ReviewerConfig
from rps.layout import reviewer_actor
from rps.prompting import repair_text
from rps.repair import (
    LIGHT_DIFFER,
    LIGHT_GIVEN,
    LIGHT_NONE,
    correction_outcomes,
    light_amendment,
    light_lines,
    repair_feedback,
    repair_item,
)
from rps.review.cli import EXIT_INVALID, EXIT_OK, run
from rps.review.client import ReviewerClient
from rps.review.packets import VERDICT_SCHEMA, build_packet, normalize_verdict, packet_document, publish_packet, validate_verdict
from rps.state import RowState, State
from rps.store import ActorStore, LocalStore
from rps.tasks import SpecLine, TaskSpec

from harness import World
from helpers import Builder
from repair_helpers import repair_config
from review_helpers import review_config

LIGHT = "shot light"
VALUES = amend_values()[LIGHT]
CAN_AMEND = {"id": "rev-b", "family": "beta", "may_amend": [LIGHT]}
TASK = ids.task_id("photos", "1", {"001": "x"})
CALL = ids.call_id(TASK, "001", 1)
LINE = {"line_id": "001", "item_id": "img-001", "spec": "a cat"}


# ------------------------------------------------------------------ the configuration


def test_may_amend_lists_shot_light_and_a_configuration_without_it_is_unchanged():
    reviewer = ReviewerConfig.from_dict({"id": "a", "family": "x", "may_amend": [LIGHT]}, "r")
    assert reviewer.may_amend == (LIGHT,) and reviewer.to_dict()["may_amend"] == [LIGHT]
    assert ReviewerConfig.from_dict(reviewer.to_dict(), "r") == reviewer
    plain = ReviewerConfig.from_dict({"id": "a", "family": "x"}, "r")
    assert plain.may_amend == () and "may_amend" not in plain.to_dict()
    assert Config.from_dict(review_config([{"id": "a", "family": "x"}]).to_dict()).review.amendable("a") == ()


@pytest.mark.parametrize("bad", [["shot angle"], ["shot light", "thing colour"], "shot light", [5]])
def test_any_other_key_in_may_amend_is_a_configuration_error(bad):
    with pytest.raises(ConfigError, match="may_amend"):
        ReviewerConfig.from_dict({"id": "a", "family": "x", "may_amend": bad}, "review.reviewers[0]")
    with pytest.raises(ConfigError, match="only 'shot light' may be listed"):
        ReviewerConfig.from_dict({"id": "a", "family": "x", "may_amend": ["shot angle"]}, "r")


def test_the_values_are_the_rules_list_and_none():
    assert VALUES[-1] == NONE_VALUE and "soft window" in VALUES and len(VALUES) > 10
    assert VALUES[:-1] == [v for v in VALUES if v != NONE_VALUE]


# ------------------------------------------------------------------ packets


def rows(n=2):
    b = Builder()
    return [(b.row(CALL, i), LINE, b"pic", "jpg") for i in range(n)]


def packet(**kw):
    return build_packet(packet_id="p1", reviewer_id="rev-b", brief_version="1", created="2026-11-07T09:00:00.000000Z", rows=rows(), **kw)


def test_a_packet_for_a_reviewer_who_may_amend_carries_the_permission_and_any_other_is_unchanged():
    doc = packet_document(packet(may_amend=(LIGHT,), amend_values=amend_values()))
    assert doc["may_amend"] == [LIGHT] and doc["amend_values"] == {LIGHT: VALUES}
    assert doc["verdict_schema"]["properties"]["amend"]["properties"][LIGHT]["enum"] == VALUES
    plain = packet_document(packet())
    assert list(plain) == ["format", "packet_id", "reviewer_id", "brief_version", "created", "verdict_schema", "rows"]
    assert plain["verdict_schema"] == VERDICT_SCHEMA and "amend" not in VERDICT_SCHEMA["properties"]
    assert json.dumps(plain) == json.dumps(packet_document(packet(may_amend=(), amend_values=None)))


# ------------------------------------------------------------------ verdicts and the client


def verdict(**extra):
    return {"row_id": "r1", "verdict": "fix", "reasons": ["contradicts_picture"], "notes": "the light", **extra}


def test_an_amend_is_valid_only_with_fix_a_listed_key_and_a_listed_value():
    ok = verdict(amend={LIGHT: "soft window"})
    assert validate_verdict(ok, may_amend=(LIGHT,), amend_values=amend_values()) == []
    assert validate_verdict(verdict(amend={LIGHT: NONE_VALUE}), may_amend=(LIGHT,), amend_values=amend_values()) == []
    assert normalize_verdict(ok, may_amend=(LIGHT,), amend_values=amend_values())["amend"] == {LIGHT: "soft window"}
    assert "amend" not in normalize_verdict(verdict(), may_amend=(LIGHT,), amend_values=amend_values())
    assert validate_verdict(ok)  # a reviewer who may not: refused
    assert validate_verdict(ok, may_amend=(), amend_values=None)
    keep = {"row_id": "r1", "verdict": "keep", "amend": {LIGHT: "neon"}}
    assert any("only with the verdict fix" in p for p in validate_verdict(keep, may_amend=(LIGHT,), amend_values=amend_values()))
    for bad in ({LIGHT: "plaid"}, {"shot angle": "neon"}, {LIGHT: 5}, {}, "neon"):
        assert validate_verdict(verdict(amend=bad), may_amend=(LIGHT,), amend_values=amend_values())
    assert amend_problems("fix", {LIGHT: "neon"}, (LIGHT,), amend_values()) == []


def publish(remote, reviewer="rev-b", **kw):
    b = Builder()
    stored = [b.row(CALL, i) for i in range(2)]
    made = build_packet(
        packet_id=ids.packet_id([f"{reviewer}:{r['row_id']}" for r in stored]),
        reviewer_id=reviewer,
        brief_version="1",
        created="2026-11-07T09:00:00.000000Z",
        rows=[(row, LINE, b"pic", "jpg") for row in stored],
        **kw,
    )
    publish_packet(ActorStore(remote, layout.controller_actor("20261107T090000Z-aaaa")), made)
    return made


def desk(tmp_path, reviewer, **kw):
    remote = LocalStore(tmp_path / "remote", fsync=False)
    made = publish(remote, reviewer, **kw)
    client = ReviewerClient(reviewer, remote, tmp_path / "desk", fsync=False)
    local, _ = client.pull()
    return made, client, local, remote


def test_the_client_records_an_amend_only_where_the_packet_allows_it(tmp_path):
    made, client, local, _ = desk(tmp_path, "rev-b", may_amend=(LIGHT,), amend_values=amend_values())
    first, second = local.row_ids
    kept = client.record_verdict(made.packet_id, first, "fix", ["contradicts_picture"], "light", amend={LIGHT: "neon"})
    assert kept["amend"] == {LIGHT: "neon"} and client.judged_rows(local)[first]["amend"] == {LIGHT: "neon"}
    from rps.review.client import ReviewError

    for verdict_word, value in (("keep", "neon"), ("fix", "plaid")):
        with pytest.raises(ReviewError):
            client.record_verdict(made.packet_id, second, verdict_word, ["x"] if verdict_word == "fix" else [], "", amend={LIGHT: value})
    client.record_verdict(made.packet_id, second, "fix", ["x"], "no light", amend={LIGHT: NONE_VALUE})
    assert client.check(made.packet_id).complete and client.push(made.packet_id).pushed


def test_the_client_refuses_an_amend_for_a_reviewer_whose_packet_does_not_allow_it_and_check_and_push_agree(tmp_path):
    from rps.review.client import ReviewError

    made, client, local, _ = desk(tmp_path, "rev-a")
    first, second = local.row_ids
    with pytest.raises(ReviewError, match="may not correct"):
        client.record_verdict(made.packet_id, first, "fix", ["x"], "", amend={LIGHT: "neon"})
    # a hand-written file is refused at check and at push
    lines = [{"row_id": row_id, "verdict": "fix", "reasons": ["x"], "notes": "", "amend": {LIGHT: "neon"}} for row_id in local.row_ids]
    local.verdicts_file.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    report = client.check(made.packet_id)
    assert not report.ok and any("amend" in problem for problem in report.problems)
    result = client.push(made.packet_id)
    assert not result.pushed and result.reasons


def test_the_command_line_takes_amend(tmp_path):
    made, client, local, _ = desk(tmp_path, "rev-b", may_amend=(LIGHT,), amend_values=amend_values())
    out, err = [], []

    class Sink:
        def __init__(self, into):
            self.into = into

        def write(self, text):
            self.into.append(text)

    def call(row, amend):
        return run(
            ["verdict", "--packet", made.packet_id, "--row", row, "--verdict", "fix", "--reasons", "contradicts_picture",
             "--notes", "n", "--amend", amend, "--into", str(tmp_path / "desk")],
            Sink(out), Sink(err),
        )

    assert call(local.row_ids[0], "shot light=soft window") == EXIT_OK
    assert call(local.row_ids[1], "shot light=none") == EXIT_OK
    judged = client.judged_rows(client.local_packet(made.packet_id))
    assert [judged[r]["amend"][LIGHT] for r in local.row_ids] == ["soft window", "none"]
    assert call(local.row_ids[0], "shot light") == EXIT_INVALID
    assert call(local.row_ids[0], "shot light=plaid") == EXIT_INVALID


# ------------------------------------------------------------------ the item and the repair text


ITEM = {"kind": "single", "series": "s-1", "ids": ["x"], "depth": "simple", "spec": {"shot": {"light": "neon"}, "thing": {"name": "lamp"}}}
SERIES = {
    "kind": "series",
    "series": "s-2",
    "ids": ["a", "b", "c"],
    "seed_id": "seed-1",
    "specs": {"deep": {"shot": {"light": "neon"}, "thing": {"name": "lamp"}}},
}


def fix(reviewer, value=None, notes="the light is wrong"):
    out = {"reviewer_id": reviewer, "verdict": "fix", "reasons": ["contradicts_picture"], "notes": notes, "audit": False}
    if value is not None:
        out["amend"] = {LIGHT: value}
    return out


def test_the_amended_item_holds_the_record_as_given_or_without_it():
    assert amended_item(ITEM, LIGHT, "soft window")["spec"]["shot"] == {"light": "soft window"}
    assert ITEM["spec"]["shot"] == {"light": "neon"}  # the original is not touched
    gone = amended_item(ITEM, LIGHT, NONE_VALUE)
    assert "shot" not in gone["spec"] and gone["spec"]["thing"] == {"name": "lamp"} and gone["amend"] == {LIGHT: NONE_VALUE}
    assert amended_item(SERIES, LIGHT, "candle")["specs"]["deep"]["shot"]["light"] == "candle"
    unlit = {"kind": "single", "ids": ["x"], "spec": {"thing": {"name": "lamp"}}}
    assert amended_item(unlit, LIGHT, "candle")["spec"]["shot"] == {"light": "candle"}
    assert amended_item("a text spec", LIGHT, "candle") == "a text spec"


def test_one_value_is_set_two_differing_values_set_nothing():
    one = light_amendment([fix("rev-a", "soft window"), fix("rev-b", "soft window")])
    assert one["value"] == "soft window" and one["values"] == ["soft window"]
    assert [r["reviewer_id"] for r in one["reviewers"]] == ["rev-a", "rev-b"]
    two = light_amendment([fix("rev-a", "soft window"), fix("rev-b", "neon")])
    assert two["value"] is None and two["values"] == ["soft window", "neon"]
    assert light_amendment([fix("rev-a")]) is None
    assert light_lines(one) == [LIGHT_GIVEN.format(value="soft window")]
    assert light_lines(light_amendment([fix("rev-a", NONE_VALUE)])) == [LIGHT_NONE]
    assert light_lines(two) == [LIGHT_DIFFER.format(values='"soft window", "neon"')]
    assert light_lines(None) == []


def row_state(verdicts, record=None):
    row = RowState(row_id="r", call_id="c", task_id="t", line="001", route="x", record=record or {"id": "r", "thoughts": ["t"]})
    row.verdicts = verdicts
    return row


def test_the_repair_text_gives_the_light_line_in_words_and_the_notes_stay_apart():
    row = row_state([fix("rev-a", "soft window", notes="thought 2 is wrong")])
    feedback = repair_feedback(row)
    assert feedback[0] == "thought 2 is wrong" and feedback[1] == LIGHT_GIVEN.format(value="soft window")
    text = repair_text(feedback)
    assert (
        "A reviewer who saw the picture gives the light as: soft window. Keep this record exactly and make every sentence agree with it."
        in text
    )
    assert "- thought 2 is wrong" in text and "- A reviewer who saw" not in text
    none = repair_text(repair_feedback(row_state([fix("rev-a", NONE_VALUE)])))
    assert (
        "A reviewer who saw the picture found no light that a listed value fits. Record no shot light and state no light source or direction."
        in none
    )
    both = repair_text(repair_feedback(row_state([fix("rev-a", "soft window"), fix("rev-b", "neon")])))
    assert '"soft window", "neon"' in both and "No light record is set for you" in both
    # without a correction the text is what it was
    assert repair_feedback(row_state([fix("rev-a")])) == ("the light is wrong", json.dumps({"id": "r", "thoughts": ["t"]}))


def test_the_repair_item_follows_the_verdicts():
    assert repair_item(ITEM, row_state([fix("a", "candle")]))["spec"]["shot"]["light"] == "candle"
    assert "shot" not in repair_item(ITEM, row_state([fix("a", NONE_VALUE)]))["spec"]
    assert repair_item(ITEM, row_state([fix("a", "candle"), fix("b", "neon")])) is ITEM
    assert repair_item(ITEM, row_state([fix("a")])) is ITEM and repair_item(ITEM, None) is ITEM


# ------------------------------------------------------------------ the checker holds the repaired row to it


def item_row(light=None):
    spec = {"thing": {"name": "lamp"}}
    if light:
        spec["shot"] = {"light": light}
    return {"id": "x", "series": "s-1", "spec": spec, "source": {"license": "CC BY-NC"}}


def codes(row, item):
    return [p.code for p in validator.check_against_item(row, {**item, "license": "CC BY-NC"})]


def test_the_repaired_row_must_keep_the_value_and_a_none_row_must_hold_no_light():
    given = amended_item(ITEM, LIGHT, "candle")
    assert codes(item_row("candle"), given) == []
    assert "spec_kept" in codes(item_row("neon"), given) and "spec_kept" in codes(item_row(), given)
    none = amended_item(ITEM, LIGHT, NONE_VALUE)
    assert codes(item_row(), none) == []
    found = [p for p in validator.check_against_item(item_row("neon"), {**none, "license": "CC BY-NC"}) if p.code == "light_removed"]
    assert found and "no light that a listed value fits" in found[0].message and found[0].severity == "error"
    assert "light_removed" not in codes(item_row("neon"), ITEM)


# ------------------------------------------------------------------ through the controller


def lamp_task(lines=("001",)):
    return TaskSpec(
        source="photos",
        batch="1",
        route="fake-batch",
        purpose="production",
        lines=tuple(SpecLine(line, {**ITEM, "ids": [f"{line}-1"]}, item_id=f"img-{line}") for line in lines),
    )


def lamp_behavior(payload):
    from rps.providers import FakeOutcome

    given = (payload["spec"].get("spec") or {}).get("shot", {}).get("light")
    repaired = bool(payload.get("repair_of"))
    row = {"id": f"{payload['line']}-{payload['attempt']}", "thoughts": [("repaired: " if repaired else "") + "a lamp"], "spec": {"shot": {"light": given or "neon"}}}
    if repaired and given is None:
        row["spec"] = {}
    return FakeOutcome(output=json.dumps(row))


def reviewer_says(value):
    def judge(reviewer, item):
        if item.row["thoughts"][0].startswith("repaired"):
            return {"verdict": "keep"}
        out = {"verdict": "fix", "reasons": ["contradicts_picture"], "notes": "the light is wrong"}
        if reviewer.may_amend and value is not None:
            out["amend"] = {LIGHT: value}
        return out

    return judge


def lamp_world(tmp_path, reviewers=None):
    config = repair_config(reviewers or [CAN_AMEND])
    seen = []

    def assess(row, context):
        seen.append((row.get("id"), context.spec))
        return Assessment(passed=True)

    return World(tmp_path, config=config, behavior=lamp_behavior), seen, assess


def repair_payloads(world):
    return [e.payload for e in world.events_of("call_prepared") if e.payload.get("repair_of")]


def requests(world):
    return [r[2] for batch in world.provider.batches.values() for r in batch["requests"] if r[2].get("repair_of")]


@pytest.mark.parametrize("value", ["candle", NONE_VALUE])
def test_a_repair_after_a_correction_is_built_from_the_amended_item_and_assessed_against_it(tmp_path, value):
    world, seen, assess = lamp_world(tmp_path)
    controller = world.controller(assess=assess)
    controller.add_task(lamp_task())
    world.drive(controller, reviewer_says(value))
    sent = requests(world)
    assert len(sent) == 1
    spec = sent[0]["spec"]
    if value == NONE_VALUE:
        assert "shot" not in spec["spec"] and spec["amend"] == {LIGHT: NONE_VALUE}
        assert "found no light that a listed value fits" in sent[0]["task_text"]
    else:
        assert spec["spec"]["shot"] == {"light": "candle"}
        assert "gives the light as: candle. Keep this record exactly" in sent[0]["task_text"]
    assert LIGHT_NONE in sent[0]["feedback"] if value == NONE_VALUE else LIGHT_GIVEN.format(value=value) in sent[0]["feedback"]
    # the first row was assessed against the plain item, the repaired row against the amended one
    first, again = seen[0][1], seen[-1][1]
    assert first == {**ITEM, "ids": ["001-1"]} and again == spec
    # readable afterwards: the value, who gave it, the repaired row, and whether the row kept it
    (entry,) = controller.state.light_corrections()
    assert entry["value"] == value and entry["reviewers"] == [{"reviewer_id": "rev-b", "value": value}]
    assert len(entry["repaired_by"]) == 1 and entry["outcomes"][0]["kept"] is True
    assert entry["outcomes"][0]["row_id"] == entry["repaired_by"][0]


def test_a_restart_between_the_verdict_and_the_repair_keeps_the_correction(tmp_path):
    world, seen, assess = lamp_world(tmp_path)
    first = world.controller(assess=assess)
    first.add_task(lamp_task())
    for _ in range(10):
        first.step()
        if first.state.open_packets():
            break
    world.run_reviewers(reviewer_says("candle"))
    second = world.controller(assess=assess)  # a new run: the verdict is read from the verdict file
    world.drive(second, reviewer_says("candle"))
    assert requests(world)[0]["spec"]["spec"]["shot"] == {"light": "candle"}
    rebuilt = State.from_events(world.events())
    assert rebuilt.light_corrections() == second.state.light_corrections() and rebuilt.light_corrections()[0]["value"] == "candle"
    verdicts = [v for row in rebuilt.rows.values() for v in row.verdicts if v["verdict"] == "fix"]
    assert verdicts[0]["amend"] == {LIGHT: "candle"}


def test_the_controller_gives_only_the_reviewer_with_the_permission_a_packet_that_says_so(tmp_path):
    world, _seen, assess = lamp_world(tmp_path, [CAN_AMEND, {"id": "rev-c", "family": "gamma"}])
    controller = world.controller(assess=assess)
    controller.add_task(lamp_task(("001", "002")))
    for _ in range(10):
        controller.step()
        if len(controller.state.packets) >= 2:
            break
    docs = {p.reviewer_id: json.loads(world.state_store.read_text(layout.packet_json_path(p.reviewer_id, p.packet_id))) for p in controller.state.packets.values()}
    assert docs["rev-b"]["may_amend"] == [LIGHT] and docs["rev-b"]["amend_values"] == {LIGHT: VALUES}
    assert "may_amend" not in docs["rev-c"] and docs["rev-c"]["verdict_schema"] == VERDICT_SCHEMA


def test_an_amend_the_configuration_does_not_allow_is_dropped_and_the_verdict_still_counts(tmp_path):
    world, _seen, assess = lamp_world(tmp_path, [{"id": "rev-c", "family": "gamma"}])
    controller = world.controller(assess=assess)
    controller.add_task(lamp_task())
    for _ in range(10):
        controller.step()
        if controller.state.open_packets():
            break
    (packet,) = controller.state.open_packets()
    row_id = packet.items[0]["row_id"]
    line = {"row_id": row_id, "verdict": "fix", "reasons": ["contradicts_picture"], "notes": "light", "amend": {LIGHT: "candle"}}
    ActorStore(world.state_store, reviewer_actor("rev-c")).write_text(layout.verdicts_path("rev-c", packet.packet_id), json.dumps(line) + "\n")
    controller.step()
    (received,) = [e.payload for e in world.events_of("verdict_received")]
    assert received["verdict"] == "fix" and "amend" not in received
    assert any("the correction is dropped" in problem and "may not correct" in problem for problem in controller.problems)
    assert controller.state.light_corrections() == []


def test_an_invalid_value_from_a_reviewer_who_may_amend_is_dropped_too(tmp_path):
    world, _seen, assess = lamp_world(tmp_path)
    controller = world.controller(assess=assess)
    controller.add_task(lamp_task())
    for _ in range(10):
        controller.step()
        if controller.state.open_packets():
            break
    (packet,) = controller.state.open_packets()
    row_id = packet.items[0]["row_id"]
    line = {"row_id": row_id, "verdict": "fix", "reasons": ["contradicts_picture"], "notes": "light", "amend": {LIGHT: "plaid"}}
    ActorStore(world.state_store, reviewer_actor("rev-b")).write_text(layout.verdicts_path("rev-b", packet.packet_id), json.dumps(line) + "\n")
    controller.step()
    (received,) = [e.payload for e in world.events_of("verdict_received")]
    assert received["verdict"] == "fix" and "amend" not in received
    assert any("plaid" in problem for problem in controller.problems)


def test_a_repair_without_a_correction_is_built_as_before(tmp_path):
    world, seen, assess = lamp_world(tmp_path)
    controller = world.controller(assess=assess)
    controller.add_task(lamp_task())
    world.drive(controller, reviewer_says(None))
    sent = requests(world)
    assert sent[0]["spec"] == {**ITEM, "ids": ["001-1"]} and "reviewer who saw the picture" not in sent[0]["task_text"]
    assert controller.state.light_corrections() == []


def test_the_outcome_says_when_the_repaired_row_did_not_keep_the_value():
    original = row_state([fix("rev-b", "candle")])
    original.repair = {"call_id": "c", "route": "x", "calls": [], "rows": ["n"]}
    new = RowState(row_id="n", call_id="c2", task_id="t", line="001", route="x", record={"spec": {"shot": {"light": "neon"}}}, status="accepted")
    (entry,) = correction_outcomes({"r": original, "n": new})
    assert entry["outcomes"] == [{"row_id": "n", "kept": False, "status": "accepted"}]
    new.record = {"spec": {"shot": {"light": "candle"}}}
    assert correction_outcomes({"r": original, "n": new})[0]["outcomes"][0]["kept"] is True


# ------------------------------------------------------------------ a person who is not the picture's subject

RULES = Path(validator.__file__).resolve().parents[1] / "rules"
PROMPTS = [RULES / "writer_system.md", *(RULES / "profiles" / name / "writer_system.md" for name in ("openai", "gemini", "deepseek"))]
NOT_THE_SUBJECT = (
    "A person who is not the picture's subject (a passer-by, a figure far in the background, a hand or an arm at the edge, "
    "a shadow, a statue, a printed or painted figure) does not make you skip: write the row for its recorded things, "
    "describe no person and do not mention that one. Skip with class \"person\" only when a person is the subject of the picture."
)


@pytest.mark.parametrize("path", PROMPTS, ids=lambda p: p.parent.name)
def test_every_writer_prompt_says_a_person_who_is_not_the_subject_does_not_make_the_writer_skip(path):
    text = path.read_text(encoding="utf-8")
    assert NOT_THE_SUBJECT in text
    block = text.index("WHEN THE PICTURE IS ATTACHED")
    assert text.index(NOT_THE_SUBJECT) > block
    skip = next(line for line in text.splitlines() if "no person is in the records" in line or "no person is recorded" in line)
    assert "With the picture attached, the part headed WHEN THE PICTURE IS ATTACHED decides." in skip
    assert "A tree with a painted face is not a person." in skip  # the line keeps its text for a text call


def test_a_text_calls_task_text_has_nothing_about_the_new_rule():
    from rps.prompting import render_task_text

    assert "subject" not in render_task_text("spec for 001", picture=False)


def test_the_brief_and_the_pool_hold_the_new_paragraphs():
    brief = (RULES / "review_brief.md").read_text(encoding="utf-8")
    assert "is not the picture's subject" in brief and "`should_skip` for a picture whose subject is a person the records leave out" in brief
    assert "Never correct what you cannot see" in brief and "`may_amend`" in brief and "write the light the picture shows into the note, as before" in brief
    pool = (RULES / "pool.md").read_text(encoding="utf-8")
    for number in (103, 104, 105, 106):
        assert f"| {number} |" in pool
    assert "light_removed" in pool
