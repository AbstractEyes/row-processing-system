"""The repair route map: `repair.routes` sends the rows of one route to another route to be written again.

A route that is not in the map repairs its own rows, as the repair step always did. A mapped route's rows are
written again by the route the map names: that route's model, prompt profile and settings, its `repair` cap.
"""

import json
from collections import Counter

import pytest

from rps import ids, layout
from rps.assess import Assessment
from rps.config import Config, ConfigError, RepairConfig
from rps.events import replay
from rps.ledger import PriceTable
from rps.prompting import REPAIR_INTRO, WriterRules
from rps.providers import Acceptance, FakeOutcome, FakeWriter, SimulatedCrash
from rps.state import State

from harness import World, task_spec
from helpers import Builder
from repair_helpers import CAPS, FIX_REASON, THREE, gated_writer, repair_route, sends_back, writes_and_rewrites
from review_helpers import crash_after, crash_on, outcome, review_config

ROUTE = {
    "provider": "fake",
    "model": "fake-model",
    "kind": "batch",
    "budget_usd": 10,
    "purpose_caps": {"test": 1, "pricing": 2, "probe": 0.5, "rebound": 3, "production": 4, "repair": 1},
}
WITHOUT_REPAIR_CAP = {**ROUTE, "purpose_caps": {"production": 4}}


def make(routes=None, *, mapping=None, step=True, **repair):
    """A configuration data dict: routes `r1`, `r2` and `r3` (all on, each with a repair cap) unless `routes` says
    otherwise, `fix_to_hand` on, and a `repair` section with the step on (or off) and the map `mapping`, if any."""
    section = {"enabled": step, **repair}
    if mapping is not None:
        section["routes"] = mapping
    return {
        "routes": routes if routes is not None else {"r1": dict(ROUTE), "r2": dict(ROUTE), "r3": dict(ROUTE)},
        "sources": {"photos": {"image_size": 512, "license": "CC BY-NC"}},
        "review": {"policy": {"fix_to_hand": True}},
        "repair": section,
    }


def map_notes(config):
    return [note for note in config.problems() if note.startswith("repair.routes")]


# ------------------------------------------------------------------ the setting


def test_the_map_is_empty_unless_the_configuration_names_one_and_a_route_then_repairs_its_own_rows():
    config = Config.from_dict(make())
    assert config.repair.routes == {} and config.repair == RepairConfig(enabled=True)
    assert RepairConfig().routes == {} and Config.from_dict(make(step=False)).repair.routes == {}
    assert [config.repair.route_for(name) for name in ("r1", "r2", "r3")] == ["r1", "r2", "r3"]
    assert map_notes(config) == []


def test_a_configuration_with_no_map_reads_and_hashes_as_it_did_before_the_map_existed():
    config = Config.from_dict(make(min_rows=5))
    assert config.to_dict()["repair"] == {"enabled": True, "min_rows": 5}  # no empty `routes` in it
    assert Config.from_dict(make(min_rows=5, mapping={})).digest() == config.digest()
    assert Config.from_dict(config.to_dict()) == config and Config.from_dict(config.to_dict()).digest() == config.digest()


def test_the_map_is_read_and_written_back():
    config = Config.from_dict(make(mapping={"r1": "r2", "r3": "r2"}))
    assert config.repair.routes == {"r1": "r2", "r3": "r2"}
    assert [config.repair.route_for(name) for name in ("r1", "r2", "r3")] == ["r2", "r2", "r2"]
    assert config.to_dict()["repair"] == {"enabled": True, "min_rows": 20, "routes": {"r1": "r2", "r3": "r2"}}
    assert Config.from_dict(config.to_dict()) == config
    assert Config.from_dict(config.to_dict()).digest() == config.digest()
    assert config.digest() != Config.from_dict(make()).digest()  # a changed map is a changed configuration


def test_a_map_is_written_back_in_name_order_whatever_order_it_was_given_in():
    one = Config.from_dict(make(mapping={"r3": "r2", "r1": "r2"}))
    other = Config.from_dict(make(mapping={"r1": "r2", "r3": "r2"}))
    assert list(one.to_dict()["repair"]["routes"]) == ["r1", "r3"] and one.digest() == other.digest()


def test_several_routes_may_be_repaired_by_the_same_route():
    config = Config.from_dict(make(mapping={"r1": "r3", "r2": "r3"}))
    assert config.repair.route_for("r1") == config.repair.route_for("r2") == "r3" and config.repair.route_for("r3") == "r3"


def test_a_map_is_read_with_the_step_off_and_does_nothing_until_it_is_on():
    config = Config.from_dict(make(mapping={"r1": "r2"}, step=False))
    assert config.repair.enabled is False and config.repair.routes == {"r1": "r2"}


# ------------------------------------------------------------------ what is refused


@pytest.mark.parametrize(
    "mapping, message",
    [
        pytest.param({"r9": "r2"}, r"repair\.routes\.r9: unknown writing route 'r9'", id="a writing route that is not configured"),
        pytest.param({"r1": "r9"}, r"repair\.routes\.r1: unknown repairing route 'r9'", id="a repairing route that is not configured"),
        pytest.param({"r1": "R2"}, r"repair\.routes\.r1: unknown repairing route 'R2'", id="route names are exact"),
    ],
)
def test_both_names_of_the_map_must_be_configured_routes(mapping, message):
    with pytest.raises(ConfigError, match=message):
        Config.from_dict(make(mapping=mapping))


def test_the_error_for_an_unknown_route_lists_the_routes_there_are():
    with pytest.raises(ConfigError, match=r"use one of \['r1', 'r2', 'r3'\]"):
        Config.from_dict(make(mapping={"r1": "r9"}))


def test_the_step_being_off_does_not_excuse_a_map_that_names_no_route():
    with pytest.raises(ConfigError, match="unknown repairing route"):
        Config.from_dict(make(mapping={"r1": "r9"}, step=False))


@pytest.mark.parametrize("name", ["r1", "r2"])
def test_a_route_cannot_be_mapped_to_itself(name):
    with pytest.raises(ConfigError, match=rf"repair\.routes\.{name}: a route cannot be repaired by itself"):
        Config.from_dict(make(mapping={name: name}))


@pytest.mark.parametrize(
    "mapping",
    [
        pytest.param({"r1": "r2", "r2": "r3"}, id="a chain"),
        pytest.param({"r1": "r2", "r2": "r1"}, id="two routes mapped to each other"),
        pytest.param({"r3": "r1", "r1": "r2"}, id="a chain written in another order"),
    ],
)
def test_a_route_that_rows_are_mapped_to_is_not_itself_mapped_so_there_are_no_chains(mapping):
    message = r"repair\.routes\.r\d: route 'r\d' is itself in the map, repaired by 'r\d'; a row is repaired by one route"
    with pytest.raises(ConfigError, match=message):
        Config.from_dict(make(mapping=mapping))


def test_a_self_map_is_reported_as_one_and_not_as_a_chain():
    with pytest.raises(ConfigError, match="cannot be repaired by itself") as caught:
        Config.from_dict(make(mapping={"r1": "r1", "r2": "r1"}))
    assert "chain" not in str(caught.value)


