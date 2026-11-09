"""The `repair` section of the configuration, and the purpose `repair`."""

import pytest

from rps.config import DEFAULT_REPAIR_MIN_ROWS, PURPOSES, REBOUND, REPAIR, Config, ConfigError, RepairConfig
from rps.ledger import Ledger, PriceTable, effective_purpose
from rps.state import State

ROUTE = {
    "provider": "fake",
    "model": "fake-model",
    "kind": "batch",
    "budget_usd": 10,
    "purpose_caps": {"test": 1, "pricing": 2, "probe": 0.5, "rebound": 3, "production": 4},
}


def make(**overrides):
    data = {"routes": {"r1": dict(ROUTE)}, "sources": {"photos": {"image_size": 512, "license": "CC BY-NC"}}}
    data.update(overrides)
    return data


def repair_notes(config):
    return [note for note in config.problems() if "repair" in note]


# ------------------------------------------------------------------ the section


def test_the_step_is_off_and_gathers_twenty_rows_unless_the_configuration_says_otherwise():
    config = Config.from_dict(make())
    assert config.repair == RepairConfig() and config.repair.enabled is False
    assert config.repair.min_rows == 20 == DEFAULT_REPAIR_MIN_ROWS
    assert config.to_dict()["repair"] == {"enabled": False, "min_rows": 20}
    assert Config.from_dict(make(repair={})).repair == RepairConfig()


def test_the_section_is_read_and_written_back():
    config = Config.from_dict(make(repair={"enabled": True, "min_rows": 5}))
    assert config.repair.enabled is True and config.repair.min_rows == 5
    assert config.to_dict()["repair"] == {"enabled": True, "min_rows": 5}
    assert Config.from_dict(config.to_dict()) == config
    only_enabled = Config.from_dict(make(repair={"enabled": True}))
    assert only_enabled.repair.min_rows == DEFAULT_REPAIR_MIN_ROWS
    only_rows = Config.from_dict(make(repair={"min_rows": 3}))
    assert only_rows.repair.enabled is False and only_rows.repair.min_rows == 3
    assert Config.from_dict(make(repair={"min_rows": 7.0})).repair.min_rows == 7  # a whole number written with a point


def test_the_section_changes_the_digest_of_the_configuration():
    assert Config.from_dict(make(repair={"enabled": True})).digest() != Config.from_dict(make()).digest()


@pytest.mark.parametrize(
    "section, message",
    [
        ({"enabled": "yes"}, r"repair\.enabled: expected true or false"),
        ({"enabled": 1}, r"repair\.enabled: expected true or false"),
        ({"enabled": None}, r"repair\.enabled: expected true or false"),
        ({"min_rows": 0}, r"repair\.min_rows: must be at least 1"),
        ({"min_rows": -3}, r"repair\.min_rows: must be at least 1"),
        ({"min_rows": 2.5}, r"repair\.min_rows: expected a whole number"),
        ({"min_rows": "5"}, r"repair\.min_rows: expected a number"),
        ({"min_rows": True}, r"repair\.min_rows: expected a number"),
        ({"min_rows": None}, r"repair\.min_rows: expected a number"),
        ({"batch": 3}, r"repair: unknown key\(s\) \['batch'\]"),
        ({"enabled": True, "rows": 3}, r"repair: unknown key\(s\) \['rows'\]"),
        ([], r"repair: expected an object"),
        ("on", r"repair: expected an object"),
    ],
)
def test_a_section_that_cannot_be_used_is_refused(section, message):
    with pytest.raises(ConfigError, match=message):
        Config.from_dict(make(repair=section))


def test_the_section_is_one_of_the_top_level_keys_and_not_a_key_of_anything_else():
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict(make(review={"repair": {"enabled": True}}))
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict(make(commit={"repair": {"enabled": True}}))
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({"routes": {"r": dict(ROUTE, repair=True)}})


# ------------------------------------------------------------------ the purpose


def test_repair_is_a_purpose_next_to_the_others():
    assert REPAIR == "repair" and REPAIR in PURPOSES and REBOUND in PURPOSES
    assert PURPOSES[:5] == ("test", "pricing", "probe", "rebound", "production")  # the others kept their places


