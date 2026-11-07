"""Run configuration, loaded from a JSON file or a plain dict.

The configuration holds no secrets. API keys are read from environment variables
by the provider adapters, and a value that looks like a key is rejected here.

Example:

    {
      "data_repo": "owner/dataset-name",
      "bucket": "owner/state-bucket",
      "work_dir": "state_local",
      "routes": {
        "fast-batch": {
          "provider": "anthropic", "model": "model-id", "kind": "batch",
          "budget_usd": 50.0,
          "purpose_caps": {"test": 2, "pricing": 3, "probe": 1, "rebound": 8, "production": 40}
        }
      },
      "sources": {"photos": {"image_size": 512, "license": "CC BY-NC"}},
      "time_budget_seconds": 10800
    }
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .ids import canonical_json
from .keys import looks_like_secret
from .layout import NAME_PATTERN, PathError, normalize_path

# Why a call is made. Each (route, purpose) pair has its own dollar cap.
PURPOSES: tuple[str, ...] = ("test", "pricing", "probe", "rebound", "production")
# A retry or re-submission after a failed or rejected call always counts as this purpose.
REBOUND = "rebound"
ROUTE_KINDS: tuple[str, ...] = ("api", "batch", "claude-code", "codex", "gemini-cli")

# The Hub's documented ceiling is about 320 commits per hour per repository.
HUB_COMMITS_PER_HOUR_LIMIT = 320
# Commits stay under this many files.
HUB_FILES_PER_COMMIT_LIMIT = 99


class ConfigError(ValueError):
    """The configuration is invalid."""


def _fail(message: str) -> None:
    raise ConfigError(message)


def _number(
    value: Any,
    where: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    integer: bool = False,
) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{where}: expected a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        _fail(f"{where}: must be finite")
    if integer:
        if isinstance(value, float) and not value.is_integer():
            _fail(f"{where}: expected a whole number, got {value!r}")
        value = int(value)
    if minimum is not None and value < minimum:
        _fail(f"{where}: must be at least {minimum}, got {value!r}")
    if maximum is not None and value > maximum:
        _fail(f"{where}: must be at most {maximum}, got {value!r}")
    return value


def _text(value: Any, where: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(f"{where}: expected a non-empty string, got {value!r}")
    return value


def _check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        _fail(f"{where}: unknown key(s) {unknown}")


def _reject_secret_values(value: Any, where: str = "config") -> None:
    if isinstance(value, str):
        if looks_like_secret(value):
            _fail(f"{where}: a value looks like an API key or token; keys belong in environment variables")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_secret_values(item, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_values(item, f"{where}[{index}]")


@dataclass(frozen=True)
class RouteConfig:
    """One way of getting rows written: a provider, a model and an access path.

    `budget_usd` is the total cap for the route. `purpose_caps` holds one dollar cap
    per purpose; a purpose with no entry has a cap of zero, so its calls are refused.
    """

    provider: str
    model: str
    kind: str = "api"
    effort: str | None = None
    budget_usd: float = 0.0
    purpose_caps: Mapping[str, float] = field(default_factory=dict)
    max_output_tokens: int = 4096
    max_batch_calls: int = 500

    def cap_for(self, purpose: str) -> float:
        """Dollar cap for a purpose on this route."""
        if purpose not in PURPOSES:
            raise ConfigError(f"unknown purpose: {purpose!r}")
        return float(self.purpose_caps.get(purpose, 0.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "kind": self.kind,
            "effort": self.effort,
            "budget_usd": self.budget_usd,
            "purpose_caps": {key: self.purpose_caps[key] for key in sorted(self.purpose_caps)},
            "max_output_tokens": self.max_output_tokens,
            "max_batch_calls": self.max_batch_calls,
        }

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "RouteConfig":
        where = f"routes.{name}"
        if not NAME_PATTERN.match(name):
            _fail(f"{where}: route names use letters, digits, '.', '_' and '-' (up to 64 characters)")
        if not isinstance(data, Mapping):
            _fail(f"{where}: expected an object")
        _check_keys(
            data,
            {"provider", "model", "kind", "effort", "budget_usd", "purpose_caps", "max_output_tokens", "max_batch_calls"},
            where,
        )
        kind = data.get("kind", "api")
        if kind not in ROUTE_KINDS:
            _fail(f"{where}.kind: must be one of {list(ROUTE_KINDS)}, got {kind!r}")
        caps_in = data.get("purpose_caps", {})
        if not isinstance(caps_in, Mapping):
            _fail(f"{where}.purpose_caps: expected an object")
        caps: dict[str, float] = {}
        for purpose, cap in caps_in.items():
            if purpose not in PURPOSES:
                _fail(f"{where}.purpose_caps: unknown purpose {purpose!r}; use one of {list(PURPOSES)}")
            caps[purpose] = float(_number(cap, f"{where}.purpose_caps.{purpose}", minimum=0))
        return cls(
            provider=_text(data.get("provider"), f"{where}.provider"),
            model=_text(data.get("model"), f"{where}.model"),
            kind=kind,
            effort=_text(data.get("effort"), f"{where}.effort", allow_none=True),
            budget_usd=float(_number(data.get("budget_usd", 0.0), f"{where}.budget_usd", minimum=0)),
            purpose_caps=caps,
            max_output_tokens=_number(
                data.get("max_output_tokens", 4096), f"{where}.max_output_tokens", minimum=1, integer=True
            ),
            max_batch_calls=_number(data.get("max_batch_calls", 500), f"{where}.max_batch_calls", minimum=1, integer=True),
        )


@dataclass(frozen=True)
class SourceConfig:
    """Settings for one data source (a dataset, a seed list, a picture collection).

    `image_size` is the longest side in pixels of the pictures shown to the writer
    and to reviewers; None keeps the original size. `images_dir` is the folder that
    holds the original pictures.
    """

    image_size: int | None = None
    license: str | None = None
    dataset: str | None = None
    images_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_size": self.image_size,
            "license": self.license,
            "dataset": self.dataset,
            "images_dir": self.images_dir,
        }

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "SourceConfig":
        where = f"sources.{name}"
        if not isinstance(data, Mapping):
            _fail(f"{where}: expected an object")
        _check_keys(data, {"image_size", "license", "dataset", "images_dir"}, where)
        size = data.get("image_size")
        return cls(
            image_size=None if size is None else _number(size, f"{where}.image_size", minimum=16, integer=True),
            license=_text(data.get("license"), f"{where}.license", allow_none=True),
            dataset=_text(data.get("dataset"), f"{where}.dataset", allow_none=True),
            images_dir=_text(data.get("images_dir"), f"{where}.images_dir", allow_none=True),
        )


@dataclass(frozen=True)
class CommitConfig:
    """How writes to the stores are batched.

    The state store (event logs, packets) flushes after `events_per_commit` buffered
    writes or `seconds_per_commit` seconds, whichever comes first. The dataset repo
    (accepted rows, ledger) makes at most one commit per `ship_seconds_per_commit`
    seconds, with at most `ship_files_per_commit` files each and at most
    `ship_commits_per_hour` commits in any hour.
    """

    events_per_commit: int = 100
    seconds_per_commit: float = 5.0
    ship_seconds_per_commit: float = 60.0
    ship_files_per_commit: int = HUB_FILES_PER_COMMIT_LIMIT
    ship_commits_per_hour: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_per_commit": self.events_per_commit,
            "seconds_per_commit": self.seconds_per_commit,
            "ship_seconds_per_commit": self.ship_seconds_per_commit,
            "ship_files_per_commit": self.ship_files_per_commit,
            "ship_commits_per_hour": self.ship_commits_per_hour,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CommitConfig":
        where = "commit"
        if not isinstance(data, Mapping):
            _fail(f"{where}: expected an object")
        _check_keys(
            data,
            {
                "events_per_commit",
                "seconds_per_commit",
                "ship_seconds_per_commit",
                "ship_files_per_commit",
                "ship_commits_per_hour",
            },
            where,
        )
        default = cls()
        return cls(
            events_per_commit=_number(
                data.get("events_per_commit", default.events_per_commit),
                f"{where}.events_per_commit",
                minimum=1,
                integer=True,
            ),
            seconds_per_commit=float(
                _number(data.get("seconds_per_commit", default.seconds_per_commit), f"{where}.seconds_per_commit", minimum=0)
            ),
            ship_seconds_per_commit=float(
                _number(
                    data.get("ship_seconds_per_commit", default.ship_seconds_per_commit),
                    f"{where}.ship_seconds_per_commit",
                    minimum=1,
                )
            ),
            ship_files_per_commit=_number(
                data.get("ship_files_per_commit", default.ship_files_per_commit),
                f"{where}.ship_files_per_commit",
                minimum=1,
                maximum=HUB_FILES_PER_COMMIT_LIMIT,
                integer=True,
            ),
            ship_commits_per_hour=_number(
                data.get("ship_commits_per_hour", default.ship_commits_per_hour),
                f"{where}.ship_commits_per_hour",
                minimum=1,
                maximum=HUB_COMMITS_PER_HOUR_LIMIT,
                integer=True,
            ),
        )


@dataclass(frozen=True)
class ReviewConfig:
    """How rows are grouped for review and when a row counts as decided."""

    packet_size: int = 20
    brief_version: str = "1"
    reviewers: tuple[str, ...] = ()
    required_verdicts: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_size": self.packet_size,
            "brief_version": self.brief_version,
            "reviewers": list(self.reviewers),
            "required_verdicts": self.required_verdicts,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewConfig":
        where = "review"
        if not isinstance(data, Mapping):
            _fail(f"{where}: expected an object")
        _check_keys(data, {"packet_size", "brief_version", "reviewers", "required_verdicts"}, where)
        reviewers = data.get("reviewers", [])
        if not isinstance(reviewers, (list, tuple)):
            _fail(f"{where}.reviewers: expected a list")
        for reviewer in reviewers:
            if not isinstance(reviewer, str) or not NAME_PATTERN.match(reviewer):
                _fail(f"{where}.reviewers: invalid reviewer id {reviewer!r}")
        if len(set(reviewers)) != len(reviewers):
            _fail(f"{where}.reviewers: duplicate reviewer ids")
        required = _number(data.get("required_verdicts", 1), f"{where}.required_verdicts", minimum=1, integer=True)
        if reviewers and required > len(reviewers):
            _fail(f"{where}.required_verdicts: {required} is more than the {len(reviewers)} reviewers")
        return cls(
            packet_size=_number(data.get("packet_size", 20), f"{where}.packet_size", minimum=1, integer=True),
            brief_version=str(_text(str(data.get("brief_version", "1")), f"{where}.brief_version")),
            reviewers=tuple(reviewers),
            required_verdicts=required,
        )


_TOP_KEYS = {
    "data_repo",
    "bucket",
    "work_dir",
    "price_table",
    "pool_version",
    "routes",
    "sources",
    "commit",
    "review",
    "time_budget_seconds",
    "poll_seconds",
    "max_attempts",
    "stop_file",
    "remote_stop_path",
    "events_max_bytes",
    "wait_for_review",
    "tasks_file",
}


@dataclass(frozen=True)
class Config:
    """Everything a run needs to know, except secrets.

    `bucket` names the storage bucket that holds event logs, review packets and
    their pictures; `data_repo` names the dataset repository that receives accepted
    rows and the ledger. When either is None the run keeps that data in a local
    folder under `work_dir` instead (used for tests and dry runs).
    """

    data_repo: str | None = None
    bucket: str | None = None
    work_dir: str = "state_local"
    price_table: str | None = None
    pool_version: str = "0"
    routes: Mapping[str, RouteConfig] = field(default_factory=dict)
    sources: Mapping[str, SourceConfig] = field(default_factory=dict)
    commit: CommitConfig = field(default_factory=CommitConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    time_budget_seconds: float | None = None
    poll_seconds: float = 30.0
    max_attempts: int = 2
    stop_file: str | None = "STOP"
    remote_stop_path: str | None = "control/STOP"
    events_max_bytes: int = 2 * 1024 * 1024
    wait_for_review: bool = False
    tasks_file: str | None = None

    def route(self, name: str) -> RouteConfig:
        try:
            return self.routes[name]
        except KeyError:
            raise ConfigError(f"unknown route: {name!r}") from None

    def source(self, name: str) -> SourceConfig:
        """Settings for a source; a source with no entry gets the defaults."""
        return self.sources.get(name, SourceConfig())

    def work_path(self, *parts: str) -> Path:
        return Path(self.work_dir).joinpath(*parts)

    def stop_file_path(self) -> Path | None:
        if not self.stop_file:
            return None
        path = Path(self.stop_file)
        return path if path.is_absolute() else self.work_path(self.stop_file)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_repo": self.data_repo,
            "bucket": self.bucket,
            "work_dir": self.work_dir,
            "price_table": self.price_table,
            "pool_version": self.pool_version,
            "routes": {name: self.routes[name].to_dict() for name in sorted(self.routes)},
            "sources": {name: self.sources[name].to_dict() for name in sorted(self.sources)},
            "commit": self.commit.to_dict(),
            "review": self.review.to_dict(),
            "time_budget_seconds": self.time_budget_seconds,
            "poll_seconds": self.poll_seconds,
            "max_attempts": self.max_attempts,
            "stop_file": self.stop_file,
            "remote_stop_path": self.remote_stop_path,
            "events_max_bytes": self.events_max_bytes,
            "wait_for_review": self.wait_for_review,
            "tasks_file": self.tasks_file,
        }

    def digest(self) -> str:
        """Short stable hash of the configuration, recorded when a run starts."""
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()[:12]

    def problems(self) -> list[str]:
        """Pre-flight notes: things that are legal but probably not intended."""
        notes: list[str] = []
        for name, route in self.routes.items():
            if route.budget_usd <= 0:
                notes.append(f"route {name}: total budget is $0, so every call with a cost will be refused")
            for purpose in PURPOSES:
                if route.cap_for(purpose) <= 0:
                    notes.append(f"route {name}: no cap for purpose '{purpose}' (calls with that purpose are refused)")
            total = sum(route.cap_for(purpose) for purpose in PURPOSES)
            if total > route.budget_usd + 1e-9 and route.budget_usd > 0:
                notes.append(
                    f"route {name}: purpose caps add up to ${total:.2f}, above the route total ${route.budget_usd:.2f}"
                    " (the route total still applies)"
                )
        return notes

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        if not isinstance(data, Mapping):
            _fail("config: expected an object")
        _reject_secret_values(data)
        _check_keys(data, _TOP_KEYS, "config")
        default = cls()
        routes_in = data.get("routes", {})
        sources_in = data.get("sources", {})
        if not isinstance(routes_in, Mapping):
            _fail("routes: expected an object")
        if not isinstance(sources_in, Mapping):
            _fail("sources: expected an object")
        budget = data.get("time_budget_seconds", default.time_budget_seconds)
        remote_stop = data.get("remote_stop_path", default.remote_stop_path)
        if remote_stop is not None:
            try:
                remote_stop = normalize_path(remote_stop)
            except PathError as error:
                raise ConfigError(f"remote_stop_path: {error}") from None
        return cls(
            data_repo=_text(data.get("data_repo"), "data_repo", allow_none=True),
            bucket=_text(data.get("bucket"), "bucket", allow_none=True),
            work_dir=_text(data.get("work_dir", default.work_dir), "work_dir"),
            price_table=_text(data.get("price_table"), "price_table", allow_none=True),
            pool_version=str(_text(str(data.get("pool_version", default.pool_version)), "pool_version")),
            routes={name: RouteConfig.from_dict(name, spec) for name, spec in routes_in.items()},
            sources={name: SourceConfig.from_dict(name, spec) for name, spec in sources_in.items()},
            commit=CommitConfig.from_dict(data.get("commit", {})),
            review=ReviewConfig.from_dict(data.get("review", {})),
            time_budget_seconds=None if budget is None else float(_number(budget, "time_budget_seconds", minimum=1)),
            poll_seconds=float(_number(data.get("poll_seconds", default.poll_seconds), "poll_seconds", minimum=0)),
            max_attempts=_number(data.get("max_attempts", default.max_attempts), "max_attempts", minimum=1, integer=True),
            stop_file=_text(data.get("stop_file", default.stop_file), "stop_file", allow_none=True),
            remote_stop_path=remote_stop,
            events_max_bytes=_number(
                data.get("events_max_bytes", default.events_max_bytes), "events_max_bytes", minimum=1024, integer=True
            ),
            wait_for_review=_flag(data.get("wait_for_review", default.wait_for_review), "wait_for_review"),
            tasks_file=_text(data.get("tasks_file"), "tasks_file", allow_none=True),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        """Load a configuration from a JSON file."""
        try:
            text = Path(path).read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"cannot read config file {str(path)!r}: {error}") from None
        return cls.from_dict(data)


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{where}: expected true or false, got {value!r}")
    return value
