"""The review chain: per writer family, a first reviewer, a challenger, an arbiter, an overseer and then the owner."""

import json

import pytest

from rps import ids, layout
from rps.config import ChainEntry, Config, ConfigError, ReviewConfig
from rps.layout import reviewer_actor
from rps.providers import FakeOutcome
from rps.review import cli
from rps.review.api_seat import ASK_LINE, ASK_LINE_UNSURE, answer_schema, read_answer, reviews_text, user_text
from rps.review.client import ReviewError, ReviewerClient
from rps.review.packets import PacketError, build_packet, packet_document, publish_packet, validate_verdict, verdict_schema
from rps.review.policy import (
    CONTESTED_REASON,
    FIX_TO_HAND_REASON,
    STILL_CONTESTED_ARBITER,
    STILL_CONTESTED_OVERSEER,
    ChainStages,
    settle_chain,
    writer_verdicts,
)
from rps.state import Assignment, RowState, State
from rps.store import ActorStore, LocalStore
from rps.tasks import SpecLine, TaskSpec

from harness import World, task_spec
from helpers import Builder
from repair_helpers import repair_config, writes_and_rewrites
from review_helpers import flag_everything, review_config
from seat_helpers import FakeAnthropic, Clock, make_seat, run_until_quiet, verdict_file

LIGHT = "shot light"
REVIEWERS = [
    {"id": "rev-a", "family": "alpha"},
    {"id": "rev-x", "family": "xi"},
    {"id": "rev-b", "family": "beta"},
    {"id": "rev-c", "family": "gamma"},
    {"id": "rev-d", "family": "delta"},
]
FULL = {"first": ["rev-a", "rev-x"], "challenger": "rev-b", "arbiter": "rev-c", "overseer": "rev-d"}
WRITER = "writer-family"  # the family of the writing route of these tests
LINES = ("001", "002", "003", "004", "005", "006", "007", "008")


def with_chain(config, chain):
    data = config.to_dict()
    data["review"] = {**data["review"], "chain": chain}
    return Config.from_dict(data)


def chain_config(chain=None, *, reviewers=REVIEWERS, repair=False, policy=None, packet_size=3):
    base = repair_config(reviewers, enabled=repair, policy={"first_keep_accepts": False, **(policy or {})}) if repair else review_config(
        reviewers, packet_size=packet_size, policy=policy
    )
    return with_chain(base, {WRITER: FULL if chain is None else chain})


def judge(script, log=None):
    """A review function that answers by spec line and stage. `script` maps a line to {stage: word} (a word, or a word
    and extra fields as a tuple); a stage that is not named keeps. A row that was written again (its thought starts with
    "repaired") is judged by the line followed by `*`. The note says which stage wrote it."""

    def verdict(reviewer, item):
        stage = item.stage or "first"
        line = item.task["line_id"]
        again = item.row["thoughts"][0].startswith("repaired") if item.row.get("thoughts") else False
        spec = script.get(line + ("*" if again else ""), {}).get(stage, "keep")
        word, extra = spec if isinstance(spec, tuple) else (spec, {})
        if log is not None:
            log.append((reviewer.id, line, stage, item))
        if word == "keep":
            return {"verdict": "keep", **extra}
        return {"verdict": word, "reasons": ["needs_work"], "notes": f"{stage} note on {line}", **extra}

    return verdict


def run(tmp_path, script, *, chain=None, lines=LINES[:3], config=None, log=None, behavior=writes_and_rewrites, **kw):
    world = World(tmp_path, config=config or chain_config(chain, **kw), behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(lines))
    world.drive(controller, judge(script, log))
    return world, controller


def by_line(controller, repaired=False):
    return {
        row.line: row
        for row in controller.state.rows.values()
        if (row.repair_of is not None) == repaired
    }


def statuses(controller):
    return {line: row.status for line, row in by_line(controller).items()}


def hand_reason(row):
    return (row.hand or {}).get("reason")


def repair_requests(world):
    return {r[2]["line"]: r[2] for batch in world.provider.batches.values() for r in batch["requests"] if r[2].get("repair_of")}


def feedback(world, line):
    return " ".join(repair_requests(world)[line]["feedback"])


# ------------------------------------------------------------------ the configuration


def review(chain, reviewers=None):
    data = {"reviewers": reviewers or [*REVIEWERS, {"id": "rev-w", "family": WRITER}], "provider_families": {"fake": WRITER}}
    return ReviewConfig.from_dict({**data, "chain": chain})


def test_a_chain_is_read_and_written_back_and_left_out_when_empty():
    config = review({WRITER: FULL, "deepseek": {"first": ["rev-x"]}})
    assert config.chain_for(WRITER) == ChainEntry(("rev-a", "rev-x"), "rev-b", "rev-c", "rev-d")
    assert config.chain_for("deepseek") == ChainEntry(("rev-x",)) and config.chain_for("nobody") is None
    assert config.to_dict()["chain"] == {"deepseek": {"first": ["rev-x"]}, WRITER: FULL}
    assert ReviewConfig.from_dict(config.to_dict()) == config
    plain = ReviewConfig.from_dict({"reviewers": REVIEWERS})
    assert plain.chain == {} and "chain" not in plain.to_dict()
    assert Config.from_dict(review_config(REVIEWERS).to_dict()).to_dict()["review"].get("chain") is None
    assert review({}).chain == {}
    # the arbiter and the overseer may be of the writer's own family
    assert review({WRITER: {"first": ["rev-a"], "challenger": "rev-b", "arbiter": "rev-w", "overseer": "rev-c"}}).chain_for(WRITER).arbiter == "rev-w"


