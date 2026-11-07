"""Wiring: stores, writers and controllers built from a configuration."""

import huggingface_hub
import pytest

from rps import runtime
from rps.config import ConfigError
from rps.controller import Controller
from rps.events import replay
from rps.providers import AnthropicWriter, FakeWriter
from rps.store.base import StoreError
from rps.store.bucket import BucketStore
from rps.store.hub import HubStore
from rps.store.local import LocalStore

from fakes import FakeBucketApi, FakeHubApi
from harness import fake_image_loader, task_spec
from helpers import make_config


def local_config(tmp_path, **overrides):
    return make_config(work_dir=str(tmp_path / "work"), **overrides)


def route(provider="fake", model="fake-model", kind="batch"):
    return {"provider": provider, "model": model, "kind": kind, "budget_usd": 1.0, "purpose_caps": {"production": 1.0}}


def test_without_a_bucket_or_repository_the_stores_are_folders_under_the_work_dir(tmp_path):
    config = local_config(tmp_path)
    state, ship = runtime.build_state_store(config), runtime.build_ship_store(config)
    assert isinstance(state, LocalStore) and isinstance(ship, LocalStore)
    assert state.root == tmp_path / "work" / "state" and ship.root == tmp_path / "work" / "ship"


def test_a_bucket_and_a_repository_give_remote_stores_with_the_configured_limits(tmp_path):
    config = local_config(
        tmp_path,
        bucket="owner/state",
        data_repo="owner/rows",
        commit={
            "events_per_commit": 40,
            "seconds_per_commit": 2,
            "ship_seconds_per_commit": 90,
            "ship_files_per_commit": 50,
            "ship_commits_per_hour": 100,
        },
    )
    state, ship = runtime.build_state_store(config), runtime.build_ship_store(config)
    assert isinstance(state, BucketStore) and state.bucket_id == "owner/state"
    assert (state.max_pending_ops, state.max_pending_seconds) == (40, 2)
    assert isinstance(ship, HubStore) and ship.repo_id == "owner/rows"
    assert (ship.min_interval_seconds, ship.max_ops_per_commit, ship.hourly_cap) == (90, 50, 100)


def test_the_dataset_repository_commits_at_most_once_a_minute_with_under_100_files_by_default(tmp_path):
    ship = runtime.build_ship_store(local_config(tmp_path, data_repo="owner/rows"))
    assert ship.min_interval_seconds == 60 and ship.max_pending_seconds == 60
    assert ship.max_ops_per_commit == 99 and ship.max_pending_ops == 99
    assert ship.hourly_cap == 300 < 320


class CombinedApi:
    """One client with the bucket calls and the repository calls, as HfApi has."""

    def __init__(self):
        self.hub, self.bucket = FakeHubApi(), FakeBucketApi()

    def create_bucket(self, *args, **kwargs):
        return self.bucket.create_bucket(*args, **kwargs)

    def create_repo(self, *args, **kwargs):
        return self.hub.create_repo(*args, **kwargs)


def test_create_makes_a_private_bucket_and_a_private_repository(tmp_path, monkeypatch):
    api = CombinedApi()
    monkeypatch.setattr(huggingface_hub, "HfApi", lambda token=None: api)
    monkeypatch.setenv("HF_TOKEN", "a-token-for-this-test")
    config = local_config(tmp_path, bucket="owner/state", data_repo="owner/rows")
    runtime.build_state_store(config, create=True)
    runtime.build_ship_store(config, create=True)
    assert api.bucket.created == [{"bucket_id": "owner/state", "private": True, "exist_ok": True}]
    assert api.hub.created_repos == [{"repo_id": "owner/rows", "private": True, "repo_type": "dataset", "exist_ok": True}]


def test_creating_without_a_token_fails_before_any_network_call(tmp_path):
    with pytest.raises(StoreError, match="HF_TOKEN is not set"):
        runtime.build_state_store(local_config(tmp_path, bucket="owner/state"), create=True)
    with pytest.raises(StoreError, match="HF_TOKEN is not set"):
        runtime.build_ship_store(local_config(tmp_path, data_repo="owner/rows"), create=True)


def test_a_controller_built_from_a_config_runs_a_dry_run_on_local_folders(tmp_path):
    config = local_config(tmp_path)
    printed = []
    controller = runtime.build_controller(config, printer=printed.append, image_loader=fake_image_loader)
    assert isinstance(controller, Controller) and isinstance(controller.writers["fake-batch"], FakeWriter)
    controller.add_task(task_spec(("001", "002")))
    summary = controller.run()
    assert summary.reason == "idle"
    assert controller.state.counts()["packets_open"] == 2  # the rows now wait for reviewers
    work = tmp_path / "work"
    assert [path.suffix for path in (work / "state" / "events").iterdir()] == [".jsonl"]
    assert any((work / "state" / "packets").rglob("packet.json"))
    assert (work / "ship" / "ledger" / "summary.json").exists()
    assert any("job 2 lines" in line for line in printed)


def test_stores_and_writers_can_be_supplied_instead_of_the_configured_ones(tmp_path):
    mine = LocalStore(tmp_path / "mine-state", fsync=False)
    theirs = LocalStore(tmp_path / "mine-ship", fsync=False)
    config = local_config(tmp_path)
    writers = {"fake-batch": FakeWriter("fake-batch", config.routes["fake-batch"])}
    controller = runtime.build_controller(
        config, writers=writers, state_store=mine, ship_store=theirs, printer=lambda text: None, image_loader=fake_image_loader
    )
    assert controller.writers["fake-batch"] is writers["fake-batch"]
    controller.add_task(task_spec(("001",)))
    controller.run()
    assert mine.list("events/") and theirs.list("ledger/")
    assert not (tmp_path / "work").exists()  # nothing went to the configured folders


def test_a_provider_without_an_adapter_is_refused_when_the_controller_is_built(tmp_path):
    config = local_config(tmp_path, routes={"x": route(provider="nobody")})
    with pytest.raises(ConfigError, match="no adapter for provider 'nobody'"):
        runtime.build_controller(config)


def test_a_stub_adapter_fails_loudly_the_first_time_it_is_used(tmp_path):
    config = local_config(tmp_path, routes={"claude-batch": route(provider="anthropic", model="claude-haiku-5-5")})
    controller = runtime.build_controller(config, printer=lambda text: None, image_loader=fake_image_loader)
    assert isinstance(controller.writers["claude-batch"], AnthropicWriter)
    controller.add_task(task_spec(("001",), route="claude-batch"))
    with pytest.raises(NotImplementedError, match="stub"):
        controller.run()
    kinds = [event.kind for event in replay(controller.state_store).events]
    assert "task_created" in kinds and "call_prepared" not in kinds  # no call was recorded, so nothing was spent
