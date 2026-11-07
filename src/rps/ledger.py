"""Usage, cost and budgets.

Every call carries a purpose: test, pricing, probe, rebound or production. Each
route has a total dollar cap and one cap per purpose. Before a call is submitted
its estimated cost is checked against both its (route, purpose) cap and its route's
total; a call that would exceed either is refused and the refusal is recorded as a
`budget_refused` event with the numbers. A retry or re-submission after a failed or
rejected call is always purpose "rebound", whatever the original purpose was.

Money counted against a cap is what has been spent plus the estimated cost of calls
still in flight, so a batch that has been submitted but not yet returned holds its
share of the budget.

Prices come from a price table file. Every model's entry names its source page and
the date it was checked; batch and synchronous rates are separate; a model can have
rates that change on a known date, and rates that depend on a tier (DeepSeek's peak
and off-peak hours). Where a provider's pages do not say whether two discounts stack
(a batch discount and a cache price), the table carries the higher rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date as Date
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .config import PURPOSES, REBOUND, Config

if TYPE_CHECKING:  # pragma: no cover
    from .state import State

# Comparisons against a cap tolerate this much floating-point noise.
EPSILON = 1e-9
MILLION = 1_000_000
# A price change this close is worth a note.
CHANGE_NOTICE_DAYS = 30


class PriceError(ValueError):
    """The price table cannot price a model."""


@dataclass(frozen=True)
class Usage:
    """Token counts for one call, as disjoint billable buckets.

    input_tokens         input that was neither written to nor read from a cache
    cache_write_tokens   input written to a cache
    cache_read_tokens    input read from a cache
    output_tokens        visible output tokens, excluding reasoning
    reasoning_tokens     reasoning tokens, billed as output

    Adapters convert each provider's own counts into these buckets so nothing is
    counted twice.
    """

    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be an integer >= 0, got {value!r}")

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Usage":
        data = data or {}
        return cls(**{key: int(data.get(key, 0)) for key in cls().to_dict()})

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(*(a + b for a, b in zip(self._tuple(), other._tuple())))

    def _tuple(self) -> tuple[int, ...]:
        return (
            self.input_tokens,
            self.cache_write_tokens,
            self.cache_read_tokens,
            self.output_tokens,
            self.reasoning_tokens,
        )


def _rate(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PriceError(f"{where}: expected a rate of at least 0, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float

    @classmethod
    def from_dict(cls, data: Any, where: str) -> "Rates":
        if not isinstance(data, Mapping):
            raise PriceError(f"{where}: expected an object with input and output rates")
        unknown = sorted(set(data) - {"input", "output", "cache_write", "cache_read", "note"})
        if unknown:
            raise PriceError(f"{where}: unknown key(s) {unknown}")
        if "input" not in data or "output" not in data:
            raise PriceError(f"{where}: needs input and output rates")
        input_rate = _rate(data["input"], f"{where}.input")
        # A cache rate left out is taken as the plain input rate, which can only overstate a cost.
        return cls(
            input=input_rate,
            output=_rate(data["output"], f"{where}.output"),
            cache_write=_rate(data.get("cache_write", input_rate), f"{where}.cache_write"),
            cache_read=_rate(data.get("cache_read", input_rate), f"{where}.cache_read"),
        )

    def scaled(self, factor: float) -> "Rates":
        return Rates(self.input * factor, self.output * factor, self.cache_write * factor, self.cache_read * factor)

    @property
    def input_ceiling(self) -> float:
        """The highest rate any input token can cost (a cache write can cost more than plain input)."""
        return max(self.input, self.cache_write)


@dataclass(frozen=True)
class ModeRates:
    """A synchronous rate card and, when the provider publishes one, a batch rate card.

    Without an explicit batch card the table's batch discount applies to the synchronous one.
    """

    sync: Rates
    batch: Rates | None = None

    def for_mode(self, batch: bool, discount: float) -> Rates:
        if not batch:
            return self.sync
        return self.batch if self.batch is not None else self.sync.scaled(discount)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str) -> "ModeRates":
        sync_source = data["sync"] if "sync" in data else {k: v for k, v in data.items() if k in _RATE_KEYS}
        batch_source = data.get("batch")
        return cls(
            sync=Rates.from_dict(sync_source, f"{where}.sync"),
            batch=None if batch_source is None else Rates.from_dict(batch_source, f"{where}.batch"),
        )


_RATE_KEYS = {"input", "output", "cache_write", "cache_read"}
# `valid_until` is the older spelling of `until`.
_PERIOD_KEYS = {"sync", "batch", "tiers", "until", "valid_until", "note", "then"} | _RATE_KEYS


@dataclass(frozen=True)
class Period:
    """The rates in force until a date (inclusive); `tiers` are alternative cards by name."""

    rates: ModeRates
    tiers: Mapping[str, ModeRates]
    until: str | None = None
    note: str | None = None

    def card(self, tier: str | None) -> ModeRates:
        """The card for a tier. With tiers and no (or an unknown) tier, the dearest tier is used."""
        if not self.tiers:
            return self.rates
        if tier in self.tiers:
            return self.tiers[tier]
        return max(self.tiers.values(), key=lambda card: (card.sync.output, card.sync.input))


@dataclass(frozen=True)
class ModelPrice:
    """Everything the table knows about one model's prices.

    `periods` are in date order; the last one has no end. The first period's synchronous
    rates are also available as `input`, `output`, `cache_write` and `cache_read`.
    """

    periods: tuple[Period, ...]
    provider: str | None = None
    source: str | None = None
    checked: str | None = None
    note: str | None = None

    @property
    def input(self) -> float:
        return self.periods[0].rates.sync.input

    @property
    def output(self) -> float:
        return self.periods[0].rates.sync.output

    @property
    def cache_write(self) -> float:
        return self.periods[0].rates.sync.cache_write

    @property
    def cache_read(self) -> float:
        return self.periods[0].rates.sync.cache_read

    @property
    def valid_until(self) -> str | None:
        return self.periods[0].until

    def period_on(self, day: Date) -> Period:
        """The period in force on a day (the last one if every period has ended)."""
        for period in self.periods:
            if period.until is None or day <= Date.fromisoformat(period.until):
                return period
        return self.periods[-1]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str) -> "ModelPrice":
        if not isinstance(data, Mapping):
            raise PriceError(f"{where}: expected an object")
        unknown = sorted(set(data) - (_PERIOD_KEYS | {"provider", "source", "checked"}))
        if unknown:
            raise PriceError(f"{where}: unknown key(s) {unknown}")
        periods: list[Period] = []
        node: Mapping[str, Any] | None = data
        level = 0
        while node is not None:
            here = where if level == 0 else f"{where}.then"
            if level > 0:
                if not isinstance(node, Mapping):
                    raise PriceError(f"{here}: expected an object")
                unknown = sorted(set(node) - _PERIOD_KEYS)
                if unknown:
                    raise PriceError(f"{here}: unknown key(s) {unknown}")
            periods.append(_parse_period(node, here))
            node = node.get("then")
            level += 1
        for earlier in periods[:-1]:
            if earlier.until is None:
                raise PriceError(f"{where}: a period followed by `then` needs an `until` date")
        checked = data.get("checked")
        if checked is not None:
            try:
                Date.fromisoformat(str(checked))
            except ValueError:
                raise PriceError(f"{where}.checked: not a date: {checked!r}") from None
        return cls(
            periods=tuple(periods),
            provider=data.get("provider"),
            source=data.get("source"),
            checked=None if checked is None else str(checked),
            note=periods[0].note,
        )


def _parse_period(node: Mapping[str, Any], where: str) -> Period:
    until = node.get("until", node.get("valid_until"))
    if until is not None:
        try:
            Date.fromisoformat(str(until))
        except ValueError:
            raise PriceError(f"{where}.until: not a date: {until!r}") from None
    tiers_in = node.get("tiers", {})
    if not isinstance(tiers_in, Mapping):
        raise PriceError(f"{where}.tiers: expected an object")
    tiers = {name: ModeRates.from_dict(card, f"{where}.tiers.{name}") for name, card in tiers_in.items()}
    if tiers and not (set(node) & ({"sync", "batch"} | _RATE_KEYS)):
        # A model priced only by tiers needs no base card; the dearest tier stands in for it.
        base = max(tiers.values(), key=lambda card: (card.sync.output, card.sync.input))
    else:
        base = ModeRates.from_dict(node, where)
    return Period(rates=base, tiers=tiers, until=None if until is None else str(until), note=node.get("note"))


@dataclass(frozen=True)
class PriceTable:
    """Per-model prices, with the date the table was checked.

    `batch_discount` multiplies the synchronous rates of a model that has no batch card.
    """

    date: str
    models: Mapping[str, ModelPrice]
    batch_discount: float = 0.5

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PriceTable":
        try:
            table_date = str(data["date"])
            Date.fromisoformat(table_date)
            models = {name: ModelPrice.from_dict(entry, f"models.{name}") for name, entry in data["models"].items()}
            discount = float(data.get("batch_discount", 0.5))
        except PriceError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise PriceError(f"invalid price table: {error}") from None
        if not 0 < discount <= 1:
            raise PriceError("batch_discount must be in (0, 1]")
        return cls(date=table_date, models=models, batch_discount=discount)

    @classmethod
    def load(cls, path: str | Path) -> "PriceTable":
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise PriceError(f"cannot read price table {str(path)!r}: {error}") from None

    @classmethod
    def default(cls) -> "PriceTable":
        """The price table shipped with the package."""
        text = resources.files("rps").joinpath("data/prices.json").read_text(encoding="utf-8")
        return cls.from_dict(json.loads(text))

    def price(self, model: str) -> ModelPrice:
        try:
            return self.models[model]
        except KeyError:
            raise PriceError(f"no price for model {model!r} in the price table dated {self.date}") from None

    def rates(self, model: str, *, batch: bool = False, tier: str | None = None, on: Date | None = None) -> Rates:
        """The per-million-token rates for a model on a day (today when omitted)."""
        period = self.price(model).period_on(on or Date.today())
        return period.card(tier).for_mode(batch, self.batch_discount)

    def cost(
        self, model: str, usage: Usage, *, batch: bool = False, tier: str | None = None, on: Date | None = None
    ) -> float:
        """Dollar cost of a call's usage."""
        rates = self.rates(model, batch=batch, tier=tier, on=on)
        dollars = (
            usage.input_tokens * rates.input
            + usage.cache_write_tokens * rates.cache_write
            + usage.cache_read_tokens * rates.cache_read
            + (usage.output_tokens + usage.reasoning_tokens) * rates.output
        ) / MILLION
        return round(dollars, 8)

    def estimate(
        self, model: str, input_tokens: int, max_output_tokens: int, *, batch: bool = False, on: Date | None = None
    ) -> float:
        """Upper bound on a call's cost.

        Every input token is priced at the highest input-side rate (a cache write can cost
        more than plain input) and output is counted up to its limit. A model with tiers is
        priced at its dearest tier.
        """
        period = self.price(model).period_on(on or Date.today())
        cards = list(period.tiers.values()) or [period.rates]
        dollars = 0.0
        for card in cards:
            rates = card.for_mode(batch, self.batch_discount)
            dollars = max(
                dollars,
                (max(0, input_tokens) * rates.input_ceiling + max(0, max_output_tokens) * rates.output) / MILLION,
            )
        return round(dollars, 8)

    def warnings(self, today: Date | None = None, max_age_days: int = 30) -> list[str]:
        """Notes about a stale table, prices past their end date and prices about to change."""
        today = today or Date.today()
        notes = []
        age = (today - Date.fromisoformat(self.date)).days
        if age > max_age_days:
            notes.append(f"price table is {age} days old (dated {self.date})")
        for name, price in sorted(self.models.items()):
            current = price.period_on(today)
            if current.until is None:
                continue
            ends = Date.fromisoformat(current.until)
            if ends < today:
                notes.append(f"price for {name} was valid until {current.until}")
            elif (ends - today).days <= CHANGE_NOTICE_DAYS and current is not price.periods[-1]:
                notes.append(f"price for {name} changes after {current.until}")
            elif (ends - today).days <= CHANGE_NOTICE_DAYS:
                notes.append(f"price for {name} is only given until {current.until}")
        return notes