@pytest.mark.parametrize(
    "chain, message",
    [
        ([], "review.chain: expected an object that maps a writer family"),
        ({WRITER: ["rev-a"]}, f"review.chain.{WRITER}: expected an object with 'first'"),
        ({WRITER: {"first": ["rev-a"], "judge": "rev-b"}}, "unknown key"),
        ({WRITER: {}}, f"review.chain.{WRITER}.first: expected a non-empty list of reviewer ids"),
        ({WRITER: {"first": []}}, f"review.chain.{WRITER}.first: expected a non-empty list"),
        ({WRITER: {"first": "rev-a"}}, f"review.chain.{WRITER}.first: expected a non-empty list"),
        ({WRITER: {"first": ["rev-zz"]}}, f"review.chain.{WRITER}.first[0]: unknown reviewer 'rev-zz'"),
        ({WRITER: {"first": ["rev-a"], "challenger": "rev-zz"}}, f"review.chain.{WRITER}.challenger: unknown reviewer 'rev-zz'"),
        ({WRITER: {"first": ["rev-a"], "challenger": 5}}, f"review.chain.{WRITER}.challenger: expected one reviewer id"),
        ({WRITER: {"first": ["rev-a"], "arbiter": "rev-c"}}, f"review.chain.{WRITER}.arbiter: an arbiter needs a challenger"),
        ({WRITER: {"first": ["rev-a"], "challenger": "rev-b", "overseer": "rev-d"}}, f"review.chain.{WRITER}.overseer: an overseer needs an arbiter"),
        ({WRITER: {"first": ["rev-a", "rev-a"]}}, "first[1]: reviewer 'rev-a' is already named at"),
        ({WRITER: {"first": ["rev-a"], "challenger": "rev-a"}}, f"review.chain.{WRITER}.challenger: reviewer 'rev-a' is already named at"),
        ({WRITER: {"first": ["rev-a"], "challenger": "rev-b", "arbiter": "rev-b"}}, "arbiter: reviewer 'rev-b' is already named"),
        ({WRITER: {"first": ["rev-a"], "challenger": "rev-b", "arbiter": "rev-c", "overseer": "rev-a"}}, "overseer: reviewer 'rev-a' is already named"),
        ({WRITER: {"first": ["rev-w"]}}, f"first[0]: reviewer 'rev-w' is of the writer's own family '{WRITER}'"),
        ({WRITER: {"first": ["rev-a"], "challenger": "rev-w"}}, f"challenger: reviewer 'rev-w' is of the writer's own family '{WRITER}'"),
        ({"bad family": {"first": ["rev-a"]}}, "expected writer families as keys"),
    ],
)
def test_a_malformed_chain_is_a_configuration_error_that_says_what_and_where(chain, message):
    with pytest.raises(ConfigError) as error:
        review(chain)
    assert message in str(error.value)


# ------------------------------------------------------------------ the rule, as a pure function


def v(stage, word, unsure=False):
    return {"stage": stage, "verdict": word, **({"unsure": True} if unsure else {})}


FULL_STAGES = ChainStages(True, True, True)


@pytest.mark.parametrize(
    "stages, verdicts, outcome, reason_or_stage",
    [
        (ChainStages(), [], "need_first", "first"),
        (ChainStages(), [v("first", "keep")], "accept", None),
        (ChainStages(), [v("first", "fix")], "hand", FIX_TO_HAND_REASON),
        (ChainStages(), [v("first", "reject")], "discard", None),
        (FULL_STAGES, [v("first", "keep")], "further", "challenge"),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "keep")], "accept", None),
        (FULL_STAGES, [v("first", "reject"), v("challenge", "reject")], "discard", None),
        (FULL_STAGES, [v("first", "fix"), v("challenge", "fix")], "hand", FIX_TO_HAND_REASON),
        (FULL_STAGES, [v("first", "fix"), v("challenge", "reject")], "hand", FIX_TO_HAND_REASON),
        (FULL_STAGES, [v("first", "reject"), v("challenge", "fix")], "hand", FIX_TO_HAND_REASON),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix")], "further", "arbiter"),
        (FULL_STAGES, [v("first", "reject"), v("challenge", "keep")], "further", "arbiter"),
        (ChainStages(True), [v("first", "keep"), v("challenge", "fix")], "hand", CONTESTED_REASON),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "keep")], "accept", None),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "fix")], "hand", FIX_TO_HAND_REASON),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "reject")], "discard", None),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "keep", True)], "further", "overseer"),
        (ChainStages(True, True), [v("first", "keep"), v("challenge", "fix"), v("arbiter", "keep", True)], "hand", STILL_CONTESTED_ARBITER),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "fix", True), v("overseer", "reject")], "discard", None),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "fix", True), v("overseer", "fix")], "hand", FIX_TO_HAND_REASON),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "fix", True), v("overseer", "keep")], "accept", None),
        (FULL_STAGES, [v("first", "keep"), v("challenge", "fix"), v("arbiter", "fix", True), v("overseer", "keep", True)], "hand", STILL_CONTESTED_OVERSEER),
    ],
)
def test_settle_chain_follows_the_rule_as_written(stages, verdicts, outcome, reason_or_stage):
    settlement = settle_chain(stages, verdicts)
    assert settlement.outcome == outcome
    if outcome == "hand":
        assert settlement.reason == reason_or_stage
    elif outcome in ("further", "need_first"):
        assert settlement.stage == reason_or_stage


