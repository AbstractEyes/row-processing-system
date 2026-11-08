"""Route settings, picture classes and the rules section of the configuration."""

import json

import pytest

from rps.config import DEFAULT_WORKERS, Config, ConfigError, RouteConfig, RulesConfig, SourceConfig
from rps.tasks import SpecLine

from helpers import make_config


def route_dict(**overrides):
    fields = {"provider": "openai", "model": "gpt-6-luna", "kind": "batch", "budget_usd": 1.0}
    fields.update(overrides)
    return fields


def test_a_route_has_safe_defaults_for_the_new_settings():
    route = RouteConfig.from_dict("luna", route_dict())
    assert route.enabled is None and route.is_enabled is True
    assert (route.max_retries, route.timeout_seconds, route.workers, route.truncation_factor) == (2, None, DEFAULT_WORKERS, 2.0)
    assert DEFAULT_WORKERS == 32 and RouteConfig(provider="openai", model="m").workers == 32
    assert dict(route.options) == {}


def test_deepseek_stays_off_unless_a_configuration_turns_it_on():
    off = RouteConfig.from_dict("ds", route_dict(provider="deepseek", kind="api"))
    assert off.enabled is None and off.is_enabled is False
    on = RouteConfig.from_dict("ds", route_dict(provider="deepseek", kind="api", enabled=True))
    assert on.is_enabled is True
    explicit_off = RouteConfig.from_dict("luna", route_dict(enabled=False))
    assert explicit_off.is_enabled is False


def test_the_new_route_settings_round_trip_through_plain_data():
    data = route_dict(enabled=True, max_retries=5, timeout_seconds=120.5, workers=40, truncation_factor=1.5, options={"detail": "high", "cache": True, "n": 3})
    route = RouteConfig.from_dict("luna", data)
    again = RouteConfig.from_dict("luna", json.loads(json.dumps(route.to_dict())))
    assert again == route and again.options == {"detail": "high", "cache": True, "n": 3}


@pytest.mark.parametrize(
    "overrides",
    [
        {"enabled": "yes"},
        {"max_retries": -1},
        {"max_retries": 11},
        {"timeout_seconds": 0},
        {"workers": 0},
        {"workers": 2501},
        {"truncation_factor": 0.5},
        {"truncation_factor": 17},
        {"options": {"nested": {"a": 1}}},
        {"options": {"list": [1, 2]}},
        {"options": "detail=high"},
        {"surprise": 1},
    ],
)
def test_bad_route_settings_are_refused(overrides):
    with pytest.raises(ConfigError):
        RouteConfig.from_dict("luna", route_dict(**overrides))


def test_the_worker_cap_is_2500():
    assert RouteConfig.from_dict("ds", route_dict(provider="deepseek", kind="api", workers=2500)).workers == 2500


def test_a_source_can_name_the_class_of_its_pictures():
    source = SourceConfig.from_dict("photos", {"image_size": 512, "image_class": "coco"})
    assert source.image_class == "coco"
    assert SourceConfig.from_dict("photos", source.to_dict()) == source
    assert SourceConfig.from_dict("photos", {}).image_class is None
    with pytest.raises(ConfigError, match="image_class"):
        SourceConfig.from_dict("photos", {"image_class": "mystery"})


def test_a_spec_line_can_override_the_class_of_its_picture():
    line = SpecLine("001", "spec", image="p.jpg", image_class="ffhq")
    assert line.to_dict()["image_class"] == "ffhq"
    assert SpecLine.from_dict(line.to_dict()).image_class == "ffhq"
    assert SpecLine("001", "spec").image_class is None
    with pytest.raises(ValueError, match="image_class|class"):
        SpecLine("001", "spec", image="p.jpg", image_class="mystery")


def test_the_rules_section_names_two_files_or_a_package():
    assert RulesConfig.from_dict({"package": "rps.rules"}).package == "rps.rules"
    files = RulesConfig.from_dict({"system_file": "a.md", "schema_file": "b.json", "version": "v1"})
    assert (files.system_file, files.schema_file, files.version) == ("a.md", "b.json", "v1")
    with pytest.raises(ConfigError, match="both system_file and schema_file"):
        RulesConfig.from_dict({"system_file": "a.md"})
    with pytest.raises(ConfigError, match="both system_file and schema_file"):
        RulesConfig.from_dict({})
    with pytest.raises(ConfigError, match="surprise"):
        RulesConfig.from_dict({"package": "p", "surprise": 1})


def test_the_assessor_in_the_rules_section_is_module_colon_name_or_none():
    assert RulesConfig.from_dict({"package": "rps.rules", "assessor": "rps.assessor:Assessor"}).assessor == "rps.assessor:Assessor"
    assert RulesConfig.from_dict({"package": "rps.rules", "assessor": "none"}).assessor == "none"
    with pytest.raises(ConfigError, match="module:name"):
        RulesConfig.from_dict({"package": "rps.rules", "assessor": "Assessor"})


def test_the_screen_in_the_rules_section_is_module_colon_name_or_none():
    assert RulesConfig.from_dict({"package": "rps.rules"}).screen is None
    assert RulesConfig.from_dict({"package": "rps.rules", "screen": "rps.assessor:screen_item"}).screen == "rps.assessor:screen_item"
    assert RulesConfig.from_dict({"package": "rps.rules", "screen": "none"}).screen == "none"
    with pytest.raises(ConfigError, match=r"rules\.screen: expected 'module:name'"):
        RulesConfig.from_dict({"package": "rps.rules", "screen": "screen_item"})
    assert RulesConfig.from_dict(RulesConfig(package="rps.rules", screen="none").to_dict()).screen == "none"


def test_the_whole_configuration_round_trips_with_its_rules_section():
    config = make_config(rules={"package": "rps.rules"})
    again = Config.from_dict(json.loads(json.dumps(config.to_dict())))
    assert again.rules == config.rules and again.digest() == config.digest()
    assert make_config().rules is None


def test_a_configuration_notes_routes_that_are_switched_off():
    config = make_config(
        routes={
            "ds": route_dict(provider="deepseek", kind="api"),
            "luna": route_dict(),
        }
    )
    notes = config.problems()
    assert any(note.startswith("route ds: disabled") for note in notes)
    assert not any(note.startswith("route luna: disabled") for note in notes)