def effective_purpose(task_purpose: str, attempt: int) -> str:
    """The purpose a call is charged to: any retry (attempt 2 or later) is "rebound"."""
    if task_purpose not in PURPOSES:
        raise ValueError(f"unknown purpose: {task_purpose!r}")
    return REBOUND if attempt > 1 else task_purpose


@dataclass(frozen=True)
class BudgetDecision:
    """The result of checking a call against its caps."""

    ok: bool
    route: str
    purpose: str
    est_usd: float
    route_spent: float
    route_cap: float
    purpose_spent: float
    purpose_cap: float
    reason: str | None = None  # "purpose_cap" or "route_cap" when refused

    def numbers(self) -> dict[str, Any]:
        """The figures recorded in a budget_refused event."""
        return {
            "route": self.route,
            "purpose": self.purpose,
            "est_usd": self.est_usd,
            "reason": self.reason,
            "route_spent": round(self.route_spent, 8),
            "route_cap": self.route_cap,
            "purpose_spent": round(self.purpose_spent, 8),
            "purpose_cap": self.purpose_cap,
        }

    def message(self) -> str:
        if self.ok:
            return "within budget"
        if self.reason == "purpose_cap":
            return (
                f"{self.route}/{self.purpose}: estimated ${self.est_usd:.4f} on top of ${self.purpose_spent:.4f} spent "
                f"would pass the ${self.purpose_cap:.2f} cap for purpose '{self.purpose}'"
            )
        return (
            f"{self.route}: estimated ${self.est_usd:.4f} on top of ${self.route_spent:.4f} spent "
            f"would pass the ${self.route_cap:.2f} total for the route"
        )