def test_the_notes_that_go_back_to_the_writer_follow_who_settled_the_row():
    first, challenge = v("first", "fix"), v("challenge", "reject")
    assert writer_verdicts([first, challenge]) == [first, challenge]
    assert writer_verdicts([first]) == [first]
    arbiter = v("arbiter", "fix")
    assert writer_verdicts([v("first", "keep"), v("challenge", "fix"), arbiter]) == [arbiter]
    overseer = v("overseer", "fix")
    unsure = v("arbiter", "fix", True)
    assert writer_verdicts([v("first", "keep"), v("challenge", "fix"), unsure, overseer]) == [overseer]
    assert writer_verdicts([{"verdict": "fix"}]) == []  # a verdict outside a chain has no stage


# ------------------------------------------------------------------ first reviewers


def test_first_reviews_go_only_to_the_listed_reviewers_and_rotate_by_weight(tmp_path):
    reviewers = [{**r, **({"weight": 3} if r["id"] == "rev-x" else {})} for r in REVIEWERS]
    log = []
    world, controller = run(tmp_path, {}, chain={"first": ["rev-a", "rev-x"]}, reviewers=reviewers, lines=LINES, log=log)
    first = [entry[0] for entry in log]
    assert set(first) == {"rev-a", "rev-x"} and first.count("rev-x") == 6 and first.count("rev-a") == 2
    assert set(statuses(controller).values()) == {"accepted"}
    assert all(a.stage == "first" and a.writer_family == WRITER for row in controller.state.rows.values() for a in row.assignments)


def test_a_first_reviewer_that_is_switched_off_is_skipped_and_a_row_with_no_first_reviewer_waits_for_the_owner(tmp_path):
    off = [{**r, **({"enabled": False} if r["id"] == "rev-a" else {})} for r in REVIEWERS]
    log = []
    run(tmp_path / "one", {}, chain={"first": ["rev-a", "rev-x"]}, reviewers=off, log=log)
    assert {entry[0] for entry in log} == {"rev-x"}
    world = World(tmp_path / "two", config=chain_config({"first": ["rev-a"]}, reviewers=off), behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller, judge({}))
    (row,) = controller.state.rows.values()
    assert row.status == "hand" and hand_reason(row) == "no reviewer from another family than the writer is available"
    # a reviewer that is on again picks the row up
    world.config = chain_config({"first": ["rev-a"]})
    again = world.controller()
    world.drive(again, judge({}))
    assert again.state.rows[row.row_id].status == "accepted" and world.events_of("row_reopened")


# ------------------------------------------------------------------ a family with `first` only


def test_a_family_with_first_only_accepts_a_keep_repairs_a_fix_once_and_discards_a_reject(tmp_path):
    world, controller = run(
        tmp_path, {"002": {"first": "fix"}, "003": {"first": "reject"}}, chain={"first": ["rev-a"]}, repair=True
    )
    states = statuses(controller)
    assert states == {"001": "accepted", "002": "repaired", "003": "rejected"}
    (rewrite,) = repair_requests(world).values()
    assert "first note on 002" in " ".join(rewrite["feedback"])
    (again,) = [row for row in controller.state.rows.values() if row.repair_of is not None]
    assert again.status == "accepted" and again.under_chain
    assert controller.state.chain_tally()["single"] == {"accept": 2, "back": 1, "discard": 1}
    assert world.events_of("row_rejected")[0].payload["by"] == "reviewers"


def test_a_row_sent_back_a_second_time_stays_with_the_owner(tmp_path):
    world, controller = run(
        tmp_path, {"001": {"first": "fix"}, "001*": {"first": "fix"}}, chain={"first": ["rev-a"]}, lines=("001",), repair=True
    )
    (original,) = by_line(controller).values()
    (again,) = by_line(controller, repaired=True).values()
    assert original.status == "repaired" and again.status == "hand" and hand_reason(again) == FIX_TO_HAND_REASON
    assert len(repair_requests(world)) == 1


# ------------------------------------------------------------------ agreement


def test_agreement_accepts_discards_or_sends_back_with_the_notes_of_both(tmp_path):
    script = {
        "001": {"first": "keep", "challenge": "keep"},
        "002": {"first": "fix", "challenge": "fix"},
        "003": {"first": "reject", "challenge": "reject"},
        "004": {"first": "fix", "challenge": "reject"},
    }
    world, controller = run(tmp_path, script, lines=LINES[:4], repair=True)
    assert statuses(controller) == {"001": "accepted", "002": "repaired", "003": "rejected", "004": "repaired"}
    for line in ("002", "004"):
        text = feedback(world, line)
        assert f"first note on {line}" in text and f"challenge note on {line}" in text
        assert text.index("first note") < text.index("challenge note")
    stages = {a.stage for row in controller.state.rows.values() for a in row.assignments}
    assert stages == {"first", "challenge"}  # nobody asked an arbiter
    tally = controller.state.chain_tally()
    # the two rows written again were kept by both reviewers too, so three rows of the run agreed on a keep
    assert tally["agreed"] == {"accept": 3, "back": 2, "discard": 1} and tally["contested"] == 0 and tally["rows"] == 6


def test_the_policy_and_an_assessor_flag_do_not_apply_under_a_chain(tmp_path):
    world = World(tmp_path, config=chain_config(policy={"keeps_to_accept": 5, "max_reviewers": 5}), behavior=writes_and_rewrites)
    controller = world.controller(assess=flag_everything)
    controller.add_task(task_spec(("001",)))
    world.drive(controller, judge({}))
    (row,) = controller.state.rows.values()
    assert row.flags and row.status == "accepted" and [v["verdict"] for v in row.verdicts] == ["keep", "keep"]


# ------------------------------------------------------------------ the arbiter


