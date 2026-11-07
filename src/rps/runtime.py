"""Wiring: build the stores, writers and controller a run needs from its configuration.

A run uses two stores:

    state store   event logs, review packets and their pictures, reviewers' verdicts
                  (a storage bucket when `bucket` is set, else a local folder)
    ship store    accepted rows and the ledger
                  (a dataset repository when `data_repo` is set, else a local folder)

Both are wrapped for one writer per file by whoever uses them (the controller, a
reviewer client). Keys are read from environment variables only.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import Config
from .controller import Controller
from .providers import make_writer
from .providers.base import Writer
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


def build_writers(config: Config) -> dict[str, Writer]:
    """One writer per route, chosen by each route's provider name."""
    return {name: make_writer(name, route) for name, route in config.routes.items()}


def build_controller(
    config: Config,
    *,
    create: bool = False,
    writers: Mapping[str, Writer] | None = None,
    state_store: Store | None = None,
    ship_store: Store | None = None,
    **options: Any,
) -> Controller:
    """A controller wired to the stores and writers a configuration names.

    `create` makes the bucket and the dataset repository (both private) when they
    do not exist yet. Pass `writers`, `state_store` or `ship_store` to use your own
    instead of the configured ones; every other keyword (`prices`, `assess`,
    `printer`, ...) goes to `Controller`.

    A provider that has no adapter yet raises ConfigError here; an adapter that is
    still a stub raises NotImplementedError when the controller first uses it.
    """
    return Controller(
        config,
        state_store if state_store is not None else build_state_store(config, create=create),
        dict(writers) if writers is not None else build_writers(config),
        ship_store=ship_store if ship_store is not None else build_ship_store(config, create=create),
        **options,
    )