@pytest.mark.parametrize(
    "mapping, message",
    [
        pytest.param([], r"repair\.routes: expected an object", id="a list"),
        pytest.param("r2", r"repair\.routes: expected an object", id="text"),
        pytest.param(None, r"repair\.routes: expected an object", id="null"),
        pytest.param({"r1": ["r2"]}, r"repair\.routes\.r1: expected a non-empty string", id="a list for a route"),
        pytest.param({"r1": 2}, r"repair\.routes\.r1: expected a non-empty string, got 2", id="a number for a route"),
        pytest.param({"r1": ""}, r"repair\.routes\.r1: expected a non-empty string", id="an empty name"),
        pytest.param({"r1": None}, r"repair\.routes\.r1: expected a non-empty string", id="null for a route"),
        pytest.param({"r1": " "}, r"repair\.routes\.r1: expected a non-empty string", id="a blank name"),
        pytest.param({5: "r1"}, r"repair\.routes: expected route names as keys", id="a key that is not text"),
    ],
)
def test_a_map_that_is_not_an_object_of_route_names_is_refused(mapping, message):
    data = make()
    data["repair"]["routes"] = mapping
    with pytest.raises(ConfigError, match=message):
        Config.from_dict(data)


def test_the_map_is_a_key_of_the_repair_section_and_of_nothing_else():
    with pytest.raises(ConfigError, match=r"repair: unknown key\(s\) \['route'\]"):
        Config.from_dict(make(route={"r1": "r2"}))
    data = make()
    data["review"]["routes"] = {"r1": "r2"}
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict(data)
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({**make(), "repair_routes": {"r1": "r2"}})


def test_the_section_checks_a_self_map_and_a_chain_by_itself_and_leaves_the_route_names_to_the_configuration():
    assert RepairConfig.from_dict({"routes": {"x": "y"}}).routes == {"x": "y"}  # it cannot know the routes
    with pytest.raises(ConfigError, match="itself"):
        RepairConfig.from_dict({"routes": {"x": "x"}})
    with pytest.raises(ConfigError, match="no chains"):
        RepairConfig.from_dict({"routes": {"x": "y", "y": "z"}})
    with pytest.raises(ConfigError, match="unknown repairing route 'y'"):
        RepairConfig.from_dict({"routes": {"x": "y"}}).check_routes(["x"])
    RepairConfig.from_dict({"routes": {"x": "y"}}).check_routes({"x", "y"})


# ------------------------------------------------------------------ what the configuration says before a run


def test_a_map_to_a_route_that_is_on_and_has_a_repair_cap_is_not_noted():
    config = Config.from_dict(make(mapping={"r1": "r2"}))
    assert map_notes(config) == [] and [note for note in config.problems() if "repair" in note] == []


def test_a_map_to_a_route_that_is_switched_off_is_noted_because_its_rows_would_wait_for_the_owner():
    routes = {"r1": dict(ROUTE), "r2": {**ROUTE, "enabled": False}}
    config = Config.from_dict(make(routes=routes, mapping={"r1": "r2"}))
    assert map_notes(config) == [
        "repair.routes: route r1 is repaired by route r2, which is switched off, so the rows of r1 are never sent back and wait for the owner"
    ]


def test_a_map_to_a_route_with_no_repair_cap_is_noted_besides_the_note_every_route_without_one_gets():
    routes = {"r1": dict(ROUTE), "r2": dict(WITHOUT_REPAIR_CAP)}
    config = Config.from_dict(make(routes=routes, mapping={"r1": "r2"}))
    assert map_notes(config) == [
        "repair.routes: route r1 is repaired by route r2, which has no cap for purpose 'repair', so the rows of r1 are never sent back and wait for the owner"
    ]
    assert "route r2: no cap for purpose 'repair' (calls with that purpose are refused)" in config.problems()


def test_a_cap_of_nothing_counts_as_no_cap():
    routes = {"r1": dict(ROUTE), "r2": {**ROUTE, "purpose_caps": {**ROUTE["purpose_caps"], "repair": 0}}}
    config = Config.from_dict(make(routes=routes, mapping={"r1": "r2"}))
    assert len(map_notes(config)) == 1 and "no cap for purpose 'repair'" in map_notes(config)[0]


def test_a_target_that_is_switched_off_and_has_no_cap_gets_both_notes():
    routes = {"r1": dict(ROUTE), "r2": {**WITHOUT_REPAIR_CAP, "enabled": False}}
    notes = map_notes(Config.from_dict(make(routes=routes, mapping={"r1": "r2"})))
    assert len(notes) == 2 and "switched off" in notes[0] and "no cap for purpose 'repair'" in notes[1]


def test_every_mapping_that_cannot_work_is_noted_in_name_order():
    routes = {"r1": dict(ROUTE), "r2": dict(ROUTE), "r3": dict(WITHOUT_REPAIR_CAP)}
    config = Config.from_dict(make(routes=routes, mapping={"r2": "r3", "r1": "r3"}))
    assert [note.split(" is repaired")[0] for note in map_notes(config)] == ["repair.routes: route r1", "repair.routes: route r2"]


def test_a_mapped_route_is_not_noted_for_lacking_a_repair_cap_because_the_route_it_is_mapped_to_makes_the_calls():
    routes = {"r1": dict(WITHOUT_REPAIR_CAP), "r2": dict(ROUTE)}
    note = "route r1: no cap for purpose 'repair' (calls with that purpose are refused)"
    assert note not in Config.from_dict(make(routes=routes, mapping={"r1": "r2"})).problems()
    assert note in Config.from_dict(make(routes=routes)).problems()  # without the map the route repairs its own rows
    # the other purposes of a mapped route are still noted
    bare = {"r1": {**ROUTE, "purpose_caps": {}}, "r2": dict(ROUTE)}
    problems = Config.from_dict(make(routes=bare, mapping={"r1": "r2"})).problems()
    assert "route r1: no cap for purpose 'production' (calls with that purpose are refused)" in problems
    assert note not in problems


def test_a_mapped_route_that_is_switched_off_is_noted_as_any_switched_off_route_is_and_the_map_is_not_noted_for_it():
    routes = {"r1": {**ROUTE, "enabled": False}, "r2": dict(ROUTE)}
    config = Config.from_dict(make(routes=routes, mapping={"r1": "r2"}))
    assert 'route r1: disabled (set "enabled": true to use it)' in config.problems()
    assert map_notes(config) == []  # the route that repairs its rows is fine: that is what the map is for


def test_nothing_is_said_about_a_map_while_the_step_is_off():
    routes = {"r1": dict(ROUTE), "r2": {**WITHOUT_REPAIR_CAP, "enabled": False}}
    config = Config.from_dict(make(routes=routes, mapping={"r1": "r2"}, step=False))
    assert [note for note in config.problems() if "repair" in note] == []


# ------------------------------------------------------------------ the scenario of the controller tests
#
# Route A wrote the rows, and the reviewers sent some of them back with a fix. Then A is switched off for poor
# quality (its rows are still in review), the repair step is on, and the map says that route B repairs A's rows.
# B is another model of another provider, with its own effort, token limit and price.

A, B, C = "route-a", "route-b", "route-c"
B_MODEL = "fake-model-b"
B_PROVIDER = "openai"
LINES = ("001", "002", "003", "004")
SWITCHED_OFF_REASON = "the repair was not made, the step or the route's repair cap is switched off: the row waits for the owner"
CHECKER_REASON = "the repair failed the checker: the row waits for the owner"
CARD = {"sync": {"input": 1.0, "output": 2.0, "cache_read": 0.1}}


def prices():
    """The price table of these tests: B's model costs twice what A's does."""
    dear = {"sync": {"input": 2.0, "output": 4.0, "cache_read": 0.2}}
    return PriceTable.from_dict({"date": "2026-11-07", "models": {"fake-model": CARD, B_MODEL: dear}})


def route_a(**extra):
    return repair_route(**extra)