def packet_docs(world, controller, stage):
    docs = []
    for packet in controller.state.packets.values():
        if packet.items[0].get("stage") == stage:
            docs.append(json.loads(world.state_store.read_text(layout.packet_json_path(packet.reviewer_id, packet.packet_id))))
    return docs


def test_a_disagreement_goes_to_the_arbiter_with_both_reviews_and_no_reviewer_identity(tmp_path):
    script = {"001": {"first": "keep", "challenge": "fix", "arbiter": "keep"}}
    world, controller = run(tmp_path, script, lines=("001",))
    (doc,) = packet_docs(world, controller, "arbiter")
    (entry,) = doc["rows"]
    assert doc["reviewer_id"] == "rev-c" and entry["stage"] == "arbiter"
    assert [(r["stage"], r["verdict"]) for r in entry["reviews"]] == [("first", "keep"), ("challenge", "fix")]
    assert entry["reviews"][1]["notes"] == "challenge note on 001" and entry["reviews"][1]["reasons"] == ["needs_work"]
    assert all(set(r) <= {"stage", "verdict", "reasons", "notes", "amend"} for r in entry["reviews"])
    text = json.dumps(doc)
    for hidden in ("rev-a", "rev-x", "rev-b", "alpha", "beta"):
        assert hidden not in text
    assert "unsure" in doc["verdict_schema"]["properties"]
    # the packets of the other stages: the challenger sees the first review, a first reviewer sees nothing new
    (challenge_doc,) = packet_docs(world, controller, "challenge")
    assert [r["stage"] for r in challenge_doc["rows"][0]["reviews"]] == ["first"] and "unsure" not in challenge_doc["verdict_schema"]["properties"]
    first_packet = next(p for p in controller.state.packets.values() if p.reviewer_id in ("rev-a", "rev-x"))
    first_doc = json.loads(world.state_store.read_text(layout.packet_json_path(first_packet.reviewer_id, first_packet.packet_id)))
    assert "stage" not in first_doc["rows"][0] and "reviews" not in first_doc["rows"][0]
    assert first_doc["verdict_schema"] == json.loads(json.dumps(verdict_schema()))
    assert statuses(controller) == {"001": "accepted"}


def test_the_arbiter_keeps_fixes_or_rejects(tmp_path):
    split = {"first": "keep", "challenge": "fix"}
    script = {
        "001": {**split, "arbiter": "keep"},
        "002": {**split, "arbiter": "fix"},
        "003": {**split, "arbiter": "reject"},
    }
    world, controller = run(tmp_path, script, repair=True)
    assert statuses(controller) == {"001": "accepted", "002": "repaired", "003": "rejected"}
    text = feedback(world, "002")
    assert "arbiter note on 002" in text and "first note" not in text and "challenge note" not in text
    tally = controller.state.chain_tally()
    assert tally["contested"] == 3 and tally["arbiter"] == {
        "keep": 1, "fix": 1, "reject": 1, "unsure": 0, "with_first": 1, "with_challenger": 2,
    }


def test_an_unsure_arbiter_goes_to_the_overseer_with_three_reviews_and_an_unsure_overseer_goes_to_the_owner(tmp_path):
    split = {"first": "keep", "challenge": "fix", "arbiter": ("keep", {"unsure": True})}
    script = {
        "001": {**split, "overseer": "keep"},
        "002": {**split, "overseer": ("fix", {"unsure": True})},
        "003": {**split, "overseer": "reject"},
    }
    world, controller = run(tmp_path, script)
    assert statuses(controller) == {"001": "accepted", "002": "hand", "003": "rejected"}
    rows = by_line(controller)
    assert hand_reason(rows["002"]) == STILL_CONTESTED_OVERSEER
    docs = packet_docs(world, controller, "overseer")
    entries = [entry for doc in docs for entry in doc["rows"]]
    assert len(entries) == 3 and all([r["stage"] for r in e["reviews"]] == ["first", "challenge", "arbiter"] for e in entries)
    assert all(doc["reviewer_id"] == "rev-d" and "unsure" in doc["verdict_schema"]["properties"] for doc in docs)
    assert all("unsure" not in review for e in entries for review in e["reviews"])
    assert any(v.get("unsure") for v in rows["002"].verdicts) and rows["002"].verdicts[2]["stage"] == "arbiter"
    assert controller.state.chain_tally()["to_owner"] == 1


def test_a_missing_arbiter_or_overseer_sends_the_row_to_the_owner_with_its_reason(tmp_path):
    contested = {"001": {"first": "keep", "challenge": "fix"}}
    _world, controller = run(tmp_path / "a", contested, chain={"first": ["rev-a"], "challenger": "rev-b"}, lines=("001",))
    assert hand_reason(by_line(controller)["001"]) == CONTESTED_REASON
    unsure = {"001": {"first": "keep", "challenge": "fix", "arbiter": ("keep", {"unsure": True})}}
    chain = {"first": ["rev-a"], "challenger": "rev-b", "arbiter": "rev-c"}
    _world, controller = run(tmp_path / "b", unsure, chain=chain, lines=("001",))
    assert hand_reason(by_line(controller)["001"]) == STILL_CONTESTED_ARBITER
    # a stage whose reviewer is switched off counts as not configured
    off = [{**r, **({"enabled": False} if r["id"] == "rev-c" else {})} for r in REVIEWERS]
    _world, controller = run(tmp_path / "c", contested, reviewers=off, lines=("001",))
    assert hand_reason(by_line(controller)["001"]) == CONTESTED_REASON
    off = [{**r, **({"enabled": False} if r["id"] == "rev-b" else {})} for r in REVIEWERS]
    _world, controller = run(tmp_path / "d", {"001": {"first": "fix"}}, reviewers=off, lines=("001",))
    assert hand_reason(by_line(controller)["001"]) == FIX_TO_HAND_REASON  # no challenger: the first review decides


