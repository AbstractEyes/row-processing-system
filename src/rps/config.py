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
class ReviewerConfig:
    """One reviewer: an id (used in paths) and the model family it belongs to."""

    id: str
    family: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "family": self.family, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str) -> "ReviewerConfig":
        if not isinstance(data, Mapping):
            _fail(f"{where}: expected an object with 'id' and 'family'")
        _check_keys(data, {"id", "family", "enabled"}, where)
        for key in ("id", "family"):
            value = data.get(key)
            if not isinstance(value, str) or not NAME_PATTERN.match(value):
                _fail(f"{where}.{key}: expected letters, digits, '.', '_' or '-', got {value!r}")
        return cls(id=data["id"], family=data["family"], enabled=_flag(data.get("enabled", True), f"{where}.enabled"))


@dataclass(frozen=True)
class ReviewPolicy:
    """How reviewers' verdicts settle a row.

    A first review goes to a reviewer from a family other than the writer's. A row
    with a "keep" verdict and no assessor flag is accepted at once (when
    `first_keep_accepts` is on). A row with a "fix" or "reject" verdict, or an
    assessor flag, is in question and goes to a further reviewer from a family not
    yet used on it. `keeps_to_accept` keeps accept it and `rejects_to_discard`
    rejects discard it. After `max_reviewers` reviewers with no settlement it goes
    to the owner's hand queue. `audit_fraction` of accepted rows get one more review
    from another family; the audit changes nothing unless it rejects the row.
    """

    keeps_to_accept: int = 2
    rejects_to_discard: int = 2
    max_reviewers: int = 3
    first_keep_accepts: bool = True
    audit_fraction: float = 0.10
    audit_seed: str = "audit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "keeps_to_accept": self.keeps_to_accept,
            "rejects_to_discard": self.rejects_to_discard,
            "max_reviewers": self.max_reviewers,
            "first_keep_accepts": self.first_keep_accepts,
            "audit_fraction": self.audit_fraction,
            "audit_seed": self.audit_seed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewPolicy":
        where = "review.policy"
        if not isinstance(data, Mapping):
            _fail(f"{where}: expected an object")
        _check_keys(
            data,
            {"keeps_to_accept", "rejects_to_discard", "max_reviewers", "first_keep_accepts", "audit_fraction", "audit_seed"},
            where,
        )
        default = cls()
        policy = cls(
            keeps_to_accept=_number(
                data.get("keeps_to_accept", default.keeps_to_accept), f"{where}.keeps_to_accept", minimum=1, integer=True
            ),
            rejects_to_discard=_number(
                data.get("rejects_to_discard", default.rejects_to_discard),
                f"{where}.rejects_to_discard",
                minimum=1,
                integer=True,
            ),
            max_reviewers=_number(
                data.get("max_reviewers", default.max_reviewers), f"{where}.max_reviewers", minimum=1, integer=True
            ),
            first_keep_accepts=_flag(data.get("first_keep_accepts", default.first_keep_accepts), f"{where}.first_keep_accepts"),
            audit_fraction=float(
                _number(data.get("audit_fraction", default.audit_fraction), f"{where}.audit_fraction", minimum=0, maximum=1)
            ),
            audit_seed=str(_text(str(data.get("audit_seed", default.audit_seed)), f"{where}.audit_seed")),
        )
        if policy.max_reviewers < min(policy.keeps_to_accept, policy.rejects_to_discard):
            _fail(f"{where}.max_reviewers: too small to ever settle a row")
        return policy


# Which model family a provider's output belongs to, for choosing reviewers.
DEFAULT_PROVIDER_FAMILIES: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "google",
    "google": "google",
    "deepseek": "deepseek",
    "human": "human",
    "fake": "fake",
}


@dataclass(frozen=True)
class ReviewConfig:
    """Reviewers, their families, how rows are grouped into packets, and the policy."""

    packet_size: int = 20
    brief_version: str = "1"
    reviewers: tuple[ReviewerConfig, ...] = ()
    policy: ReviewPolicy = field(default_factory=ReviewPolicy)
    provider_families: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_PROVIDER_FAMILIES))

    def family_of(self, provider: str) -> str:
        """The model family of a writer's provider (the provider itself if unmapped)."""
        return self.provider_families.get(provider, provider)

    def enabled_reviewers(self) -> tuple[ReviewerConfig, ...]:
        return tuple(reviewer for reviewer in self.reviewers if reviewer.enabled)

    def reviewer(self, reviewer_id: str) -> ReviewerConfig | None:
        for reviewer in self.reviewers:
            if reviewer.id == reviewer_id:
                return reviewer
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_size": self.packet_size,
            "brief_version": self.brief_version,
            "reviewers": [reviewer.to_dict() for reviewer in self.reviewers],
            "policy": self.policy.to_dict(),
            "provider_families": {key: self.provider_families[key] for key in sorted(self.provider_families)},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewConfig":
        where = "review"
        if not isinstance(data, Mapping):
            _fail(f"{where}: expected an object")
        _check_keys(data, {"packet_size", "brief_version", "reviewers", "policy", "provider_families"}, where)
        reviewers_in = data.get("reviewers", [])
        if not isinstance(reviewers_in, (list, tuple)):
            _fail(f"{where}.reviewers: expected a list")
        reviewers = tuple(
            ReviewerConfig.from_dict(item, f"{where}.reviewers[{index}]") for index, item in enumerate(reviewers_in)
        )
        ids = [reviewer.id for reviewer in reviewers]
        if len(set(ids)) != len(ids):
            _fail(f"{where}.reviewers: duplicate reviewer ids")
        families = dict(DEFAULT_PROVIDER_FAMILIES)
        extra = data.get("provider_families", {})
        if not isinstance(extra, Mapping):
            _fail(f"{where}.provider_families: expected an object")
        for provider, family in extra.items():
            if not isinstance(family, str) or not NAME_PATTERN.match(family):
                _fail(f"{where}.provider_families.{provider}: invalid family {family!r}")
            families[str(provider)] = family
        return cls(
            packet_size=_number(data.get("packet_size", 20), f"{where}.packet_size", minimum=1, integer=True),
            brief_version=str(_text(str(data.get("brief_version", "1")), f"{where}.brief_version")),
            reviewers=reviewers,
            policy=ReviewPolicy.from_dict(data.get("policy", {})),
            provider_families=families,
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