class Ledger:
    """Prices calls and enforces the per-route and per-purpose budgets."""

    def __init__(self, config: Config, prices: PriceTable) -> None:
        self.config = config
        self.prices = prices

    # -- pricing

    def is_batch(self, route: str) -> bool:
        return self.config.route(route).kind == "batch"

    def estimate(self, route: str, est_input_tokens: int, max_output_tokens: int, *, on: Date | None = None) -> float:
        route_config = self.config.route(route)
        return self.prices.estimate(
            route_config.model, est_input_tokens, max_output_tokens, batch=route_config.kind == "batch", on=on
        )

    def cost(self, route: str, usage: Usage, *, tier: str | None = None, on: Date | None = None) -> float:
        route_config = self.config.route(route)
        return self.prices.cost(route_config.model, usage, batch=route_config.kind == "batch", tier=tier, on=on)

    def validate_prices(self) -> None:
        """Raise PriceError unless every route's model has a price."""
        for name, route in self.config.routes.items():
            if route.model not in self.prices.models:
                raise PriceError(f"route {name!r}: no price for model {route.model!r} (table dated {self.prices.date})")

    # -- budgets

    def check(self, state: "State", route: str, purpose: str, est_usd: float) -> BudgetDecision:
        """Check a call against its (route, purpose) cap and its route's total."""
        route_config = self.config.route(route)
        purpose_cap = route_config.cap_for(purpose)
        route_cap = route_config.budget_usd
        purpose_spent = state.spend(route, purpose).total
        route_spent = state.spend(route).total
        reason = None
        if purpose_spent + est_usd > purpose_cap + EPSILON:
            reason = "purpose_cap"
        elif route_spent + est_usd > route_cap + EPSILON:
            reason = "route_cap"
        return BudgetDecision(
            ok=reason is None,
            route=route,
            purpose=purpose,
            est_usd=est_usd,
            route_spent=route_spent,
            route_cap=route_cap,
            purpose_spent=purpose_spent,
            purpose_cap=purpose_cap,
            reason=reason,
        )

    def route_stopped(self, state: "State", route: str) -> bool:
        """True once a route's total budget is spent."""
        cap = self.config.route(route).budget_usd
        return cap > 0 and state.spend(route).total >= cap - EPSILON

    # -- reporting

    def status_rows(self, state: "State") -> list[dict[str, Any]]:
        """One row per (route, purpose) with spend against its cap, plus a total row per route."""
        rows = []
        for name in sorted(self.config.routes):
            route = self.config.routes[name]
            for purpose in PURPOSES:
                spend = state.spend(name, purpose)
                cap = route.cap_for(purpose)
                rows.append(
                    {
                        "route": name,
                        "purpose": purpose,
                        "spent": round(spend.actual, 6),
                        "reserved": round(spend.reserved, 6),
                        "cap": cap,
                        "remaining": round(cap - spend.total, 6),
                        "calls": spend.calls,
                    }
                )
            total = state.spend(name)
            rows.append(
                {
                    "route": name,
                    "purpose": "(route total)",
                    "spent": round(total.actual, 6),
                    "reserved": round(total.reserved, 6),
                    "cap": route.budget_usd,
                    "remaining": round(route.budget_usd - total.total, 6),
                    "calls": total.calls,
                }
            )
        return rows

    def format_status(self, state: "State") -> str:
        """A fixed-width table of spend per (route, purpose) against its cap."""
        header = f"{'route':<22}{'purpose':<15}{'spent':>10}{'in flight':>11}{'cap':>10}{'left':>10}{'calls':>7}"
        lines = [header, "-" * len(header)]
        for row in self.status_rows(state):
            lines.append(
                f"{row['route'][:21]:<22}{row['purpose']:<15}"
                f"{_money(row['spent']):>10}{_money(row['reserved']):>11}{_money(row['cap']):>10}"
                f"{_money(row['remaining']):>10}{row['calls']:>7}"
            )
        lines.append(f"prices dated {self.prices.date}")
        return "\n".join(lines)

    def task_lines(self, state: "State") -> list[dict[str, Any]]:
        """One usage record per (task, route): tokens, dollars, attempts, minutes and row counts."""
        lines: dict[tuple[str, str], dict[str, Any]] = {}
        for call in state.calls.values():
            task = state.tasks.get(call.task_id)
            key = (call.task_id, call.route)
            entry = lines.setdefault(
                key,
                {
                    "task_id": call.task_id,
                    "route": call.route,
                    "model": self.config.routes[call.route].model if call.route in self.config.routes else "",
                    "effort": task.effort if task else None,
                    "calls": 0,
                    "attempts": 0,
                    "input_tokens": 0,
                    "cache_write_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "usd": 0.0,
                    "usd_by_purpose": {},
                    "price_table_date": None,
                    "minutes": 0.0,
                    "rows_out": 0,
                    "rows_passed": 0,
                    "rows_accepted": 0,
                    "_lines": set(),
                },
            )
            entry["_lines"].add(call.line)
            if call.status in ("returned", "failed"):
                entry["attempts"] += 1
                for name, value in call.usage.items():
                    if name in entry:
                        entry[name] += int(value)
                entry["usd"] += call.usd
                entry["usd_by_purpose"][call.purpose] = entry["usd_by_purpose"].get(call.purpose, 0.0) + call.usd
                entry["minutes"] += call.minutes
                if call.price_table_date and (entry["price_table_date"] or "") < call.price_table_date:
                    entry["price_table_date"] = call.price_table_date
        for row in state.rows.values():
            entry = lines.get((row.task_id, row.route))
            if entry is None:
                continue
            entry["rows_out"] += 1
            if row.status in ("passed", "in_review", "in_question", "hand", "accepted"):
                entry["rows_passed"] += 1
            if row.status == "accepted":
                entry["rows_accepted"] += 1
        result = []
        for key in sorted(lines):
            entry = lines[key]
            entry["calls"] = len(entry.pop("_lines"))
            entry["usd"] = round(entry["usd"], 8)
            entry["usd_by_purpose"] = {k: round(v, 8) for k, v in sorted(entry["usd_by_purpose"].items())}
            entry["minutes"] = round(entry["minutes"], 4)
            result.append(entry)
        return result

    def summary(self, state: "State") -> dict[str, Any]:
        """Spend per route and purpose against caps, with the price table's date."""
        return {
            "price_table_date": self.prices.date,
            "routes": {
                name: {
                    "budget_usd": route.budget_usd,
                    "spent_usd": round(state.spend(name).actual, 8),
                    "in_flight_usd": round(state.spend(name).reserved, 8),
                    "purposes": {
                        purpose: {
                            "cap_usd": route.cap_for(purpose),
                            "spent_usd": round(state.spend(name, purpose).actual, 8),
                            "calls": state.spend(name, purpose).calls,
                        }
                        for purpose in PURPOSES
                    },
                }
                for name, route in sorted(self.config.routes.items())
            },
        }


def _money(value: float) -> str:
    return f"${value:,.4f}" if abs(value) < 1000 else f"${value:,.2f}"