# ------------------------------------------------------------------ unsure


def staged_packet(stage="arbiter", reviewer="rev-c", rows=2):
    b = Builder()
    call = ids.call_id(ids.task_id("photos", "1", {"001": "x"}), "001", 1)
    stored = [b.row(call, i) for i in range(rows)]
    line = {"line_id": "001", "item_id": "img-001", "spec": "a cat"}
    reviews = [{"stage": "first", "verdict": "keep", "reasons": [], "notes": "fine"}, {"stage": "challenge", "verdict": "fix", "reasons": ["invented"], "notes": "n"}]
    return build_packet(
        packet_id=ids.packet_id([f"{reviewer}:{r['row_id']}" for r in stored]),
        reviewer_id=reviewer,
        brief_version="1",
        created="2026-11-10T09:00:00.000000Z",
        rows=[(row, line, b"pic", "jpg") for row in stored],
        chain={r["row_id"]: {"stage": stage, "reviews": reviews} for r in stored} if stage else None,
    )


def desk(tmp_path, packet):
    remote = LocalStore(tmp_path / "remote", fsync=False)
    publish_packet(ActorStore(remote, layout.controller_actor("20261110T090000Z-aaaa")), packet)
    client = ReviewerClient(packet.reviewer_id, remote, tmp_path / "desk", fsync=False)
    local, _ = client.pull()
    return client, local, remote


@pytest.mark.parametrize("stage", ["arbiter", "overseer"])
def test_the_client_records_unsure_on_an_arbiter_or_overseer_row(tmp_path, stage):
    client, local, _remote = desk(tmp_path, staged_packet(stage))
    assert local.stage == stage
    first, second = local.row_ids
    kept = client.record_verdict(local.packet_id, first, "keep", [], "close call", unsure=True)
    assert kept["unsure"] is True and client.judged_rows(local)[first]["unsure"] is True
    sure = client.record_verdict(local.packet_id, second, "keep", [], "", unsure=False)
    assert "unsure" not in sure  # false is the same as absent
    assert client.check(local.packet_id).complete and client.push(local.packet_id).pushed


@pytest.mark.parametrize("stage", ["challenge", None])
def test_the_client_refuses_unsure_anywhere_else_at_record_check_and_push(tmp_path, stage):
    client, local, _remote = desk(tmp_path, staged_packet(stage))
    first, second = local.row_ids
    with pytest.raises(ReviewError, match="unsure is allowed only on a row whose packet entry says its stage is arbiter or overseer"):
        client.record_verdict(local.packet_id, first, "keep", [], "", unsure=True)
    lines = [{"row_id": r, "verdict": "keep", "reasons": [], "notes": "", "unsure": True} for r in local.row_ids]
    local.verdicts_file.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    report = client.check(local.packet_id)
    assert not report.ok and any("unsure" in problem for problem in report.problems)
    result = client.push(local.packet_id)
    assert not result.pushed and result.reasons
    assert validate_verdict({"row_id": "r", "verdict": "keep", "unsure": "yes"}, may_unsure=True)
    assert validate_verdict({"row_id": "r", "verdict": "keep", "unsure": True}, may_unsure=True) == []


class Sink:
    def __init__(self):
        self.parts = []

    def write(self, text):
        self.parts.append(text)

    @property
    def text(self):
        return "".join(self.parts)


def test_the_command_line_takes_unsure_only_where_the_packet_allows_it(tmp_path):
    client, local, _remote = desk(tmp_path, staged_packet("arbiter"))
    out, err = Sink(), Sink()
    base = ["verdict", "--packet", local.packet_id, "--verdict", "keep", "--into", str(tmp_path / "desk")]
    assert cli.run([*base, "--row", local.row_ids[0], "--unsure"], out, err) == cli.EXIT_OK
    assert client.judged_rows(client.local_packet(local.packet_id))[local.row_ids[0]]["unsure"] is True
    other = tmp_path / "other"
    other.mkdir()
    _client, plain, _remote2 = desk(other, staged_packet("challenge"))
    code = cli.run(["verdict", "--packet", plain.packet_id, "--row", plain.row_ids[0], "--verdict", "keep", "--unsure", "--into", str(other / "desk")], out, err)
    assert code == cli.EXIT_INVALID and "unsure is allowed only" in err.text