def route_b(**extra):
    """The route that repairs: another model, provider, effort and token limit than route A."""
    return repair_route(**{"model": B_MODEL, "provider": B_PROVIDER, "effort": "high", "max_output_tokens": 777, **extra})


def config(*, a=None, b=None, c=None, mapping=None, step=True, min_rows=1, reviewers=THREE, **extra):
    """The configuration of a run: routes A and B (and C when it is given), the repair step on or off, `fix_to_hand`
    on, and the map `mapping` when there is one."""
    section = {"enabled": step, "min_rows": min_rows}
    if mapping is not None:
        section["routes"] = mapping
    routes = {A: a if a is not None else route_a(), B: b if b is not None else route_b()}
    if c is not None:
        routes[C] = c
    return review_config(
        reviewers, writer_family="writer-family", policy={"fix_to_hand": True}, routes=routes, repair=section, **extra
    )


def switched_off(**options):
    """The configuration of the second stage: route A is switched off, and the map sends its rows to route B."""
    return config(a=route_a(enabled=False), mapping={A: B}, **options)


def rows_waiting(tmp_path, *, fix=("001", "003"), lines=LINES, tasks=None, config_options=None, controller_options=None, **world_options):
    """The first stage: route A writes `lines` (or the `tasks` given) with the repair step off, and the reviewers send
    the rows of the lines in `fix` back with a fix verdict: they wait in the hand queue for the owner.

    `config_options` go to `config`, `controller_options` to the controller, and the rest to the `World`.
    Returns the world, which the second stage goes on in, and the controller of this stage."""
    world = World(
        tmp_path, config=config(step=False, **(config_options or {})), behavior=writes_and_rewrites, prices=prices(), **world_options
    )
    controller = world.controller(**(controller_options or {}))
    for spec in tasks or [task_spec(lines, route=A)]:
        controller.add_task(spec)
    world.drive(controller, sends_back(*fix))
    assert sorted(row.line for row in controller.state.hand_queue()) == sorted(fix)
    return world, controller


def through_the_map(
    tmp_path, *, fix=("001", "003"), lines=LINES, tasks=None, second=None, config_options=None, controller_options=None, **world_options
):
    """Both stages: the rows wait in the hand queue, then route A is switched off, the step is on, the map names B
    (the configuration `second`, when it is given) and a new run drives everything to its end.
    Returns the world and the controller of the second stage."""
    world, _first = rows_waiting(
        tmp_path, fix=fix, lines=lines, tasks=tasks, config_options=config_options, controller_options=controller_options, **world_options
    )
    world.config = second or switched_off()
    controller = world.controller(**(controller_options or {}))
    world.drive(controller, sends_back(*fix))
    return world, controller


def originals(controller):
    """The rows the first calls wrote, by spec line."""
    return {row.line: row for row in controller.state.rows.values() if row.repair_of is None}


def repaired(controller):
    """The rows repair calls wrote, by the id of the row they repair."""
    return {row.repair_of: row for row in controller.state.rows.values() if row.repair_of is not None}


def payloads(world, kind="call_prepared", **match):
    return [e.payload for e in world.events_of(kind) if all(e.payload.get(k) == v for k, v in match.items())]


def requests_made(world, *, repair):
    """The payloads of the requests the fake provider was handed, for the repair calls or for the other calls."""
    sent = [p for batch in world.provider.batches.values() for _call, _custom, p in batch["requests"]]
    return [p for p in sent if bool(p["repair_of"]) is repair]


def hand_queue_file(world):
    if not world.state_store.exists(layout.HAND_QUEUE_PATH):
        return []
    return [json.loads(line) for line in world.state_store.read_text(layout.HAND_QUEUE_PATH).splitlines()]


def reviewers_of(controller, row_id):
    return [v["reviewer_id"] for v in controller.state.rows[row_id].verdicts]


# ------------------------------------------------------------------ the rows of a switched-off route


def test_the_rows_of_a_route_that_is_switched_off_are_sent_to_the_route_the_map_names_and_it_writes_them_again(tmp_path):
    world, controller = through_the_map(tmp_path)
    state, first = controller.state, originals(controller)
    task = next(iter(state.tasks))
    assert controller.route_state(A)[0] == "closed" and controller.route_state(B)[0] == "open"
    sent = world.events_of("repair_requested")
    assert [e.payload for e in sent] == [
        {"row_id": first[line].row_id, "call_id": ids.call_id(task, line, 2), "route": B} for line in ("001", "003")
    ]
    assert list(sent[0].payload) == ["row_id", "call_id", "route"]  # the shape of the event is as it was: only the route is B's
    again = repaired(controller)
    assert set(again) == {first["001"].row_id, first["003"].row_id}
    for line in ("001", "003"):
        new = again[first[line].row_id]
        assert first[line].status == "repaired" and first[line].route == A and first[line].repair_of is None
        assert new.row_id == ids.row_id(ids.call_id(task, line, 2), 0) and new.status == "accepted" and new.line == line
        assert new.route == B and new.accepted_by == "reviewers"
    assert [first[line].status for line in ("002", "004")] == ["accepted", "accepted"]
    assert state.counts()["rows"] == {"repaired": 2, "accepted": 4} and state.hand_queue() == []
    assert any("sent 2 row(s) to route-b to be written again" in line and "(including rows written by route-a)" in line for line in world.printed)
    assert controller.is_idle() and state.snapshot() == State.from_events(world.events()).snapshot()


def test_the_repair_calls_are_route_bs_charged_to_its_repair_cap_at_its_price_and_never_to_route_a(tmp_path):
    world, controller = through_the_map(tmp_path)
    state, first = controller.state, originals(controller)
    prepared = payloads(world, purpose="repair")
    assert [(p["route"], p["attempt"], p["repair_of"], p["retry_of"]) for p in prepared] == [
        (B, 2, first[line].row_id, None) for line in ("001", "003")
    ]
    assert {p["call_id"] for p in prepared} == {e.payload["call_id"] for e in world.events_of("repair_requested")}
    assert [p["route"] for p in payloads(world, purpose="production")] == [A] * 4  # the first calls were route A's
    assert state.spend(B, "repair").calls == 2 and state.spend(A, "repair").calls == 0
    assert state.spend(B, "production").calls == 0 and state.spend(B, "rebound").calls == 0
    assert state.spend(A, "production").calls == 4 and state.spend(A, "rebound").calls == 0
    assert state.spend(B, "repair").actual == pytest.approx(2 * 0.0014) and state.spend(A).actual == pytest.approx(4 * 0.0007)  # B's price
    summary = json.loads(world.ship_store.read_text("ledger/summary.json"))["routes"]
    assert summary[B]["purposes"]["repair"]["calls"] == 2 and summary[A]["purposes"]["repair"]["calls"] == 0
    assert summary[B]["purposes"]["repair"]["spent_usd"] == pytest.approx(0.0028)
    assert {state.calls[p["call_id"]].route for p in prepared} == {B}


def test_the_ledger_has_a_record_for_the_task_on_route_b_with_its_model_and_effort(tmp_path):
    world, controller = through_the_map(tmp_path)
    task = next(iter(controller.state.tasks))
    records = {entry["route"]: entry for entry in (json.loads(l) for l in world.ship_store.read_text("ledger/tasks.jsonl").splitlines())}
    assert set(records) == {A, B} and {entry["task_id"] for entry in records.values()} == {task}
    assert (records[A]["model"], records[A]["effort"]) == ("fake-model", "low")
    assert (records[B]["model"], records[B]["effort"]) == (B_MODEL, "high")  # not the effort of the task, which is A's
    assert set(records[B]["usd_by_purpose"]) == {"repair"} and set(records[A]["usd_by_purpose"]) == {"production"}
    assert records[B]["calls"] == 2 and records[B]["rows_out"] == 2 and records[B]["rows_accepted"] == 2
    assert records[A]["rows_out"] == 4 and records[A]["rows_accepted"] == 2  # the two that were sent back are `repaired`, not accepted


