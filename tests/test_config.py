import json

import pytest

from rps.config import PURPOSES, REBOUND, Config, ConfigError

ROUTE = {
    "provider": "fake",
    "model": "fake-model",
    "kind": "batch",
    "effort": "low",
    "budget_usd": 10,
    "purpose_caps": {"test": 1, "pricing": 2, "probe": 0.5, "rebound": 3, "production": 8},
}


def make(**overrides):
    data = {"routes": {"r1": dict(ROUTE)}, "sources": {"photos": {"image_size": 512, "license": "CC BY-NC"}}}
    data.update(overrides)
    return data


def test_defaults_and_round_trip():
    config = Config.from_dict(make(data_repo="owner/data", bucket="owner/state", time_budget_seconds=3600))
    assert config.data_repo == "owner/data"
    assert config.bucket == "owner/state"
    assert config.routes["r1"].cap_for("pricing") == 2.0
    assert config.routes["r1"].budget_usd == 10.0
    assert config.source("photos").image_size == 512
    assert config.source("unknown").image_size is None
    assert config.commit.ship_seconds_per_commit == 60.0
    assert config.commit.ship_files_per_commit == 99
    assert Config.from_dict(config.to_dict()) == config
    assert REBOUND in PURPOSES


def test_load_from_json_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(make()), encoding="utf-8")
    assert Config.load(path).routes["r1"].model == "fake-model"
    with pytest.raises(ConfigError):
        Config.load(tmp_path / "missing.json")
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(path)


def test_a_purpose_without_a_cap_has_a_cap_of_zero():
    config = Config.from_dict({"routes": {"r": {"provider": "fake", "model": "m", "budget_usd": 5}}})
    assert config.routes["r"].cap_for("production") == 0.0
    notes = config.problems()
    assert any("production" in note for note in notes)


def test_unknown_keys_are_rejected_at_every_level():
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict(make(budget=5))
    bad_route = dict(ROUTE, extra=1)
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict({"routes": {"r": bad_route}})
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict(make(commit={"every": 5}))


def test_purpose_and_kind_are_validated():
    with pytest.raises(ConfigError, match="unknown purpose"):
        Config.from_dict({"routes": {"r": dict(ROUTE, purpose_caps={"gaming": 1})}})
    with pytest.raises(ConfigError, match="kind"):
        Config.from_dict({"routes": {"r": dict(ROUTE, kind="telepathy")}})
    with pytest.raises(ConfigError):
        Config.from_dict({"routes": {"r": dict(ROUTE, budget_usd=-1)}})
    with pytest.raises(ConfigError):
        Config.from_dict({"routes": {"bad name!": dict(ROUTE)}})


def test_commit_limits_follow_the_hub_guidance():
    with pytest.raises(ConfigError):
        Config.from_dict(make(commit={"ship_files_per_commit": 100}))
    with pytest.raises(ConfigError):
        Config.from_dict(make(commit={"ship_commits_per_hour": 400}))
    ok = Config.from_dict(make(commit={"ship_seconds_per_commit": 120, "ship_files_per_commit": 50}))
    assert ok.commit.ship_seconds_per_commit == 120.0


def test_values_that_look_like_keys_are_rejected_without_echoing_them():
    secret = "sk-" + "a" * 30
    with pytest.raises(ConfigError) as caught:
        Config.from_dict(make(pool_version=secret))
    assert secret not in str(caught.value)
    with pytest.raises(ConfigError):
        Config.from_dict(make(bucket="hf_" + "B" * 30))


def test_review_settings_are_checked():
    same = [{"id": "a", "family": "x"}, {"id": "a", "family": "y"}]
    with pytest.raises(ConfigError):
        Config.from_dict(make(review={"reviewers": same}))
    with pytest.raises(ConfigError):
        Config.from_dict(make(review={"reviewers": [{"id": "a"}]}))
    with pytest.raises(ConfigError):
        Config.from_dict(make(review={"reviewers": ["just-a-name"]}))
    with pytest.raises(ConfigError):
        Config.from_dict(make(review={"policy": {"audit_fraction": 2}}))
    with pytest.raises(ConfigError):
        Config.from_dict(make(review={"policy": {"max_reviewers": 1, "keeps_to_accept": 2, "rejects_to_discard": 2}}))
    with pytest.raises(ConfigError):
        Config.from_dict(make(review={"policy": {"unknown": 1}}))


def test_review_policy_defaults_follow_the_owners_protocol():
    policy = Config.from_dict(make()).review.policy
    assert policy.keeps_to_accept == 2
    assert policy.rejects_to_discard == 2
    assert policy.max_reviewers == 3
    assert policy.first_keep_accepts is True
    assert policy.audit_fraction == pytest.approx(0.10)


def test_reviewers_carry_families_and_can_be_disabled():
    config = Config.from_dict(
        make(
            review={
                "reviewers": [
                    {"id": "desk", "family": "anthropic"},
                    {"id": "mac", "family": "google", "enabled": False},
                    {"id": "codex", "family": "openai"},
                ],
                "packet_size": 5,
                "provider_families": {"acme": "acme-family"},
            }
        )
    )
    assert [r.id for r in config.review.enabled_reviewers()] == ["desk", "codex"]
    assert config.review.reviewer("mac").family == "google"
    assert config.review.reviewer("nobody") is None
    assert config.review.family_of("gemini") == "google"
    assert config.review.family_of("acme") == "acme-family"
    assert config.review.family_of("unmapped") == "unmapped"
    assert Config.from_dict(config.to_dict()) == config