def test_the_controller_drops_an_unsure_mark_it_should_not_have_and_the_verdict_counts(tmp_path):
    world = World(tmp_path, config=chain_config({"first": ["rev-a"]}), behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    for _ in range(10):
        controller.step()
        if controller.state.open_packets():
            break
    (packet,) = controller.state.open_packets()
    row_id = packet.items[0]["row_id"]
    line = {"row_id": row_id, "verdict": "keep", "reasons": [], "notes": "", "unsure": True}
    ActorStore(world.state_store, reviewer_actor("rev-a")).write_text(layout.verdicts_path("rev-a", packet.packet_id), json.dumps(line) + "\n")
    controller.step()
    (received,) = [e.payload for e in world.events_of("verdict_received")]
    assert received["verdict"] == "keep" and "unsure" not in received
    assert any("the unsure mark is dropped, the verdict counts" in problem for problem in controller.problems)
    assert controller.state.rows[row_id].status == "accepted"


# ------------------------------------------------------------------ a light correction


ITEM = {"kind": "single", "series": "s-1", "ids": ["x"], "depth": "simple", "spec": {"shot": {"light": "neon"}, "thing": {"name": "lamp"}}}


def lamp_task(lines):
    return TaskSpec(
        source="photos",
        batch="1",
        route="fake-batch",
        purpose="production",
        lines=tuple(SpecLine(line, {**ITEM, "ids": [f"{line}-1"]}, item_id=f"img-{line}") for line in lines),
    )


def lamp_behavior(payload):
    given = (payload["spec"].get("spec") or {}).get("shot", {}).get("light")
    repaired = bool(payload.get("repair_of"))
    row = {"id": f"{payload['line']}-{payload['attempt']}", "thoughts": [("repaired: " if repaired else "") + "a lamp"], "spec": {"shot": {"light": given or "neon"}}}
    return FakeOutcome(output=json.dumps(row))


def test_a_light_correction_is_used_after_agreement_and_ignored_after_an_arbiters_own_fix(tmp_path):
    reviewers = [{**r, **({"may_amend": [LIGHT]} if r["id"] in ("rev-a", "rev-b") else {})} for r in REVIEWERS]
    config = chain_config(FULL | {"first": ["rev-a"]}, reviewers=reviewers, repair=True)
    world = World(tmp_path, config=config, behavior=lamp_behavior)
    controller = world.controller()
    controller.add_task(lamp_task(("001", "002")))
    script = {
        "001": {"first": ("fix", {"amend": {LIGHT: "candle"}}), "challenge": "fix"},
        "002": {"first": ("fix", {"amend": {LIGHT: "candle"}}), "challenge": "keep", "arbiter": "fix"},
    }
    world.drive(controller, judge(script))
    sent = repair_requests(world)
    assert sent["001"]["spec"]["spec"]["shot"] == {"light": "candle"} and "gives the light as: candle" in sent["001"]["task_text"]
    assert sent["002"]["spec"]["spec"]["shot"] == {"light": "neon"} and "reviewer who saw the picture" not in sent["002"]["task_text"]
    assert [entry["row_id"] for entry in controller.state.light_corrections()] == [by_line(controller)["001"].row_id]
    assert by_line(controller)["002"].verdicts[0]["amend"] == {LIGHT: "candle"}  # on record, but not carried to the writer


# ------------------------------------------------------------------ restarts, rows made before a chain, no audit


def plan_of(controller):
    plan, to_hand = controller._plan_assignments()
    return (
        {reviewer: [(row.row_id, round_, audit, controller._next_stage(row)) for row, round_, audit in items] for reviewer, items in plan.items()},
        sorted((row.row_id, reason) for row, reason in to_hand),
    )


def test_a_restart_between_any_two_stages_gives_the_same_plan_and_the_same_tally(tmp_path):
    script = {
        "001": {"first": "keep", "challenge": "fix", "arbiter": ("fix", {"unsure": True}), "overseer": "keep"},
        "002": {"first": "fix", "challenge": "keep", "arbiter": "reject"},
        "003": {"first": "keep", "challenge": "keep"},
    }
    world = World(tmp_path, config=chain_config(), behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(LINES[:3]))
    seen_stages = set()
    for _ in range(40):
        controller.step()
        fresh = world.controller()
        fresh.start()
        assert plan_of(fresh) == plan_of(controller)
        assert State.from_events(world.events()).chain_tally() == controller.state.chain_tally()
        seen_stages |= {a.stage for row in controller.state.rows.values() for a in row.assignments}
        world.run_reviewers(judge(script))
        if controller.is_idle() and not controller.state.open_packets():
            break
    assert seen_stages == {"first", "challenge", "arbiter", "overseer"}
    assert statuses(controller) == {"001": "accepted", "002": "rejected", "003": "accepted"}


def test_a_row_with_a_review_made_before_the_chain_finishes_under_the_policy_and_a_later_row_follows_the_chain(tmp_path):
    plain = review_config(REVIEWERS, packet_size=3)
    world = World(tmp_path, config=plain, behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    for _ in range(10):
        controller.step()
        if controller.state.open_packets():
            break
    world.run_reviewers(judge({"001": {"first": "reject"}}))  # the first review, made with no chain
    # the chain is configured now, and a second row is written
    world.config = with_chain(plain, {WRITER: FULL})
    after = world.controller()
    after.add_task(task_spec(("002",), batch="2"))
    world.drive(after, judge({"001": {"first": "reject"}, "002": {"first": "keep", "challenge": "keep"}}))
    old, new = by_line(after)["001"], by_line(after)["002"]
    assert old.status == "rejected" and not old.under_chain and all(a.stage is None for a in old.assignments)
    assert len([v for v in old.verdicts if v["verdict"] == "reject"]) == 2  # two rejects under the policy
    assert new.status == "accepted" and new.under_chain and [a.stage for a in new.assignments] == ["first", "challenge"]
    assert State.from_events(world.events()).rows[old.row_id].under_chain is False


def test_no_audit_is_drawn_from_a_row_a_chain_settled(tmp_path):
    policy = {"audit_fraction": 1.0}
    _world, controller = run(tmp_path / "chain", {}, policy=policy)
    assert not any(a.audit for row in controller.state.rows.values() for a in row.assignments)
    plain = review_config(REVIEWERS, policy=policy)
    world = World(tmp_path / "plain", config=plain, behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(LINES[:3]))
    world.drive(controller, judge({}))
    assert any(a.audit for row in controller.state.rows.values() for a in row.assignments)  # the control: the sample is drawn there


# ------------------------------------------------------------------ nothing changes outside a chain


def packet_texts(world):
    return {
        path: world.state_store.read_text(path)
        for path in sorted(world.state_store.list("packets/"))
        if path.endswith("packet.json")
    }


def test_a_configuration_without_a_chain_or_with_another_familys_chain_makes_the_same_packets(tmp_path):
    plain = review_config(REVIEWERS, packet_size=3)
    other = with_chain(plain, {"some-other-family": FULL})
    texts = []
    for name, config in (("plain", plain), ("other", other)):
        world = World(tmp_path / name, config=config, behavior=writes_and_rewrites)
        controller = world.controller()
        controller.add_task(task_spec(LINES[:4]))
        for _ in range(10):
            controller.step()
            if controller.state.open_packets():
                break
        texts.append(packet_texts(world))
        events = [e.payload for e in world.events_of("packet_published")]
        assert events and all("stage" not in item and "writer_family" not in item for p in events for item in p["items"])
    assert texts[0] and texts[0] == texts[1]
    for text in texts[0].values():
        doc = json.loads(text)
        assert list(doc) == ["format", "packet_id", "reviewer_id", "brief_version", "created", "verdict_schema", "rows"]
        assert doc["verdict_schema"] == verdict_schema() and all("stage" not in row and "reviews" not in row for row in doc["rows"])
    assert all(a.stage is None for row in controller.state.rows.values() for a in row.assignments)


def test_the_packet_builder_keeps_one_stage_to_a_packet_and_shows_nothing_of_the_reviewer():
    b = Builder()
    call = ids.call_id(ids.task_id("photos", "1", {"001": "x"}), "001", 1)
    stored = [b.row(call, i) for i in range(2)]
    line = {"line_id": "001", "item_id": "img-001", "spec": "a cat"}
    rows = [(r, line, None, None) for r in stored]
    one = {"stage": "arbiter", "reviews": []}
    other = {"stage": "overseer", "reviews": []}
    with pytest.raises(PacketError, match="one stage"):
        build_packet(packet_id="p", reviewer_id="rev-c", brief_version="1", created="t", rows=rows, chain={stored[0]["row_id"]: one, stored[1]["row_id"]: other})
    with pytest.raises(PacketError, match="one stage"):
        build_packet(packet_id="p", reviewer_id="rev-c", brief_version="1", created="t", rows=rows, chain={stored[0]["row_id"]: one})
    leaky = {"stage": "arbiter", "reviews": [{"stage": "first", "verdict": "keep", "reviewer_id": "rev-a"}]}
    with pytest.raises(PacketError, match="shows only"):
        build_packet(packet_id="p", reviewer_id="rev-c", brief_version="1", created="t", rows=rows, chain={r["row_id"]: leaky for r in stored})
    with pytest.raises(PacketError, match="challenge, arbiter or overseer"):
        build_packet(packet_id="p", reviewer_id="rev-c", brief_version="1", created="t", rows=rows, chain={r["row_id"]: {"stage": "first"} for r in stored})
    made = build_packet(packet_id="p", reviewer_id="rev-c", brief_version="1", created="t", rows=rows, chain={r["row_id"]: one for r in stored})
    assert made.stage == "arbiter" and [i["stage"] for i in made.items()] == ["arbiter", "arbiter"]
    assert packet_document(made)["rows"][0]["reviews"] == []


# ------------------------------------------------------------------ the pull and status output


def test_pull_and_status_show_the_stage_and_the_earlier_reviews(tmp_path, monkeypatch):
    world = World(tmp_path, config=chain_config(), behavior=writes_and_rewrites)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    script = {"001": {"first": "keep", "challenge": "fix"}}
    for _ in range(20):
        controller.step()
        if any(p.items[0].get("stage") == "arbiter" for p in controller.state.packets.values()):
            break
        world.run_reviewers(judge(script))
    monkeypatch.setattr(cli.Config, "load", staticmethod(lambda path: world.config))
    monkeypatch.setattr(cli, "build_state_store", lambda config: world.state_store)
    out, err = Sink(), Sink()
    into = str(tmp_path / "arbiter-desk")
    assert cli.run(["pull", "--reviewer", "rev-c", "--config", "c.json", "--into", into], out, err) == cli.EXIT_OK
    assert "stage: arbiter; earlier reviews: first keep; challenge fix (needs_work)" in out.text
    assert "This packet is at the arbiter stage" in out.text and "--unsure" in out.text
    out2 = Sink()
    assert cli.run(["status", "--reviewer", "rev-c", "--config", "c.json", "--into", into], out2, err) == cli.EXIT_OK
    assert ", stage arbiter" in out2.text
    pull_text = out.text
    assert pull_text  # kept for the report: what pull prints for a staged packet


# ------------------------------------------------------------------ the API review seat


def staged_entry(stage="arbiter"):
    packet = staged_packet(stage, reviewer="claude-haiku", rows=1)
    return packet_document(packet)["rows"][0]


def test_the_seat_text_shows_the_earlier_reviews_after_the_row_and_before_the_instruction():
    entry = staged_entry("arbiter")
    text = user_text(entry)
    head, _, tail = text.partition("## Earlier reviews of this row")
    assert json.loads(head)["row_id"] == entry["row_id"] and tail.endswith("\n\n" + ASK_LINE_UNSURE)
    assert "Review 1 (first): verdict keep; no reason codes; note: fine" in tail
    assert "Review 2 (challenge): verdict fix; reason codes: invented; note: n" in tail
    assert "rev-" not in tail
    withs = {**entry, "reviews": [{"stage": "first", "verdict": "fix", "reasons": ["a"], "notes": "n", "amend": {LIGHT: "candle"}}], "stage": "challenge"}
    assert "correction: shot light = candle" in reviews_text(withs) and user_text(withs).endswith("\n\n" + ASK_LINE)
    plain = {k: v for k, v in entry.items() if k not in ("stage", "reviews")}
    assert user_text(plain) == json.dumps({"row_id": plain["row_id"], "row": plain["row"], "task": plain.get("task")}, ensure_ascii=True, indent=1) + "\n\n" + ASK_LINE


def test_the_seat_accepts_unsure_only_for_an_arbiter_or_overseer_row():
    answer = json.dumps({"verdict": "keep", "reasons": [], "notes": "close", "unsure": True})
    verdict, problem = read_answer(answer, "r1", allow_unsure=True)
    assert problem == "" and verdict["unsure"] is True
    refused, problem = read_answer(answer, "r1")
    assert refused is None and "unexpected field" in problem
    assert "unsure" in answer_schema(unsure=True)["properties"] and "unsure" not in answer_schema()["properties"]
    unsure_false, _ = read_answer(json.dumps({"verdict": "keep", "reasons": [], "notes": "", "unsure": False}), "r1", allow_unsure=True)
    assert "unsure" not in unsure_false


def test_the_seat_asks_a_staged_row_with_the_reviews_and_writes_unsure_into_its_verdict_file(tmp_path):
    remote = LocalStore(tmp_path / "remote", fsync=False)
    packet = staged_packet("arbiter", reviewer="claude-haiku", rows=1)
    publish_packet(ActorStore(remote, layout.controller_actor("20261110T090000Z-aaaa")), packet)
    api = FakeAnthropic(Clock(), answer=lambda request: json.dumps({"verdict": "keep", "reasons": [], "notes": "close", "unsure": True}))
    # the packet's brief version is "1", not the one the package renders: name it so that the seat judges the packet
    seat, _printed = make_seat(remote, tmp_path, api, also_brief_versions=["1"])
    run_until_quiet(seat, api)
    (request,) = api.batches.sent
    text = request["params"]["messages"][0]["content"][-1]["text"]
    assert "## Earlier reviews of this row" in text and text.endswith(ASK_LINE_UNSURE)
    assert "unsure" in request["params"]["output_config"]["format"]["schema"]["properties"]
    (line,) = verdict_file(remote, packet)
    assert line["verdict"] == "keep" and line["unsure"] is True


# ------------------------------------------------------------------ the tally


def test_the_tally_of_a_scripted_run_by_outcome_and_by_family(tmp_path):
    script = {
        "001": {"first": "keep", "challenge": "keep"},
        "002": {"first": "fix", "challenge": "fix"},
        "003": {"first": "reject", "challenge": "reject"},
        "004": {"first": "keep", "challenge": "fix", "arbiter": "keep"},
        "005": {"first": "keep", "challenge": "reject", "arbiter": "reject"},
        "006": {"first": "fix", "challenge": "keep", "arbiter": ("fix", {"unsure": True}), "overseer": "fix"},
        "007": {"first": "reject", "challenge": "keep", "arbiter": ("keep", {"unsure": True}), "overseer": ("reject", {"unsure": True})},
        "008": {"first": "keep", "challenge": "reject", "arbiter": "fix"},
    }
    world, controller = run(tmp_path, script, lines=LINES)
    tally = controller.state.chain_tally()
    expected = {
        "rows": 8,
        "waiting": 0,
        "agreed": {"accept": 1, "back": 1, "discard": 1},
        "single": {"accept": 0, "back": 0, "discard": 0},
        "contested": 5,
        "arbiter": {"keep": 2, "fix": 2, "reject": 1, "unsure": 2, "with_first": 1, "with_challenger": 2},
        "overseer": {"keep": 0, "fix": 1, "reject": 1, "unsure": 1},
        "to_owner": 1,
    }
    assert {k: v for k, v in tally.items() if k != "by_family"} == expected
    assert tally["by_family"] == {WRITER: expected}
    assert statuses(controller) == {
        "001": "accepted", "002": "hand", "003": "rejected", "004": "accepted",
        "005": "rejected", "006": "hand", "007": "hand", "008": "hand",
    }
    assert State.from_events(world.events()).chain_tally() == tally
    print("chain_tally of the scripted run:", json.dumps(tally, sort_keys=True))


def test_the_tally_counts_rows_by_their_writer_family_and_counts_waiting_rows():
    state = State()

    def row(row_id, family, verdicts, status="accepted"):
        made = RowState(row_id=row_id, call_id="c", task_id="t", line="001", route="r", record={}, status=status)
        made.assignments = [Assignment("p", "rev-a", "alpha", 1, stage="first", writer_family=family)]
        made.verdicts = [{"reviewer_id": "x", "family": "f", "reasons": [], "notes": "", "audit": False, **item} for item in verdicts]
        state.rows[row_id] = made

    row("a", "openai", [v("first", "keep"), v("challenge", "keep")])
    row("b", "openai", [v("first", "keep")], status="in_question")
    row("c", "deepseek", [v("first", "fix")], status="hand")
    plain = RowState(row_id="d", call_id="c", task_id="t", line="001", route="r", record={})
    plain.assignments = [Assignment("p", "rev-a", "alpha", 1)]
    state.rows["d"] = plain  # a row outside a chain is not in the tally
    tally = state.chain_tally()
    assert tally["rows"] == 3 and tally["waiting"] == 1 and tally["agreed"]["accept"] == 1 and tally["single"]["back"] == 1
    assert tally["by_family"]["openai"]["rows"] == 2 and tally["by_family"]["openai"]["waiting"] == 1
    assert tally["by_family"]["deepseek"]["single"] == {"accept": 0, "back": 1, "discard": 0}