def test_the_writer_is_given_route_bs_model_and_limit_with_the_spec_line_and_the_notes_of_the_first_call(tmp_path):
    world, controller = through_the_map(tmp_path, fix=("001",), lines=("001", "002"))
    (new,) = repaired(controller).values()
    first = controller.state.rows[new.repair_of]
    (request,) = requests_made(world, repair=True)
    (note,) = [v["notes"] for v in first.verdicts]
    assert request["model"] == B_MODEL and request["max_output_tokens"] == 777 and request["attempt"] == 2
    assert request["repair_of"] == first.row_id and request["spec"] == "spec for 001"  # the line is the task's own
    assert request["feedback"][:-1] == [note] and json.loads(request["feedback"][-1]) == {"id": "001-1", "thoughts": ["a thought about spec for 001"]}
    assert request["task_text"].startswith("spec for 001\n\nAn earlier row for this item was sent back by a reviewer.")
    # the first calls were route A's
    assert {p["model"] for p in requests_made(world, repair=False)} == {"fake-model"}
    assert {p["max_output_tokens"] for p in requests_made(world, repair=False)} == {4096}
    (prepared,) = payloads(world, purpose="repair")
    assert prepared["feedback"] == request["feedback"] and prepared["max_output_tokens"] == 777


class Recording(FakeWriter):
    """A fake writer that keeps the calls it was asked to prepare."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def prepare(self, calls):
        self.calls.extend(calls)
        return super().prepare(calls)


def test_the_call_is_built_from_route_bs_provider_model_kind_effort_and_limit(tmp_path):
    world, controller = through_the_map(tmp_path, fix=("001",), lines=("001", "002"), writer_class=Recording)
    assert controller.writers[A].calls == []  # route A is switched off: no call was built for it in this run
    (call,) = controller.writers[B].calls
    first = controller.state.rows[call.repair_of]
    assert call.route == B and call.provider == B_PROVIDER and call.model == B_MODEL and call.kind == "batch"
    assert call.effort == "high" and call.max_output_tokens == 777 and call.purpose == "repair" and call.attempt == 2
    assert call.line == "001" and call.spec == "spec for 001" and call.task_id == first.task_id and call.repair_of == first.row_id
    assert call.call_id == ids.call_id(first.task_id, "001", 2)


def test_a_row_written_by_the_repair_carries_route_bs_writer_label_and_a_link_to_the_row_it_replaces(tmp_path):
    world, controller = through_the_map(tmp_path)
    first, again = originals(controller), repaired(controller)
    task = next(iter(controller.state.tasks))
    for line in ("001", "003"):
        new = again[first[line].row_id]
        label, old = new.record["writer"], first[line].record["writer"]
        assert (label["provider"], label["model"], label["effort"], label["route"]) == (B_PROVIDER, B_MODEL, "high", "batch")
        assert (old["provider"], old["model"], old["effort"]) == ("fake", "fake-model", "low")
        assert label["task_id"] == old["task_id"] == task and new.repair_of == first[line].row_id
        assert new.record["source_label"] == first[line].record["source_label"]  # the same item, written again
    shipped = {row["row_id"]: row for row in world.shipped_rows()}
    assert set(shipped) == {first["002"].row_id, first["004"].row_id} | {new.row_id for new in again.values()}
    assert not any("repair" in key for row in shipped.values() for key in row)  # the link is in the events and the state only
    assert {shipped[new.row_id]["writer"]["model"] for new in again.values()} == {B_MODEL}
    # the rows ship in a file of their own, under the route that wrote them
    assert sorted(world.ship_store.list("accepted/")) == sorted([layout.accepted_path(A, task), layout.accepted_path(B, task)])


def test_the_first_review_of_the_new_row_goes_outside_route_bs_family_and_to_a_reviewer_that_did_not_review_the_original(tmp_path):
    reviewers = [{"id": "rev-a", "family": "alpha"}, {"id": "rev-b", "family": "beta"}, {"id": "rev-o", "family": B_PROVIDER}]
    options = {"reviewers": reviewers}
    fix = ("001", "002", "003")
    world, controller = through_the_map(
        tmp_path, fix=fix, lines=("001", "002", "003", "004", "005", "006"), second=switched_off(**options), config_options=options
    )
    first, again = originals(controller), repaired(controller)
    assert [reviewers_of(controller, first[line].row_id) for line in fix] == [["rev-a"], ["rev-b"], ["rev-o"]]
    new = {line: again[first[line].row_id] for line in fix}
    assert reviewers_of(controller, new["001"].row_id) == ["rev-b"] and reviewers_of(controller, new["002"].row_id) == ["rev-a"]
    for row in new.values():  # never route B's own family (openai), whoever reviewed the original
        assert row.verdicts[0]["family"] != B_PROVIDER and row.assignments[0].round == 1
    assert reviewers_of(controller, new["003"].row_id) in (["rev-a"], ["rev-b"])


def test_the_prompt_profile_of_route_b_writes_the_repair_and_the_row_it_wrote_says_so(tmp_path):
    schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    terse = WriterRules(
        system="terse prompt", schema=schema, version="terse-1", profile="terse",
        task_text=lambda spec, ordinal: f"Item: {spec}\n", retry="Fix these checks:\n{check_messages}\n",
    )
    rules = WriterRules(
        system="plain prompt", schema=schema, version="plain-1", task_text=lambda spec, ordinal: f"Item {spec}",
        profile_loader=lambda name: terse,
    )
    options = {"b": route_b(profile="terse")}
    world, controller = through_the_map(
        tmp_path, fix=("001",), lines=("001", "002"), second=switched_off(**options), config_options=options,
        controller_options={"rules": rules}, writer_class=Recording,
    )
    assert controller.rules_for(B).profile == "terse" and controller.rules_for(A) is rules
    (call,) = controller.writers[B].calls
    assert call.rules is controller.rules_for(B) and call.rules.system == "terse prompt" and call.rules.version == "terse-1"
    # the same spec line in route B's wording, then the words of the repair (never the profile's own retry block)
    assert call.task_text.startswith("Item: spec for 001\n\n" + REPAIR_INTRO) and "Fix these checks" not in call.task_text
    (prepared,) = payloads(world, purpose="repair")
    assert (prepared["route"], prepared["rules_version"], prepared["rules_profile"]) == (B, "terse-1", "terse")
    first_call = payloads(world, purpose="production")[0]
    assert (first_call["route"], first_call["rules_version"], first_call["rules_profile"]) == (A, "plain-1", "default")
    first, (new,) = originals(controller)["001"], repaired(controller).values()
    assert first.record["writer"]["rules_profile"] == "default" and first.record["writer"]["rules_version"] == "plain-1"
    assert new.record["writer"]["rules_profile"] == "terse" and new.record["writer"]["rules_version"] == "terse-1"
    packet_items = [item for e in world.events_of("packet_published") for item in e.payload["items"] if item["row_id"] == new.row_id]
    assert [(i["rules_profile"], i["rules_version"]) for i in packet_items] == [("terse", "terse-1")]


def test_a_picture_goes_only_where_the_routing_table_allows_it_for_the_repairing_routes_provider(tmp_path):
    # route A's provider may be sent the picture and route B's, which the table does not know, may not
    options = {"b": route_b(provider="mystery")}
    world, controller = through_the_map(
        tmp_path, fix=("001",), tasks=[task_spec(("001", "002"), route=A, image="p.jpg")], second=switched_off(**options),
        config_options=options,
    )
    first = originals(controller)
    (prepared,) = payloads(world, purpose="repair")
    assert prepared["route"] == B and prepared["est_usd"] == 0.0 and prepared["repair_of"] == first["001"].row_id
    assert first["001"].status == "hand" and first["001"].hand["reason"] == "the repair call failed (bad_request): the row waits for the owner"
    assert requests_made(world, repair=True) == []  # nothing was handed to the provider: the picture stayed where it was
    assert first["002"].status == "accepted" and controller.state.spend(B, "repair").actual == 0 and controller.is_idle()


def test_the_rows_stay_waiting_for_the_owner_without_the_map_as_before(tmp_path):
    world, first = rows_waiting(tmp_path)
    waiting = sorted(row.row_id for row in first.state.hand_queue())
    world.config = config(a=route_a(enabled=False))  # route A is switched off, the step is on, and nothing names route B
    second = world.controller()
    for _ in range(4):
        second.step()
    assert world.events_of("repair_requested") == [] and payloads(world, purpose="repair") == []
    assert sorted(row.row_id for row in second.state.hand_queue()) == waiting
    assert {row.hand["reason"] for row in second.state.hand_queue()} == {FIX_REASON}
    assert {entry["row_id"] for entry in hand_queue_file(world)} == set(waiting)
    assert second.state.spend(B).calls == 0 and second.state.spend(A, "repair").calls == 0 and second.is_idle()
    assert not any("repair.routes" in line for line in world.printed)  # nothing is said about a map there is not


def test_a_map_sends_the_rows_of_a_route_that_is_still_on_as_well_and_the_route_needs_no_repair_cap_of_its_own(tmp_path):
    bare = {key: value for key, value in CAPS.items() if key != "repair"}
    world, _first = rows_waiting(tmp_path)
    world.config = config(a=route_a(caps=bare), mapping={A: B})  # route A is on, and has no cap for repair
    controller = world.controller()
    world.drive(controller, sends_back("001", "003"))
    assert [e.payload["route"] for e in world.events_of("repair_requested")] == [B, B]
    assert controller.state.counts()["rows"] == {"repaired": 2, "accepted": 4} and controller.state.spend(A, "repair").calls == 0
    assert controller.state.spend(B, "repair").calls == 2
    assert not any("route route-a: no cap for purpose 'repair'" in line for line in world.printed)  # route B makes the calls


def test_a_route_that_is_not_in_the_map_repairs_its_own_rows_next_to_a_mapped_one(tmp_path):
    tasks = [task_spec(("001", "002"), route=A), task_spec(("011", "012"), batch="3", route=C)]
    world = World(tmp_path, config=config(step=False, c=repair_route()), behavior=writes_and_rewrites, prices=prices())
    first = world.controller()
    for spec in tasks:
        first.add_task(spec)
    world.drive(first, sends_back("001", "011", "012"))
    assert len(first.state.hand_queue()) == 3
    world.config = config(a=route_a(enabled=False), c=repair_route(), mapping={A: B})
    controller = world.controller()
    world.drive(controller, sends_back("001", "011", "012"))
    state = controller.state
    sent = {state.rows[e.payload["row_id"]].line: e.payload["route"] for e in world.events_of("repair_requested")}
    assert sent == {"001": B, "011": C, "012": C}  # the mapped route's row goes to B, the other route repairs its own
    assert {p["route"] for p in payloads(world, purpose="repair")} == {B, C}
    assert state.spend(B, "repair").calls == 1 and state.spend(C, "repair").calls == 2 and state.spend(A, "repair").calls == 0
    for new in repaired(controller).values():
        assert new.route == sent[state.rows[new.repair_of].line] and new.status == "accepted"
    assert state.counts()["rows"] == {"repaired": 3, "accepted": 4}  # the kept row of line 002 and the three written again


# ------------------------------------------------------------------ a map that cannot work


@pytest.mark.parametrize(
    "target, why",
    [
        pytest.param(route_b(caps={key: value for key, value in CAPS.items() if key != "repair"}), "has no cap for purpose 'repair'", id="no repair cap"),
        pytest.param(route_b(caps={**CAPS, "repair": 0}), "has no cap for purpose 'repair'", id="a repair cap of nothing"),
        pytest.param(route_b(enabled=False), "is switched off", id="switched off"),
    ],
)
def test_a_map_to_a_route_with_no_repair_cap_or_one_that_is_switched_off_is_noted_and_nothing_is_repaired(tmp_path, target, why):
    world, _first = rows_waiting(tmp_path)
    waiting = sorted(row.row_id for row in _first.state.hand_queue())
    world.config = config(a=route_a(enabled=False), b=target, mapping={A: B})
    controller = world.controller()
    for _ in range(4):
        controller.step()
    note = f"repair.routes: route route-a is repaired by route route-b, which {why}, so the rows of route-a are never sent back and wait for the owner"
    assert note in controller.config.problems() and f"[rps] note: {note}" in world.printed  # said when the run starts
    assert world.events_of("repair_requested") == [] and payloads(world, purpose="repair") == []
    assert sorted(row.row_id for row in controller.state.hand_queue()) == waiting
    assert {entry["row_id"] for entry in hand_queue_file(world)} == set(waiting)
    assert controller.state.counts()["refused_calls"] == 0 and controller.is_idle()


def test_a_repairing_route_that_cannot_take_calls_for_this_run_is_waited_for_and_the_rows_are_not_sent_back_to_it(tmp_path, monkeypatch):
    class OnlyOpen(FakeWriter):
        """A fake writer for a route that takes calls only while its name is in `open_routes`."""

        open_routes = {A, B}

        def accepting(self):
            return Acceptance(True) if self.route_name in type(self).open_routes else Acceptance(False, "no key for this route")

    world, _first = rows_waiting(tmp_path, writer_class=OnlyOpen)
    world.config = switched_off()
    monkeypatch.setattr(OnlyOpen, "open_routes", {A})
    controller = world.controller()
    for _ in range(3):
        controller.step()
    assert world.events_of("repair_requested") == [] and len(controller.state.hand_queue()) == 2  # B is closed: nothing is sent to it
    monkeypatch.setattr(OnlyOpen, "open_routes", {A, B})  # the route is open again
    world.drive(controller, sends_back("001", "003"))
    assert [e.payload["route"] for e in world.events_of("repair_requested")] == [B, B]
    assert controller.state.counts()["rows"] == {"repaired": 2, "accepted": 4}


# ------------------------------------------------------------------ a repair that comes to nothing


@pytest.mark.parametrize(
    "outcome_, reason, failed",
    [
        pytest.param(
            FakeOutcome(error="declined", retryable=False, kind="refused"),
            "the repair call failed (refused): the row waits for the owner",
            1,
            id="refused",
        ),
        pytest.param(
            FakeOutcome(error="overloaded", retryable=True),
            "the repair call failed (provider_error): the row waits for the owner",
            2,
            id="always failing",
        ),
        pytest.param(
            FakeOutcome(output=json.dumps({"skip": True, "class": "other", "reason": "cannot be written again", "rows": []})),
            "the repair gave no row, the writer set it aside instead of writing it again: the row waits for the owner",
            0,
            id="set aside",
        ),
    ],
)
def test_a_mapped_repair_that_comes_to_nothing_returns_the_original_to_the_owner_with_the_reason(tmp_path, outcome_, reason, failed):
    behavior = lambda payload: outcome_ if payload["repair_of"] else writes_and_rewrites(payload)  # noqa: E731
    world, _first = rows_waiting(tmp_path, fix=("001",), lines=("001", "002"))
    world.behavior = behavior
    world.config = switched_off()
    controller = world.controller()
    world.drive(controller, sends_back("001"))
    state, first = controller.state, originals(controller)
    assert first["001"].status == "hand" and first["001"].hand["reason"] == reason and first["002"].status == "accepted"
    assert [e.payload["reason"] for e in world.events_of("row_to_hand")] == [FIX_REASON, reason]
    assert [e.payload["route"] for e in world.events_of("repair_requested")] == [B] and state.hand_queue() == [first["001"]]
    assert {p["route"] for p in payloads(world, purpose="repair")} == {B}
    assert state.spend(B, "repair").failed == failed and state.spend(A, "repair").calls == 0
    assert {row["row_id"] for row in world.shipped_rows()} == {first["002"].row_id} and controller.is_idle()
    (entry,) = hand_queue_file(world)
    assert entry["reason"] == reason and entry["route"] == A and entry["row_id"] == first["001"].row_id  # the row route A wrote
    assert entry["repair"]["calls"] and "repair_of" not in entry
    for _ in range(3):
        assert controller.step() == 0  # it is never sent back again
    assert len(world.events_of("repair_requested")) == 1


def test_a_mapped_repair_whose_new_row_fails_the_checker_is_not_written_again_and_the_original_returns_to_the_owner(tmp_path):
    def fails_repaired_rows(row, context):
        if row["thoughts"][0].startswith("repaired"):
            return Assessment(passed=False, errors=("thought 1 is too long",))
        return Assessment(passed=True)

    options = {"max_attempts": 3}  # a line's own attempts would allow a retry; a repair gets none
    world, _first = rows_waiting(tmp_path, fix=("001",), lines=("001", "002"), config_options=options)
    world.config = switched_off(**options)
    controller = world.controller(assess=fails_repaired_rows)
    world.drive(controller, sends_back("001"))
    first, again = originals(controller), repaired(controller)
    new = again[first["001"].row_id]
    assert new.route == B and new.status == "rejected" and new.rejection["retry"] is False
    assert first["001"].status == "hand" and first["001"].hand["reason"] == CHECKER_REASON
    assert len(payloads(world, purpose="repair")) == 1 and controller.state.spend(B, "repair").calls == 1  # nothing more was asked of anyone
    assert controller.state.spend(A, "rebound").calls == 0 and controller.state.spend(B, "rebound").calls == 0


def test_a_mapped_repair_call_that_failed_for_a_passing_reason_is_made_again_on_the_same_route_with_the_same_notes(tmp_path):
    def behavior(payload):
        if payload["repair_of"] and payload["attempt"] == 2:
            return FakeOutcome(error="overloaded", retryable=True)
        return writes_and_rewrites(payload)

    world, _first = rows_waiting(tmp_path, fix=("001",), lines=("001", "002"))
    world.behavior = behavior
    world.config = switched_off()
    controller = world.controller()
    world.drive(controller, sends_back("001"))
    first, second = payloads(world, purpose="repair")
    assert (first["route"], first["attempt"], first["retry_of"]) == (B, 2, None)
    assert (second["route"], second["attempt"], second["retry_of"]) == (B, 3, first["call_id"])
    assert first["feedback"] == second["feedback"] and second["repair_of"] == first["repair_of"]
    state = controller.state
    assert state.spend(B, "repair").calls == 2 and state.spend(B, "rebound").calls == 0 and state.spend(A, "rebound").calls == 0
    assert state.spend(A, "repair").calls == 0 and state.counts()["rows"] == {"repaired": 1, "accepted": 2}
    assert [request["model"] for request in requests_made(world, repair=True)] == [B_MODEL, B_MODEL]


# ------------------------------------------------------------------ spend


def test_a_repair_call_route_bs_cap_cannot_hold_is_refused_though_route_as_repair_cap_has_room_and_a_later_run_writes_it(tmp_path):
    world, _first = rows_waiting(tmp_path)
    world.config = switched_off(b=route_b(caps={**CAPS, "repair": 0.002}))  # a call is held at about $0.0026 on route B
    controller = world.controller()
    world.drive(controller, sends_back("001", "003"))
    refused = world.events_of("budget_refused")
    assert len(refused) == 2 and {e.payload["route"] for e in refused} == {B} and {e.payload["purpose"] for e in refused} == {"repair"}
    assert {e.payload["reason"] for e in refused} == {"purpose_cap"} and refused[0].payload["purpose_cap"] == 0.002
    assert len(world.events_of("repair_requested")) == 2 and payloads(world, purpose="repair") == []
    assert controller.state.counts()["rows"] == {"repairing": 2, "accepted": 2} and controller.state.hand_queue() == []
    assert controller.state.spend(A, "repair").calls == 0 and "2 by the repair cap of route-b ($0.0020)" in controller.format_status()
    world.config = switched_off()  # a later run with room for them writes the rows again, on route B
    later = world.controller()
    world.drive(later, sends_back("001", "003"))
    assert later.state.counts()["rows"] == {"repaired": 2, "accepted": 4} and later.state.spend(B, "repair").calls == 2


# ------------------------------------------------------------------ in batches, by repairing route


def busy_world(tmp_path, *, min_rows, fix, tasks, held=("B01",), packets=1):
    """Route B is kept busy by a call that is held, while the rows of the first tasks (on A, and on B) have been
    reviewed: their verdicts are in the store and the controller has not read them yet. The map is on, route A too.
    Returns (world, controller, held)."""
    held = set(held)
    world = World(
        tmp_path, config=config(mapping={A: B}, min_rows=min_rows), behavior=writes_and_rewrites, prices=prices(),
        writer_class=gated_writer(held), polls_until_ended=0,
    )
    controller = world.controller()
    for spec in tasks:
        controller.add_task(spec)
    for _ in range(10):
        controller.step()
        if len(controller.state.open_packets()) >= packets:
            break
    else:
        raise AssertionError("no packet was published")
    controller.add_task(task_spec(tuple(held), batch="2", route=B))
    controller.step()
    assert controller.state.unfinished_calls(B) == 1  # the held call: route B has other work
    world.run_reviewers(sends_back(*fix))
    return world, controller, held


def test_a_mapped_batch_waits_for_min_rows_while_the_repairing_route_is_busy_though_the_route_that_wrote_the_rows_is_idle(tmp_path):
    world, controller, held = busy_world(tmp_path, min_rows=3, fix=("001", "003"), tasks=[task_spec(LINES, route=A)])
    controller.step()
    assert len(controller.state.hand_queue()) == 2  # two rows are fewer than 3, route B is busy, and route A has nothing to do
    assert controller._route_has_nothing_else_to_do(A) is True and controller._route_has_nothing_else_to_do(B) is False
    assert world.events_of("repair_requested") == [] and payloads(world, purpose="repair") == []
    for _ in range(2):
        controller.step()
    assert world.events_of("repair_requested") == []
    held.clear()  # route B has nothing else to do once its last call is in: the smaller batch goes
    controller.step()
    assert [e.payload["route"] for e in world.events_of("repair_requested")] == [B, B] and controller.state.hand_queue() == []
    assert any("sent 2 row(s) to route-b to be written again" in line for line in world.printed)
    world.drive(controller, sends_back("001", "003"))
    assert len(payloads(world, purpose="repair")) == 2 and controller.state.spend(B, "repair").calls == 2


def test_the_rows_of_the_mapped_route_and_the_rows_of_the_repairing_route_count_together_toward_min_rows_and_go_in_one_batch(tmp_path):
    tasks = [task_spec(("001", "002"), route=A), task_spec(("011", "012"), batch="3", route=B)]
    world, controller, held = busy_world(tmp_path, min_rows=2, fix=("001", "011"), tasks=tasks, held=("H01",), packets=2)
    controller.step()
    sent = world.events_of("repair_requested")  # one row of each route: two candidates for route B, which is busy
    assert sorted(controller.state.rows[e.payload["row_id"]].route for e in sent) == [A, B] and {e.payload["route"] for e in sent} == {B}
    assert controller.state.hand_queue() == []
    controller.step()
    prepared = payloads(world, purpose="repair")
    assert len(prepared) == 2 and {p["route"] for p in prepared} == {B}
    handles = {e.payload["handle_id"] for e in world.events_of("batch_submitted") if set(e.payload["call_ids"]) & {p["call_id"] for p in prepared}}
    assert len(handles) == 1  # both calls go out as one batch
    held.clear()
    world.drive(controller, sends_back("001", "011"))
    assert controller.state.counts()["rows"]["repaired"] == 2 and controller.state.spend(B, "repair").calls == 2


# ------------------------------------------------------------------ the route a row was sent to is the route it is written by


def stopped_after_the_first_request(tmp_path, **options):
    """The second stage is killed right after the first `repair_requested` (route B, for the first row of the batch):
    one row is sent back and its call is not prepared yet, and the other row is still in the hand queue."""
    world, _first = rows_waiting(tmp_path, **options)
    world.config = switched_off()
    crashing = world.controller(on_event=crash_on("repair_requested"))
    with pytest.raises(SimulatedCrash):
        crashing.step()
    (sent,) = world.events_of("repair_requested")
    assert sent.payload["route"] == B and not payloads(world, purpose="repair")
    return world, sent.payload["row_id"]


def test_a_row_sent_to_route_b_is_written_by_route_b_when_the_run_goes_on_whatever_the_map_says_by_then(tmp_path):
    world, requested = stopped_after_the_first_request(tmp_path)
    # the map is gone and route A is on again, with a repair cap of its own: the event said route B, and it is route B's call
    world.config = config(a=route_a())
    controller = world.controller()
    world.drive(controller, sends_back("001", "003"))
    routes = {e.payload["row_id"]: e.payload["route"] for e in world.events_of("repair_requested")}
    other = next(row_id for row_id in routes if row_id != requested)
    assert routes == {requested: B, other: A}  # the other row was still waiting: with no map it goes to its own route
    made = {p["repair_of"]: p["route"] for p in payloads(world, purpose="repair")}
    assert made == {requested: B, other: A}
    assert {p["repair_of"]: p["model"] for p in requests_made(world, repair=True)} == {requested: B_MODEL, other: "fake-model"}
    assert controller.state.spend(B, "repair").calls == 1 and controller.state.spend(A, "repair").calls == 1
    assert controller.state.counts()["rows"] == {"repaired": 2, "accepted": 4}


def test_a_row_sent_to_route_b_is_not_written_by_route_a_when_route_b_is_switched_off_before_its_call_and_goes_to_the_owner(tmp_path):
    world, requested = stopped_after_the_first_request(tmp_path)
    # route B is switched off by now; route A is on and has a repair cap, and still the row is not given to it
    world.config = config(a=route_a(), b=route_b(enabled=False), mapping={A: B})
    controller = world.controller()
    for _ in range(3):
        controller.step()
    row = controller.state.rows[requested]
    assert row.status == "hand" and row.hand["reason"] == SWITCHED_OFF_REASON
    assert payloads(world, purpose="repair") == [] and controller.state.spend(A, "repair").calls == 0
    assert len(world.events_of("repair_requested")) == 1 and len(controller.state.hand_queue()) == 2  # the other row waits for the owner too
    assert controller.is_idle()


def test_a_row_sent_to_route_b_goes_to_the_owner_when_route_b_has_lost_its_repair_cap_and_the_switched_off_route_is_not_asked(tmp_path):
    world, requested = stopped_after_the_first_request(tmp_path)
    bare = {key: value for key, value in CAPS.items() if key != "repair"}
    world.config = switched_off(b=route_b(caps=bare))
    controller = world.controller()
    for _ in range(3):
        controller.step()
    assert controller.state.rows[requested].hand["reason"] == SWITCHED_OFF_REASON
    assert payloads(world, purpose="repair") == [] and len(controller.state.hand_queue()) == 2


def test_the_state_says_which_route_a_row_was_sent_to_and_the_call_that_is_due_goes_there(tmp_path):
    world, requested = stopped_after_the_first_request(tmp_path)
    controller = world.controller()
    controller.start()
    state = controller.state
    assert state.rows[requested].repair["route"] == B and state.rows[requested].route == A
    ((task, line, status),) = state.repairs_needing_calls(2)
    assert (task.route, status.route, status.repair_of) == (A, B, requested) and line.line_id == state.rows[requested].line
    assert controller._call_route(task, status) == B


# ------------------------------------------------------------------ restarts and replays


def mapped_run(tmp_path, *, crash_at=None):
    """The rows of line 001 wait in the hand queue; route A is switched off and mapped to B, and a run repairs them
    (killed after its `crash_at`-th event, if given, and followed by a run that goes on).
    Returns the world, the last controller and the number of events the second stage wrote (when it was not killed)."""
    verdict = sends_back("001")
    world, _first = rows_waiting(tmp_path, fix=("001",), lines=("001", "002"))
    world.config = switched_off()
    before = len(world.events())
    if crash_at is None:
        controller = world.controller()
        world.drive(controller, verdict)
        return world, controller, len(world.events()) - before
    crashing = world.controller(on_event=crash_after(crash_at))
    with pytest.raises(SimulatedCrash):
        world.drive(crashing, verdict)
    controller = world.controller()
    world.drive(controller, verdict)
    return world, controller, None


def test_a_run_killed_after_any_event_of_a_mapped_repair_ends_where_an_uninterrupted_one_does(tmp_path):
    reference, _controller, total = mapped_run(tmp_path / "reference")
    expected = outcome(reference)
    assert total >= 10 and sorted(status for status, _verdicts in expected[0].values()) == ["accepted", "accepted", "repaired"]
    for count in range(1, total):
        world, controller, _ = mapped_run(tmp_path / f"kill-{count}", crash_at=count)
        where = f"a run killed after event {count}"
        assert outcome(world) == expected, f"{where} ended differently"
        sent = Counter(e.payload["row_id"] for e in world.events_of("repair_requested"))
        assert len(sent) == 1 and set(sent.values()) == {1}, where  # the row was sent back once, to route B
        assert {e.payload["route"] for e in world.events_of("repair_requested")} == {B}, where
        made = payloads(world, purpose="repair")
        assert len(made) == 1 and made[0]["route"] == B, where  # one call, on route B
        assert set(Counter(world.provider.submitted_calls).values()) == {1}, where  # no call went to the provider twice
        assert [p["model"] for p in requests_made(world, repair=True)] == [B_MODEL], where
        state = State.from_events(replay(world.state_store).events)
        assert controller.state.snapshot() == state.snapshot(), where
        assert state.bad_events == [] and state.unknown_kinds == {}, where
        (new,) = [row for row in state.rows.values() if row.repair_of is not None]
        assert new.route == B and new.record["writer"]["model"] == B_MODEL, where


def test_the_state_of_a_mapped_repair_is_the_same_when_rebuilt_from_the_events_at_every_stage(tmp_path):
    world, _first = rows_waiting(tmp_path)
    world.config = switched_off()
    world.polls_until_ended = 1
    controller = world.controller()
    verdict = sends_back("001", "003")
    seen = []
    for _ in range(12):
        controller.step()
        world.run_reviewers(verdict)
        rebuilt = State.from_events(replay(world.state_store).events)
        assert controller.state.snapshot() == rebuilt.snapshot()
        seen.append(sorted(row.status for row in rebuilt.rows.values()))
        if controller.is_idle() and not controller.state.open_packets():
            break
    assert seen[-1] == ["accepted"] * 4 + ["repaired"] * 2 and any("repairing" in statuses for statuses in seen)


# ------------------------------------------------------------------ no map: the step as it was


def test_an_empty_map_is_the_run_there_was_without_one_event_for_event(tmp_path):
    def run(path, **extra):
        world = World(path, config=config(**extra), behavior=writes_and_rewrites, prices=prices())
        controller = world.controller()
        controller.add_task(task_spec(LINES, route=A))
        world.drive(controller, sends_back("001", "003"))
        return world

    plain, empty = run(tmp_path / "plain"), run(tmp_path / "empty", mapping={})
    assert [(e.ts, e.actor, e.kind, e.payload) for e in plain.events()] == [(e.ts, e.actor, e.kind, e.payload) for e in empty.events()]
    assert plain.printed == empty.printed and plain.events_of("repair_requested")


def test_without_a_map_a_row_is_sent_back_to_the_route_that_wrote_it_and_the_events_are_what_they_were(tmp_path):
    world = World(tmp_path, config=config(c=repair_route()), behavior=writes_and_rewrites, prices=prices())
    controller = world.controller()
    controller.add_task(task_spec(("001", "002"), route=A))
    controller.add_task(task_spec(("011", "012"), batch="3", route=C))
    world.drive(controller, sends_back("001", "011"))
    sent = {controller.state.rows[e.payload["row_id"]].line: e.payload for e in world.events_of("repair_requested")}
    assert {line: payload["route"] for line, payload in sent.items()} == {"001": A, "011": C}
    assert all(list(payload) == ["row_id", "call_id", "route"] for payload in sent.values())
    for call in payloads(world, purpose="repair"):
        assert set(call) == {
            "call_id", "task_id", "line", "attempt", "route", "purpose", "est_usd", "est_input_tokens", "max_output_tokens",
            "image_size", "feedback", "retry_of", "repair_of",
        }
        assert call["route"] == controller.state.tasks[call["task_id"]].route  # the route of the task, as it was
    for new in repaired(controller).values():
        assert new.route == controller.state.tasks[new.task_id].route
        assert new.record["writer"]["model"] == "fake-model" and new.record["writer"]["effort"] == "low"
    assert not any("including rows written by" in line for line in world.printed)
    assert any("sent 1 row(s) back to route-a to be written again" in line for line in world.printed)


# ------------------------------------------------------------------ the state


NOTE = "thought 1 states a pose that no record gives"


def queued(route=A):
    """Events for a task of two lines on `route` whose first call wrote one row for line 001, which a reviewer sent
    back with a fix verdict: the row waits in the hand queue. Returns (builder, task id, call id, row id)."""
    b = Builder()
    task = b.task(lines={"001": "a cat", "002": "a dog"}, route=route)
    call = b.prepare(task, "001", route=route)
    b.submit([call], route=route)
    (row,) = b.returned(call)
    b.assessed(row, call)
    packet = {"packet_id": "p000000000001", "reviewer_id": "rev-a", "family": "alpha"}
    b.add("packet_published", {**packet, "brief_version": "1", "items": [{"row_id": row, "image": None, "round": 1, "audit": False}]})
    b.add("verdict_received", {**packet, "row_id": row, "verdict": "fix", "reasons": ["needs_work"], "notes": NOTE})
    b.add("row_to_hand", {"row_id": row, "call_id": call, "reason": FIX_REASON, "verdicts": ["rev-a: fix"]})
    return b, task, call, row


def test_the_route_a_row_was_sent_to_is_on_record_and_a_call_that_is_due_says_where_it_goes():
    b, task, _call, row = queued(A)
    b.add("repair_requested", {"row_id": row, "call_id": ids.call_id(task, "001", 2), "route": B})
    state = State.from_events(b.events)
    assert state.rows[row].repair == {"call_id": ids.call_id(task, "001", 2), "route": B, "calls": [], "rows": []}
    assert state.tasks[task].route == A and state.rows[row].route == A
    ((due_task, line, status),) = state.repairs_needing_calls(2)
    assert (due_task.route, line.line_id, status.outcome, status.repair_of, status.route) == (A, "001", "repair", row, B)
    assert [(item[1].line_id, item[2].route) for item in state.lines_needing_calls(2)] == [("001", B), ("002", None)]  # a spec line has none


def test_a_row_sent_back_to_its_own_route_is_the_same_as_it_always_was_and_its_call_is_due_on_that_route():
    b, task, _call, row = queued(A)
    b.add("repair_requested", {"row_id": row, "call_id": ids.call_id(task, "001", 2), "route": A})
    ((due_task, _line, status),) = State.from_events(b.events).repairs_needing_calls(2)
    assert (due_task.route, status.route) == (A, A)


def test_a_repair_call_on_route_b_is_charged_to_b_and_its_rows_are_route_bs():
    b, task, _call, row = queued(A)
    b.add("repair_requested", {"row_id": row, "call_id": ids.call_id(task, "001", 2), "route": B})
    repair_call = b.prepare(task, "001", 2, route=B, purpose="repair", est=0.02, repair_of=row, feedback=[NOTE, "{}"])
    b.submit([repair_call], handle_id="batch-2", route=B)
    state = State.from_events(b.events)
    call = state.calls[repair_call]
    assert (call.route, call.purpose, call.repair_of, call.status) == (B, "repair", row, "submitted")
    assert state.spend(B, "repair").reserved == pytest.approx(0.02) and state.spend(A, "repair").total == 0
    assert state.unfinished_calls(B) == 1 and state.unfinished_calls(A) == 0
    assert state.rows[row].repair["calls"] == [repair_call] and state.repairs_needing_calls(2) == []
    assert [c.call_id for c in state.pending_calls(B)] == [] and state.batches["batch-2"].route == B
    (new,) = b.returned(repair_call, handle_id="batch-2")
    b.assessed(new, repair_call)
    state = State.from_events(b.events)
    assert state.rows[new].route == B and state.rows[new].repair_of == row and state.rows[row].status == "repaired"
    assert state.line_status(task, "001", 2).outcome == "active"  # where the line stands is read from the rows that repaired it


def test_a_repair_call_on_route_b_that_failed_for_a_passing_reason_is_due_again_on_route_b():
    b, task, _call, row = queued(A)
    b.add("repair_requested", {"row_id": row, "call_id": ids.call_id(task, "001", 2), "route": B})
    first = b.prepare(task, "001", 2, route=B, purpose="repair", repair_of=row, feedback=[NOTE, "{}"])
    b.submit([first], handle_id="batch-2", route=B)
    b.failed(first, handle_id="batch-2")
    ((due_task, _line, status),) = State.from_events(b.events).repairs_needing_calls(2)
    assert (due_task.route, status.route, status.next_attempt, status.retry_of) == (A, B, 3, first)