def test_a_route_may_name_a_cap_for_it_and_without_one_the_cap_is_zero():
    config = Config.from_dict(make(routes={"r1": dict(ROUTE, purpose_caps={**ROUTE["purpose_caps"], "repair": 1.5}), "r2": dict(ROUTE)}))
    assert config.routes["r1"].cap_for("repair") == 1.5
    assert config.routes["r1"].to_dict()["purpose_caps"]["repair"] == 1.5
    assert config.routes["r2"].cap_for("repair") == 0.0
    assert Config.from_dict(config.to_dict()) == config
    with pytest.raises(ConfigError, match="unknown purpose"):
        Config.from_dict({"routes": {"r": dict(ROUTE, purpose_caps={"repairing": 1})}})
    with pytest.raises(ConfigError):
        Config.from_dict({"routes": {"r": dict(ROUTE, purpose_caps={"repair": -1})}})


def test_a_repair_call_is_charged_to_repair_whatever_its_attempt_and_never_to_rebound():
    assert effective_purpose("production", 2, repair=True) == "repair"  # attempt 2 would be a rebound
    assert effective_purpose("production", 1, repair=True) == "repair"
    assert effective_purpose("pricing", 5, repair=True) == "repair"
    assert effective_purpose("production", 2) == "rebound" and effective_purpose("production", 1) == "production"
    assert effective_purpose("production", 2, repair=False) == "rebound"
    with pytest.raises(ValueError):
        effective_purpose("fun", 1, repair=True)


def test_the_ledger_lists_the_purpose_with_its_cap():
    config = Config.from_dict(make(routes={"r1": dict(ROUTE, purpose_caps={"repair": 1.5})}))
    ledger = Ledger(config, PriceTable.default())
    state = State()
    rows = {(row["route"], row["purpose"]): row for row in ledger.status_rows(state)}
    assert rows[("r1", "repair")]["cap"] == 1.5 and rows[("r1", "repair")]["spent"] == 0 and rows[("r1", "repair")]["calls"] == 0
    assert "repair" in ledger.format_status(state)
    purposes = ledger.summary(state)["routes"]["r1"]["purposes"]
    assert purposes["repair"] == {"cap_usd": 1.5, "spent_usd": 0, "storage_usd": 0, "calls": 0}


# ------------------------------------------------------------------ what the configuration says before a run


def test_a_route_with_no_cap_for_repair_is_noted_when_the_step_is_on_in_the_words_of_the_other_purposes():
    on = Config.from_dict(make(repair={"enabled": True}, review={"policy": {"fix_to_hand": True}}))
    assert "route r1: no cap for purpose 'repair' (calls with that purpose are refused)" in on.problems()
    # the other purposes are noted in the same words
    assert "route r1: no cap for purpose 'production' (calls with that purpose are refused)" in Config.from_dict(
        make(routes={"r1": dict(ROUTE, purpose_caps={})})
    ).problems()


def test_a_route_with_a_cap_for_repair_is_not_noted():
    routes = {"r1": dict(ROUTE, purpose_caps={**ROUTE["purpose_caps"], "repair": 1})}
    config = Config.from_dict(make(routes=routes, repair={"enabled": True}, review={"policy": {"fix_to_hand": True}}))
    assert repair_notes(config) == []


def test_nothing_is_said_about_repair_while_the_step_is_off():
    assert repair_notes(Config.from_dict(make())) == []  # a configuration that never heard of the step reads as before
    assert repair_notes(Config.from_dict(make(repair={"enabled": False, "min_rows": 4}))) == []


def test_the_step_without_fix_to_hand_is_noted_because_no_row_would_wait_for_it():
    config = Config.from_dict(make(repair={"enabled": True}))
    assert any("fix_to_hand is off" in note and "nothing is sent back" in note for note in config.problems())
    config = Config.from_dict(make(repair={"enabled": True}, review={"policy": {"fix_to_hand": True}}))
    assert not any("fix_to_hand" in note for note in config.problems())
    assert not any("fix_to_hand" in note for note in Config.from_dict(make()).problems())


def test_the_repair_cap_counts_in_the_note_about_caps_above_the_route_total():
    routes = {"r1": dict(ROUTE, budget_usd=10, purpose_caps={"test": 1, "pricing": 2, "probe": 1, "rebound": 3, "production": 2, "repair": 5})}
    notes = Config.from_dict(make(routes=routes)).problems()
    assert any("purpose caps add up to $14.00, above the route total $10.00" in note for note in notes)
