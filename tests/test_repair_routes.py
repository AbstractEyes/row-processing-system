"""The repair route map: `repair.routes` sends the rows of one route to another route to be written again.

A route that is not in the map repairs its own rows, as the repair step always did. A mapped route's rows are
written again by the route the map names: that route's model, prompt profile and settings, its `repair` cap.
"""

import pytest

from rps.config import Config, ConfigError, RepairConfig

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