def test_a_fix_goes_to_a_further_reviewer_unless_the_policy_sends_it_to_the_owner():
    assert Config.from_dict(make()).review.policy.fix_to_hand is False
    config = Config.from_dict(make(review={"policy": {"fix_to_hand": True, "max_reviewers": 4}}))
    assert config.review.policy.fix_to_hand is True and config.review.policy.max_reviewers == 4
    assert config.review.policy.to_dict()["fix_to_hand"] is True
    assert Config.from_dict(config.to_dict()) == config
    assert Config.from_dict(config.to_dict()).digest() == config.digest()
    assert Config.from_dict(make()).digest() != config.digest()
    for bad in ("yes", 1, 0, None, [True]):
        with pytest.raises(ConfigError, match="fix_to_hand"):
            Config.from_dict(make(review={"policy": {"fix_to_hand": bad}}))
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict(make(review={"policy": {"fix_it": True}}))


def test_a_reviewer_may_keep_alone_unless_the_configuration_says_otherwise():
    config = Config.from_dict(
        make(
            review={
                "reviewers": [
                    {"id": "desk", "family": "anthropic"},
                    {"id": "newcomer", "family": "google", "solo_keep": False},
                    {"id": "codex", "family": "openai", "solo_keep": True},
                ]
            }
        )
    )
    assert [r.solo_keep for r in config.review.reviewers] == [True, False, True]
    assert config.review.may_keep_alone("desk") and config.review.may_keep_alone("codex")
    assert not config.review.may_keep_alone("newcomer")
    assert config.review.may_keep_alone("not-in-the-configuration")  # the default; its verdict was given when it was listed
    assert config.review.reviewers[1].to_dict() == {"id": "newcomer", "family": "google", "enabled": True, "solo_keep": False}
    assert Config.from_dict(config.to_dict()) == config
    assert Config.from_dict(config.to_dict()).digest() == config.digest()


def test_solo_keep_is_checked_and_other_unknown_reviewer_keys_are_still_refused():
    for bad in ("no", 0, 1, None, [True]):
        with pytest.raises(ConfigError, match="solo_keep"):
            Config.from_dict(make(review={"reviewers": [{"id": "a", "family": "x", "solo_keep": bad}]}))
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_dict(make(review={"reviewers": [{"id": "a", "family": "x", "trusted": True}]}))


def test_a_reviewer_weight_is_read_checked_and_left_out_when_it_is_one():
    config = Config.from_dict(
        make(review={"reviewers": [{"id": "desk", "family": "anthropic"}, {"id": "api", "family": "anthropic", "weight": 6}]})
    )
    assert [r.weight for r in config.review.reviewers] == [1, 6]
    assert [r.weight for r in config.review.enabled_reviewers()] == [1, 6]
    assert config.review.reviewers[0].to_dict() == {"id": "desk", "family": "anthropic", "enabled": True, "solo_keep": True}
    assert config.review.reviewers[1].to_dict()["weight"] == 6
    assert Config.from_dict(config.to_dict()) == config
    # a weight of 1 written out reads exactly as a reviewer with no weight
    plain = Config.from_dict(make(review={"reviewers": [{"id": "desk", "family": "anthropic"}]}))
    same = Config.from_dict(make(review={"reviewers": [{"id": "desk", "family": "anthropic", "weight": 1}]}))
    assert plain.digest() == same.digest()
    for bad in (0, -1, 1.5, 101, True, "2", None):
        with pytest.raises(ConfigError, match="weight"):
            Config.from_dict(make(review={"reviewers": [{"id": "a", "family": "x", "weight": bad}]}))


def test_a_reviewer_list_with_no_one_who_may_keep_alone_is_noted():
    def notes(reviewers):
        return [note for note in Config.from_dict(make(review={"reviewers": reviewers})).problems() if "solo_keep" in note]

    two = [{"id": "a", "family": "x"}, {"id": "b", "family": "y"}]
    assert notes(two) == []
    assert notes([dict(two[0], solo_keep=False), two[1]]) == []
    assert len(notes([dict(item, solo_keep=False) for item in two])) == 1
    # a reviewer that is switched off does not count either way
    assert notes([dict(two[0], solo_keep=False), dict(two[1], enabled=False)]) != []
    assert notes([dict(two[0], solo_keep=False, enabled=False), two[1]]) == []
    assert notes([]) == []


def test_a_reviewer_is_active_only_when_it_is_listed_and_switched_on():
    config = Config.from_dict(
        make(review={"reviewers": [{"id": "on", "family": "x"}, {"id": "off", "family": "y", "enabled": False}]})
    )
    assert config.review.is_active("on")
    assert not config.review.is_active("off")
    assert not config.review.is_active("gone")


def test_stop_file_resolves_under_the_work_dir():
    config = Config.from_dict(make(work_dir="work"))
    assert config.stop_file_path().name == "STOP"
    assert config.stop_file_path().parent.name == "work"
    assert Config.from_dict(make(stop_file=None)).stop_file_path() is None
    with pytest.raises(ConfigError):
        Config.from_dict(make(remote_stop_path="../escape"))
