"""Wiring: build the stores a run needs from its configuration.

A run uses two stores:

    state store   event logs, review packets and their pictures, reviewers' verdicts
                  (a storage bucket when `bucket` is set, else a local folder)
    ship store    accepted rows and the ledger
                  (a dataset repository when `data_repo` is set, else a local folder)

Both are wrapped for one writer per file by whoever uses them (the controller, a
reviewer client). Keys are read from environment variables only.
"""

from __future__ import annotations

from .config import Config
from .store.base import Store
from .store.bucket import BucketStore
from .store.hub import HubStore
from .store.local import LocalStore


def build_state_store(config: Config, *, create: bool = False) -> Store:
    """The store for event logs, packets and verdicts.

    With `create` the bucket is created (private) if it does not exist yet.
    """
    if config.bucket:
        store = BucketStore(
            config.bucket,
            events_per_flush=config.commit.events_per_commit,
            seconds_per_flush=config.commit.seconds_per_commit,
        )
        if create:
            store.ensure_exists(private=True)
        return store
    return LocalStore(config.work_path("state"))


def build_ship_store(config: Config, *, create: bool = False) -> Store:
    """The store for accepted rows and the ledger.

    With `create` the dataset repository is created (private) if it does not exist yet.
    """
    if config.data_repo:
        store = HubStore(
            config.data_repo,
            writer_tag="controller",
            seconds_per_commit=config.commit.ship_seconds_per_commit,
            files_per_commit=config.commit.ship_files_per_commit,
            commits_per_hour=config.commit.ship_commits_per_hour,
        )
        if create:
            store.ensure_exists(private=True)
        return store
    return LocalStore(config.work_path("ship"))
