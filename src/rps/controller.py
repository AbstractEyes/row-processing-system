"""The controller: the loop that turns spec lines into reviewed, shipped rows.

One pass (`step`) runs these stages in order. Every stage works out what to do from
the state rebuilt from events and records what it did as events, so a stage that is
interrupted and run again repeats only the unfinished part.

    plan       tasks from the planner become task_created events
    submit     due calls are prepared (budget checked first) and submitted as batches; a spec
               line the rule set's screen names a problem with is set aside before its first
               call (line_screened) and no call is made for it
    poll       submitted batches are polled; finished ones are collected into rows
    assess     each written row goes through the assess function
    review     verdicts are read, packets addressed to a reviewer who is switched off
               are taken back, rows are settled by the review policy, the owner's
               hand decisions are read, rows that went to the hand queue only for want
               of a reviewer are reopened when one is now available and the policy
               still asks for their review, rows whose repair came to nothing go back
               to the hand queue, rows the reviewers sent back with a fix verdict are
               sent to the route that wrote them, or the route `repair.routes` names
               for it, to be written again (when `repair` is on: see `rps.repair`),
               rows that need a reviewer are published in
               packets, and the pictures of answered rows are burned
    ship       accepted rows and the ledger go to the ship store

`run` repeats passes until there is nothing left to do, a stop file appears, the time
budget runs out or the run is interrupted. It never ends a run by crashing in the
middle of a write that matters: a batch id is made durable before the controller
waits for it, packet files are durable before the event that announces them, and
shipped events are written only after the files they describe are durable (for a ship
store that sends in batches, after the store has sent them).

Reviewers see a blind view of each row; the controller keeps the row to writer
mapping in its events and matches verdicts back by row id.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import __version__, ids, layout
from .assess import AssessCallFn, AssessContext, AssessFn, Assessment, default_assess
from .config import PURPOSES, Config, ConfigError, ReviewerConfig, RouteConfig
from .events import EventLog, Event, replay, utc_now
from .failures import (
    KIND_BAD_REQUEST,
    KIND_CANCELED,
    KIND_EXPIRED,
    KIND_INVALID_OUTPUT,
    KIND_LONG_PROMPT,
    KIND_MISMATCH,
    KIND_PROVIDER,
    KIND_UNEXPECTED,
    PROVIDER_ERROR_CHARS,
    STOP_ROUTE_KINDS,
)
from .images import ImageError, load_picture
from .keys import redact
from .layout import controller_actor
from .ledger import Ledger, PriceError, PriceTable, effective_purpose
from .prompting import (
    DEFAULT_PROFILE,
    ScreenFn,
    WriterRules,
    assignment_data,
    assignment_for,
    load_writer_rules,
    profile_rules,
    render_task_text,
)
from .provenance import (
    ProvenanceError,
    SourceLabel,
    WriterLabel,
    reserved_keys_in,
    rows_to_lines,
    stamp_human_row,
    stamp_row,
)
from .providers.base import CallResult, CallSpec, Handle, Request, RouteRejected, RouteUnavailable, Writer
from .repair import candidates as repair_candidates
from .repair import failure_reason as repair_failure_reason
from .repair import route_may_repair
from .review.packets import build_packet, parse_verdict_lines, publish_packet, validate_verdict
from .review.policy import (
    ACCEPT,
    DISCARD,
    FURTHER,
    HAND,
    NEED_FIRST,
    NO_FIRST_REVIEWER,
    NO_FURTHER_REVIEWER,
    REOPEN_REASONS,
    Settlement,
    audit_selected,
    pick_first_reviewer,
    pick_further_reviewer,
    settle,
)
from .routing import ImageRoutingError, allowed_image, check_image_allowed
from .state import LineState, PacketState, RowState, State, TaskState
from .store.base import ActorStore, NotFound, OwnershipError, RateLimitedError, Store, StoreError
from .store.local import LocalStore
from .store.remote import describe_error
from .tasks import FilePlanner, Planner, StorePlanner, TaskSpec

ImageLoader = Callable[[Any, Any], "tuple[bytes, str, int]"]
HAND_DECISIONS = ("accept", "discard", "replace", "review")

# A tasks file in a store behind a network is read at most this often (a local folder, on every pass).
TASKS_CHECK_SECONDS = 15.0
# A row that could not be converted is kept whole in the log of the failed call up to this many characters.
UNCONVERTED_ROW_CHARS = 4000
UNUSABLE_PREFIX = "unusable output: "
ROW_ADVICE_LEAD = " Row by row:"
MIN_ADVICE_CHARS = 60


class ControllerError(RuntimeError):
    """The controller cannot go on (a conflicting task, or a loop that makes no progress)."""


class TaskConflict(ControllerError):
    """A task with the same id was already recorded with a different route or purpose."""


@dataclass
class RunSummary:
    reason: str
    steps: int
    events: int
    seconds: float
    progress: dict[str, int]
    parked_by_budget: int
    hand_queue: int
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------- helpers


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def format_progress(
    *,
    lines_total: int,
    lines_done: int,
    rows: int,
    elapsed: float | None,
    budget: float | None,
    spent_usd: float | None = None,
    held_usd: float | None = None,
    waiting: Mapping[str, int] | None = None,
) -> str:
    """One progress line: job size, done, time spent and time left.

    With `elapsed` of None (a status print that belongs to no run) the time parts are left out.
    `spent_usd` is money paid; `held_usd` is money held for calls still in flight, shown beside
    it when there is any, so the two are never read as one.
    """
    percent = 100.0 * lines_done / lines_total if lines_total else 0.0
    parts = [f"job {lines_total} lines", f"done {lines_done} ({percent:.0f}%)", f"rows {rows}"]
    if elapsed is not None:
        parts.append(f"time spent {format_duration(elapsed)}")
        parts.append(f"time left {format_duration(budget - elapsed)}" if budget is not None else "time left no limit")
        if lines_done and lines_total > lines_done:
            parts.append(f"est. {format_duration(elapsed * (lines_total - lines_done) / lines_done)} to finish")
    if spent_usd is not None:
        parts.append(f"${spent_usd:.4f} spent")
    if held_usd:
        parts.append(f"${held_usd:.4f} held for calls in flight")
    busy =[f"{count} {name}" for name, count in (waiting or {}).items() if count]
    if busy:
        parts.append("waiting: " + ", ".join(busy))
    return " | ".join(parts)


def is_wrapper(item: Mapping[str, Any]) -> bool:
    """True for a writer's answer wrapper: a skip flag next to a list of rows.

    A rule set's `normalize` function turns the wrapper into a list of stored rows. A wrapper
    that is still one when the output is read was not converted, so it is not a row.
    """
    return "skip" in item and isinstance(item.get("rows"), list)


def parse_output(
    text: str | None, explain_wrapper: Callable[[Sequence[Mapping[str, Any]]], str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Split a model's output into (rows, skip objects, problem).

    The output is a JSON object or a list of objects; an object with "skip": true is a
    skip object, any other object is a row, except a writer's wrapper that was not turned into
    rows (a skip flag with a list of rows, see `is_wrapper`), which is a problem: the answer is
    never stored whole as one row. `explain_wrapper` is handed the wrappers found and returns the
    problem text for them. A leading code fence is tolerated.
    """
    if text is None or not text.strip():
        return [], [], "the output is empty"
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        return [], [], f"the output is not valid JSON ({error.msg})"
    items = data if isinstance(data, list) else [data]
    rows: list[dict[str, Any]] = []
    skips: list[dict[str, Any]] = []
    wrappers: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return [], [], "the output holds something other than JSON objects"
        if item.get("skip") is True:
            skips.append(item)
        elif is_wrapper(item):
            wrappers.append(item)
        else:
            rows.append(item)
    if wrappers:
        if explain_wrapper is not None:
            return [], [], explain_wrapper(wrappers)
        count = sum(len(wrapper["rows"]) for wrapper in wrappers)
        return [], [], f"the answer is the writer's wrapper holding {count} rows, and it was not turned into rows"
    if not rows and not skips:
        return [], [], "the output holds no row"
    return rows, skips, None


def _cap_money(value: Any) -> str:
    """Dollars as a cap is read: cents for an amount of ten cents or more, four places below that."""
    amount = float(value) if isinstance(value, (int, float)) else 0.0
    return f"${amount:.2f}" if amount >= 0.1 else f"${amount:.4f}"


def _cap_name(reason: Any, route: Any, purpose: Any, purpose_cap: Any, route_cap: Any) -> str:
    """A cap as people read it: "the pricing cap of deepseek-sync ($0.40)", or "the total of deepseek-sync ($5.00)"."""
    if reason == "purpose_cap":
        return f"the {purpose} cap of {route} ({_cap_money(purpose_cap)})"
    if reason == "route_cap":
        return f"the total of {route} ({_cap_money(route_cap)})"
    return "a budget"


def _converts_alone(normalize: Callable[..., Any], wrapper: Mapping[str, Any], row: Any, spec: Any, model: str) -> bool:
    """True when one row of a wrapper, handed to a `normalize` function in a wrapper of its own, comes back as rows."""
    alone = json.dumps({**wrapper, "rows": [row]}, ensure_ascii=False)
    try:
        converted = normalize(alone, spec, model)
    except Exception:  # a rule function must not stop the run
        return False
    return converted is not None and parse_output(converted)[2] is None


def _fit_lines(lines: Sequence[Any], room: int) -> str:
    """Lines as an indented list that takes at most `room` characters; empty when not even one line is worth showing.

    Whole lines go in while they fit and "and N more" stands for the rest. When the first line alone is
    too long it is cut short, keeping its start (the row and its first faults).
    """
    wanted = [" ".join(str(line).split()) for line in lines]
    wanted = [line for line in wanted if line]
    block = ""
    shown = 0
    for index, line in enumerate(wanted):
        rest = len(wanted) - index - 1
        entry = "\n  " + line
        tail = f"\n  and {rest} more" if rest else ""
        if len(block) + len(entry) + len(tail) <= room:
            block += entry
            shown += 1
            continue
        if shown == 0:
            rest = len(wanted) - 1
            keep = room - len("\n  ") - len(" ...") - (len(f"\n  and {rest} more") if rest else 0)
            if keep >= MIN_ADVICE_CHARS:
                block = "\n  " + line[:keep].rstrip() + " ..."
                shown = 1
        break
    if shown == 0:
        return ""
    if shown < len(wanted):
        block += f"\n  and {len(wanted) - shown} more"
    return block


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _parse_moment(value: Any) -> datetime | None:
    """A UTC moment from ISO text (with or without fractions of a second); None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ---------------------------------------------------------------------------- the controller


class Controller:
    """Runs the pipeline. See the module description.

    `state_store` holds event logs, packets, pictures and verdicts; `ship_store` (the
    same store when omitted) receives accepted rows and the ledger. `writers` maps each
    configured route to its writer. `assess`, `assess_call` and `planner` are the
    pluggable hooks: `assess` judges one row, `assess_call` judges all the rows one call
    returned together (and wins when both are given). `screen` reads a spec line before its
    first call and returns the reason to set the line aside with no call, or None (it is
    never asked about a retry); when omitted it is the rules' own, if they bring one.
    `rules` are the writing rules (system prompt, row schema, version); when omitted they
    are read from the configuration's `rules` section if it has one. A route that names a prompt
    profile (`profile` in its settings) writes with that profile's rules: its system prompt, task
    text, retry block, format block and schema form; the checker, the screen, the per-call choices
    and the answer's form stay those of `rules`. A profile the rules do not have is a ConfigError
    here. `clock` (monotonic seconds), `now`
    (UTC datetime) and `sleep` can be replaced in tests; `on_event` is called after
    every event is written and applied. Without a `sleep`, the pause between passes ends
    early when a one-call route finishes a call, so its free worker is given the next one
    at once. `tasks_check_seconds` sets how often a tasks file
    in the state store ("store:<path>") is read: by default on every pass for a local folder
    and at most every 15 seconds for a store behind a network.
    """

    def __init__(
        self,
        config: Config,
        state_store: Store,
        writers: Mapping[str, Writer],
        *,
        ship_store: Store | None = None,
        prices: PriceTable | None = None,
        assess: AssessFn | None = None,
        assess_call: AssessCallFn | None = None,
        screen: ScreenFn | None = None,
        rules: WriterRules | None = None,
        planner: Planner | None = None,
        image_loader: ImageLoader | None = None,
        instance: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] | None = None,
        printer: Callable[[str], Any] = print,
        on_event: Callable[[Event], None] | None = None,
        progress_seconds: float = 30.0,
        stop_check_seconds: float = 15.0,
        max_consecutive_failures: int = 5,
        tasks_check_seconds: float | None = None,
    ) -> None:
        self.config = config
        self._now = now
        self.instance = instance or ids.instance_id(now())
        self.actor = controller_actor(self.instance)
        self.state_store: Store = ActorStore(state_store, self.actor)
        self.ship_store: Store = ActorStore(ship_store if ship_store is not None else state_store, self.actor)
        self.writers = dict(writers)
        self.prices = prices or (PriceTable.load(config.price_table) if config.price_table else PriceTable.default())
        self.ledger = Ledger(config, self.prices)
        self.rules: WriterRules | None = rules if rules is not None else (
            load_writer_rules(config.rules) if config.rules is not None else None
        )
        # The rules of each prompt profile a route names (one object per profile), and each route's own.
        self.profiles: dict[str, WriterRules] = {}
        self._route_rules: dict[str, WriterRules] = self._rules_by_route()
        self.assess: AssessFn = assess or default_assess
        self.assess_call: AssessCallFn | None = assess_call
        checker = self.rules.assessor if self.rules is not None else None
        if checker is not None and assess is None and assess_call is None:
            # The rule set's own checker, unless the caller brought one.
            if callable(getattr(checker, "assess_call", None)):
                self.assess_call = checker.assess_call
            if callable(checker):
                self.assess = checker
        self.screen: ScreenFn | None = screen if screen is not None else (self.rules.screen if self.rules is not None else None)
        self.image_loader: ImageLoader = image_loader or self._default_image_loader
        self._clock = clock
        self.planner: Planner | None = planner or self._tasks_planner(state_store, tasks_check_seconds)
        # Set by a writer whose calls finish on threads of its own, each time one finishes.
        self._wake = threading.Event()
        for writer in self.writers.values():
            writer.wake = self._wake.set
        self._sleep: Callable[[float], None] = sleep if sleep is not None else self._sleep_until_woken
        self._print = printer
        self._on_event = on_event
        self._progress_seconds = progress_seconds
        self._stop_check_seconds = stop_check_seconds
        self._max_failures = max_consecutive_failures

        self.state = State()
        self.log: EventLog | None = None
        self.problems: list[str] = []
        self._reported: set[str] = set()
        self._started = False
        self._events = 0
        self._parked: set[str] = set()
        # The lines the screen has read and let through in this run: it reads a line once, and never a retry.
        self._screen_passed: set[tuple[str, str]] = set()
        # Calls a cap cannot hold yet wait here instead of being refused. `_blocked` remembers, for each
        # (route, purpose) whose next call did not fit, what that call needed and the cap that held it back,
        # so the lines behind it are not built again until the money it needs is free. `_held_back` counts
        # the lines waiting this pass, for the progress line.
        self._blocked: dict[tuple[str, str], dict[str, Any]] = {}
        self._held_back: dict[tuple[str, str], dict[str, Any]] = {}
        self._api_freed = False  # a one-call route finished a call since the last pass began
        self._last_results_flush = float("-inf")
        self._prepared_here: set[str] = set()
        # Calls whose submit failed in a way that may still have reached the provider: the
        # next attempt looks for the batch before it sends the calls again.
        self._send_unsure: set[str] = set()
        self._halted: dict[str, str] = {}  # routes stopped for this run, with the reason
        self._closed_notes: set[tuple[str, str]] = set()
        self._to_release: list[tuple[Writer, Handle]] = []
        self._submit_failures = 0
        self._ship_failures = 0
        self._last_warning: dict[str, str] = {}
        self._stop_reason: str | None = None
        self._last_stop_check = float("-inf")
        # What the ship store has been handed and what it has sent (see `_stage_ship`). The ledger files
        # by path, as text: sent, and handed over but not yet sent. The accepted-row files handed over
        # and not yet sent: (task id, route) -> (path, row ids).
        self._ledger_written: dict[str, str] = {}
        self._ledger_staged: dict[str, str] = {}
        self._ledger_probed = False
        self._staged_shipments: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
        self._hand_written: str | None = None
        # Whether this run has looked, once, for files that a withdrawn packet left in the state store.
        self._withdrawn_swept = False

    # ------------------------------------------------------------------ setup

    def _rules_by_route(self) -> dict[str, WriterRules]:
        """The writing rules each route writes with: the rules' own prompt, or the prompt profile the route names.

        Routes that name the same profile share one set of rules (kept in `profiles`). A route that names a
        profile the rules do not have, or any profile when no rules are set, is a ConfigError.
        """
        found: dict[str, WriterRules] = {}
        if self.rules is not None:
            self.profiles[self.rules.profile] = self.rules
        for name, route in self.config.routes.items():
            profile = route.profile
            if self.rules is None:
                if profile != DEFAULT_PROFILE:
                    raise ConfigError(f"route {name!r} names the prompt profile {profile!r}, but no writing rules are set")
                continue
            if profile not in self.profiles:
                try:
                    self.profiles[profile] = profile_rules(self.rules, profile)
                except ConfigError as error:
                    raise ConfigError(f"route {name!r}: {error}") from None
            found[name] = self.profiles[profile]
        return found

    def rules_for(self, route_name: str) -> WriterRules | None:
        """The writing rules a route writes with (None when no rules are set)."""
        return self._route_rules.get(route_name, self.rules)

    def _rules_fields(self, route_name: str) -> dict[str, Any]:
        """The version and the prompt profile of a route's rules, for the events that record a call (empty without rules)."""
        rules = self.rules_for(route_name)
        if rules is None:
            return {}
        return {"rules_version": rules.version, "rules_profile": rules.profile}

    def start(self) -> None:
        """Rebuild the state from the event logs and record that this run has started."""
        if self._started:
            return
        self.ledger.validate_prices()
        for name, route in self.config.routes.items():
            if name not in self.writers:
                raise ConfigError(f"route {name!r} has no writer")
            if self.writers[name].needs_rules and route.is_enabled and self.rules is None:
                raise ConfigError(
                    f"route {name!r} sends the writing rules to a model, but no rules are set "
                    "(pass `rules`, or add a `rules` section to the configuration)"
                )
            route_rules = self.rules_for(name)
            if route_rules is not None and route.is_enabled:
                self.writers[name].check_rules(route_rules)
            if route.is_enabled and self.writers[name].bills_cache_storage:
                if self.ledger.storage_rate(name, on=self._today_date()) is None:
                    raise ConfigError(
                        f"route {name!r} keeps the writing rules in a provider cache, which is billed for the hours it is "
                        f"kept, but the price table has no cache_storage rate for model {route.model!r}"
                    )
        replayed = replay(self.state_store)
        self.state = State.from_events(replayed.events)
        if replayed.problems:
            self.problems.append(replayed.report())
            self._print(f"[rps] {replayed.report()}")
        for name, writer in self.writers.items():
            # A writer takes back what it asked to have recorded (a provider-side cache it made).
            writer.restore([dict(note) for (route, _key), note in self.state.caches.items() if route == name])
        self.log = EventLog(
            self.state_store,
            self.actor,
            max_bytes=self.config.events_max_bytes,
            now=self._now,
            on_append=self._applied,
        )
        self._started = True
        self._emit(
            "controller_started",
            {
                "run": self.instance,
                "version": __version__,
                "config": self.config.digest(),
                "price_table_date": self.prices.date,
                "replayed_events": len(replayed.events),
                "skipped_lines": len(replayed.problems),
            },
        )
        for note in self.config.problems() + self.prices.warnings():
            self._print(f"[rps] note: {note}")

    def _note_problem(self, text: str) -> None:
        """Remember a problem found in outside input (once)."""
        if text not in self._reported:
            self._reported.add(text)
            self.problems.append(text)

    def _applied(self, event: Event) -> None:
        self.state.apply(event)
        self._events += 1
        if self._on_event is not None:
            self._on_event(event)

    def _emit(self, kind: str, payload: dict[str, Any], *, durable: bool | None = None) -> Event:
        assert self.log is not None, "call start() first"
        return self.log.append(kind, payload, durable=durable)

    def _today(self) -> str:
        return self._now().astimezone(timezone.utc).strftime("%Y-%m-%d")

    def _today_date(self) -> date:
        return self._now().astimezone(timezone.utc).date()

    def _timestamp(self) -> str:
        return self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _warn(self, message: str) -> None:
        self._print(f"[rps] warning: {redact(message)}")

    # ------------------------------------------------------------------ the main loop

    def run(self, time_budget_seconds: float | None = None, *, max_steps: int | None = None) -> RunSummary:
        """Run passes until idle, a stop request, the time budget or an interrupt.

        `time_budget_seconds` overrides the configured budget. A KeyboardInterrupt ends
        the run cleanly; anything else that goes wrong propagates after the stores are
        flushed (the state on disk stays consistent either way).
        """
        self.start()
        budget = time_budget_seconds if time_budget_seconds is not None else self.config.time_budget_seconds
        started = self._clock()
        steps = 0
        reason = "idle"
        last_progress = float("-inf")
        stuck = 0
        previous = None
        self._print(f"[rps] run {self.instance} started; " + self._progress_line(0.0, budget))
        try:
            while True:
                stop = self._stop_requested(started, budget)
                if stop:
                    reason = stop
                    break
                if max_steps is not None and steps >= max_steps:
                    reason = "max_steps"
                    break
                emitted = self.step()
                steps += 1
                stop = self._stop_reason
                now = self._clock()
                if now - last_progress >= self._progress_seconds:
                    self._print("[rps] " + self._progress_line(now - started, budget))
                    last_progress = now
                if stop:
                    reason = stop
                    break
                fingerprint = self._fingerprint()
                stuck = stuck + 1 if emitted and fingerprint == previous else 0
                previous = fingerprint
                if stuck >= 25:
                    raise ControllerError("the controller keeps writing events without changing anything; stopping")
                if self.is_idle():
                    reason = "idle"
                    break
                if not emitted:
                    self._wait(started, budget)
        except KeyboardInterrupt:
            reason = "interrupt"
        except Exception:
            self._flush_quietly()
            raise
        return self._finish(reason, steps, started, budget)

    def _wait(self, started: float, budget: float | None) -> None:
        pause = self.config.poll_seconds
        if budget is not None:
            pause = min(pause, max(0.0, budget - (self._clock() - started)))
        self._flush_quietly(force=False)
        if pause > 0:
            self._sleep(pause)

    def _sleep_until_woken(self, seconds: float) -> None:
        """Sleep up to `seconds`, less when a one-call route finishes a call.

        A worker that has finished is idle until the controller gives it the next call, so the
        controller looks at once instead of at its next poll. A finish that comes in while the
        controller is busy is not lost: the wake-up stays set until the controller next sleeps.
        """
        self._wake.wait(seconds)
        self._wake.clear()

    def _flush_quietly(self, force: bool = True) -> None:
        for store in (self.state_store, self.ship_store):
            try:
                store.flush(force=force)
            except RateLimitedError as error:
                self._warn(str(error))
            except StoreError as error:
                self._warn(f"could not flush a store: {error}")

    def _finish(self, reason: str, steps: int, started: float, budget: float | None) -> RunSummary:
        self._wind_down(collect=reason != "interrupt")
        self._send_shipments_at_the_end()
        elapsed = self._clock() - started
        counts = self.state.counts()
        self._emit("controller_stopped", {"run": self.instance, "reason": reason, "steps": steps, "seconds": round(elapsed, 3), "counts": counts})
        self._flush_quietly(force=True)
        summary = RunSummary(
            reason=reason,
            steps=steps,
            events=self._events,
            seconds=elapsed,
            progress=self.state.job_progress(self.config.max_attempts),
            parked_by_budget=len(self._parked),
            hand_queue=len(self.state.hand_queue()),
            problems=list(self.problems),
        )
        self._print(f"[rps] run {self.instance} ended ({reason}); " + self._progress_line(elapsed, budget))
        return summary

    def _wind_down(self, *, collect: bool = True) -> None:
        """Take in whatever has already finished, then let the writers stop their background work.

        A synchronous route's calls that finished but were not collected yet are paid for;
        recording them now saves sending those calls again. After an interrupt the last
        collection is skipped: whoever pressed the key wants out.
        """
        if collect:
            try:
                self._stage_poll()
            except NotImplementedError:
                raise
            except Exception as error:
                self._warn(f"the last collection failed: {describe_error(error)}")
        try:
            self._drain_notes()
        except NotImplementedError:
            raise
        except Exception as error:
            self._warn(f"recording what the writers noted failed: {describe_error(error)}")
        for name, writer in self.writers.items():
            try:
                writer.close()
            except Exception as error:
                self._warn(f"closing {name} failed: {describe_error(error)}")

    def _stop_requested(self, started: float, budget: float | None) -> str | None:
        if self._stop_reason:
            return self._stop_reason
        stop_file = self.config.stop_file_path()
        if stop_file is not None and stop_file.exists():
            return "stop_file"
        now = self._clock()
        if self.config.remote_stop_path and now - self._last_stop_check >= self._stop_check_seconds:
            self._last_stop_check = now
            try:
                if self.state_store.exists(self.config.remote_stop_path):
                    return "stop_file"
            except StoreError as error:
                self._warn(f"could not look for the remote stop file: {error}")
        if budget is not None and now - started >= budget:
            return "time_budget"
        return None

    def _fingerprint(self) -> tuple[Any, ...]:
        counts = self.state.counts()
        return (
            json.dumps(counts, sort_keys=True),
            len(self.state.calls),
            len(self.state.packets),
            len(self.state.shipped_files),
            sum(len(row.verdicts) for row in self.state.rows.values()),
            round(sum(self.state.spend(route).total for route in self.config.routes), 9),
        )

    # ------------------------------------------------------------------ one pass

    def step(self) -> int:
        """Run every stage once. Returns the number of events written."""
        self.start()
        before = self._events
        self._api_freed = False
        self._stage_plan()
        self._stage_submit()
        self._drain_notes()
        self._stage_poll()
        if self._api_freed:
            self._refill_api_routes()
        self._drain_notes()  # a cache kept alive while a batch was polled
        self._stage_assess()
        self._stage_review()
        self._stage_ship()
        return self._events - before

    # ------------------------------------------------------------------ plan

    def add_task(self, spec: TaskSpec) -> str | None:
        """Record a task. Returns its id, or None when it was already recorded."""
        self.start()
        if spec.route not in self.config.routes:
            raise ConfigError(f"task {spec.source}/{spec.batch}: unknown route {spec.route!r}")
        task_id = spec.task_id
        existing = self.state.tasks.get(task_id)
        if existing is not None:
            if (existing.route, existing.purpose) != (spec.route, spec.purpose):
                raise TaskConflict(
                    f"task {task_id} exists with route {existing.route!r} and purpose {existing.purpose!r}; "
                    "give the new task a different variant"
                )
            return None
        source = self.config.source(spec.source)
        route = self.config.routes[spec.route]
        lines = []
        for line in spec.lines:
            entry = line.to_dict()
            entry["license"] = line.license or source.license
            if not entry["license"]:
                raise ConfigError(
                    f"task {spec.source}/{spec.batch}, line {line.line_id}: no license "
                    "(set it on the line or on the source in the configuration)"
                )
            if line.image:
                # A picture goes only where the routing table allows its class; refuse the whole
                # task before anything is prepared or paid for.
                image_class = line.image_class or source.image_class
                try:
                    check_image_allowed(image_class, route.provider)
                except ImageRoutingError as error:
                    raise ConfigError(f"task {spec.source}/{spec.batch}, line {line.line_id}: {error}") from None
                entry["image_class"] = image_class
            lines.append(entry)
        self._emit(
            "task_created",
            {
                "task_id": task_id,
                "source": spec.source,
                "batch": spec.batch,
                "variant": spec.variant,
                "route": spec.route,
                "purpose": spec.purpose,
                "effort": route.effort,
                "pool_version": self.config.pool_version,
                "dataset": source.dataset or spec.source,
                "lines": lines,
            },
        )
        return task_id

    def _tasks_planner(self, store: Store, check_seconds: float | None) -> Planner | None:
        """The planner the configuration names: the tasks file on disk, or one in the state store.

        A file in the state store is read through the store, on every pass for a local folder and at
        most every `TASKS_CHECK_SECONDS` for a store behind a network (`check_seconds` sets it).
        """
        tasks_file = self.config.tasks_file
        if not tasks_file:
            return None
        in_store = self.config.tasks_store_path()
        if in_store is None:
            return FilePlanner(tasks_file)
        if check_seconds is None:
            check_seconds = 0.0 if isinstance(store, LocalStore) else TASKS_CHECK_SECONDS
        return StorePlanner(self.state_store, in_store, min_interval_seconds=check_seconds, clock=self._clock)

    def _stage_plan(self) -> None:
        """Record the tasks the planner hands over. A tasks file that cannot be read, or a task that cannot
        be added, is reported once and skipped: the run goes on, and the next pass reads the file again."""
        if self.planner is None:
            return
        try:
            specs = list(self.planner(self.state))
        except (ConfigError, StoreError) as error:
            self._planner_trouble(f"the tasks could not be read: {describe_error(error)}")
            return
        for spec in specs:
            try:
                self.add_task(spec)
            except (ConfigError, TaskConflict) as error:
                self._planner_trouble(f"a task was not added: {describe_error(error)}")

    def _planner_trouble(self, text: str) -> None:
        """Note a problem with the tasks the planner handed over; each distinct problem is printed once."""
        if text not in self._reported:
            self._warn(text)
        self._note_problem(text)

    # ------------------------------------------------------------------ submit

    def _default_image_loader(self, source: Any, line: Any) -> tuple[bytes, str, int]:
        folder = Path(source.images_dir) if source.images_dir else self.config.work_path("images")
        return load_picture(folder / line.image, source.image_size)

    def _call_spec(
        self,
        task: TaskState,
        line: LineState,
        attempt: int,
        feedback: Sequence[str],
        *,
        boost: bool = False,
        max_output_tokens: int | None = None,
        repair_of: str | None = None,
        route_name: str | None = None,
    ) -> tuple[CallSpec, int | None]:
        """Build the call for a spec line, with the size of the picture it carries (if any).

        A picture is loaded only after the routing table has allowed it for the route
        (`ImageRoutingError` otherwise). With `boost` the token limit is raised by the
        route's truncation factor: the previous answer was cut off at the limit. A call
        rebuilt after a restart passes the limit it was first prepared with.

        With `repair_of` (a row's id) the call is one of the repair step: the same spec line, charged to the
        purpose `repair`, whose `feedback` is the reviewers' notes and the row as it was written, put to the
        writer in the words of `rps.prompting.repair_text` instead of as check messages.

        The call is for the route of the task unless `route_name` names another: a repair call goes to the
        route the row was sent back to (`repair.routes`), and is built from that route's provider, model,
        settings and prompt profile, though the spec line is the task's own.
        """
        route_name = route_name or task.route
        route = self.config.routes[route_name]
        image = ext = None
        size = None
        if line.image:
            check_image_allowed(line.image_class, route.provider)
            image, ext, size = self.image_loader(self.config.source(task.source), line)
        if not max_output_tokens:
            max_output_tokens = route.max_output_tokens
            if boost:
                max_output_tokens = max(max_output_tokens + 1, int(max_output_tokens * route.truncation_factor))
        rules = self.rules_for(route_name)
        spec = CallSpec(
            call_id=ids.call_id(task.task_id, line.line_id, attempt),
            task_id=task.task_id,
            line=line.line_id,
            attempt=attempt,
            route=route_name,
            provider=route.provider,
            model=route.model,
            kind=route.kind,
            effort=self._effort_of(task, route_name),
            max_output_tokens=max_output_tokens,
            spec=line.spec,
            pool_version=task.pool_version,
            purpose=effective_purpose(task.purpose, attempt, repair=repair_of is not None),
            feedback=tuple(feedback),
            image=image,
            image_ext=ext,
            image_class=line.image_class if line.image else None,
            task_text=render_task_text(
                line.spec, feedback, ordinal=line.ordinal, rules=rules, repair=repair_of is not None
            ),
            rules=rules,
            repair_of=repair_of,
        )
        return spec, size

    @staticmethod
    def _purpose_of(task: TaskState, status: Any) -> str:
        """The purpose the next call of a due line is charged to: `repair` for a call of the repair step,
        else the task's purpose for a line's first call and `rebound` for any retry."""
        return effective_purpose(task.purpose, status.next_attempt, repair=status.repair_of is not None)

    @staticmethod
    def _call_route(task: TaskState, status: Any) -> str:
        """The route the next call of a due line is made on: the route of its task, except for a call of the repair
        step, which goes to the route the row was sent back to (`repair.routes`; the state has it from the
        `repair_requested` event, so a restart sends the call where the event said, whatever the configuration
        says by then)."""
        return status.route or task.route

    def _effort_of(self, task: TaskState, route_name: str) -> str | None:
        """The effort a call on `route_name` runs at: the effort recorded with the task for a call on the task's own
        route, and the route's own setting for a call on another (a repair call on the route of `repair.routes`)."""
        route = self.config.routes[route_name]
        return (task.effort or route.effort) if route_name == task.route else route.effort

    # -- which routes may take calls

    def _note_closed(self, route_name: str, reason: str, *, permanent: bool = True) -> None:
        """Say once that a route cannot take calls, and why."""
        if (route_name, reason) in self._closed_notes:
            return
        self._closed_notes.add((route_name, reason))
        if permanent:
            self._note_problem(f"route {route_name} is closed for this run: {reason}; its calls are left alone")
        self._print(f"[rps] route {route_name} is closed{'' if permanent else ' for now'}: {reason}")

    def route_state(self, route_name: str) -> tuple[str, str]:
        """(state, reason) of a route: "open" (calls may go out), "later" (closed for now; it
        opens by itself) or "closed" (for this run). Records and prints nothing."""
        route = self.config.routes.get(route_name)
        writer = self.writers.get(route_name)
        if route is None or writer is None:
            return "closed", "the route has no writer"
        if route_name in self._halted:
            return "closed", f"stopped for this run ({self._halted[route_name]})"
        if not route.is_enabled:
            return "closed", "it is switched off in the configuration"
        acceptance = writer.accepting()
        if acceptance.ok:
            return "open", ""
        return ("later" if acceptance.later else "closed"), acceptance.reason or "its writer takes no calls"

    def _route_status(self, route_name: str) -> str:
        """The state of a route, noting once why a closed route is closed."""
        state, reason = self.route_state(route_name)
        if state != "open" and route_name not in self._halted:
            self._note_closed(route_name, reason, permanent=state == "closed")
        return state

    def _halt_route(self, route_name: str, kind: str, reason: str, *, call_id: str | None = None) -> None:
        """Stop a route for the rest of this run (the provider rejected a request or the key)."""
        if route_name in self._halted:
            return
        self._halted[route_name] = kind
        text = redact(reason)[:PROVIDER_ERROR_CHARS]
        self._emit("route_halted", {"route": route_name, "kind": kind, "reason": text, "call_id": call_id})
        self._note_problem(f"route {route_name} stopped for this run ({kind}): {text}")
        self._print(f"[rps] route {route_name} stopped for this run ({kind}): {text}")

    def _api_room(self, route_name: str, route: RouteConfig) -> int:
        """How many more calls a one-call (api) route takes now: its workers, less the calls it has in flight.

        The money a call holds is taken when the call is prepared, and a call is prepared only when a worker
        is free to send it. So such a route never holds more than `workers` calls' worth of money, however
        long the list of due lines is, and a job of tens of thousands of lines is never all in memory. A stop
        loses the calls in flight (they come back as rebounds).
        """
        return max(0, route.workers - self.state.unfinished_calls(route_name))

    def _repair_switched_off(self, task: TaskState, status: Any) -> bool:
        """True for a call of the repair step that is due on a route where the step is no longer wanted (it was
        switched off, or the route that was to write the row again is switched off or has no cap for `repair` any
        more, since the row was sent back). The route is the one the row was sent back to, not the row's own, which
        may be switched off for good (`repair.routes`). Such a call is not made; the row goes back to the hand
        queue (`_settle_repairs`)."""
        return status.repair_of is not None and not route_may_repair(self.config, self._call_route(task, status))

    def _due_by_route(self) -> dict[str, list[tuple[TaskState, LineState, Any]]]:
        """The calls due, by the route they are made on: the route of the task of a spec line, and the route the
        row was sent back to for a call of the repair step."""
        grouped: dict[str, list[tuple[TaskState, LineState, Any]]] = {}
        for item in self.state.lines_needing_calls(self.config.max_attempts):
            if self._repair_switched_off(item[0], item[2]):
                continue
            grouped.setdefault(self._call_route(item[0], item[2]), []).append(item)
        return grouped

    def _stage_submit(self, *, api_only: bool = False) -> None:
        """Prepare the calls that are due, record the ones the caps can hold, and submit them.

        A one-call route is given as many calls as it has free workers. A call whose cost no cap can
        hold at the moment waits (the line stays due and is asked again once the money is free); one
        that no cap could hold even when nothing is in flight is refused for good.
        """
        if self._stop_reason:
            return
        if not api_only:
            self._held_back = {}
        due_by_route: dict[str, list[tuple[TaskState, LineState, Any]]] | None = None
        for route_name, writer in self.writers.items():
            route = self.config.routes.get(route_name)
            if route is None or (api_only and route.kind != "api") or self._route_status(route_name) != "open":
                continue
            approved: dict[str, Request] = {}
            room = self._api_room(route_name, route) if route.kind == "api" else None
            if room is None or room > 0:
                if due_by_route is None:
                    due_by_route = self._due_by_route()
                due = self._take_due(route_name, due_by_route.get(route_name, []), room)
                for group in _chunks(due, route.max_batch_calls):
                    self._admit(route_name, writer, route, group, approved)
                self._count_held_back(route_name, due_by_route.get(route_name, []), approved)
            self._submit_pending(route_name, writer, approved)

    def _refill_api_routes(self) -> None:
        """Give the workers a one-call route has just freed their next calls, and start them, without waiting for the next pass."""
        before = self._events
        self._stage_submit(api_only=True)
        if self._events != before:
            self._stage_poll(api_only=True)

    def _take_due(
        self, route_name: str, items: Sequence[tuple[TaskState, LineState, Any]], room: int | None
    ) -> list[tuple[TaskState, LineState, Any]]:
        """The due lines of a route to build calls for now, in order, at most `room` of them (all when None).

        Left out are the lines whose call was refused for good this run, and the lines of a (route, purpose)
        whose next call does not fit yet: they are not built again until the money that call needs is free.
        A line about to get its first call is screened here, so a line the screen sets aside is never built
        into a call and does not use up a free worker.
        """
        waiting = self._purposes_still_waiting(route_name)
        taken: list[tuple[TaskState, LineState, Any]] = []
        for item in items:
            task, line, status = item
            if self._purpose_of(task, status) in waiting:
                continue
            if ids.call_id(task.task_id, line.line_id, status.next_attempt) in self._parked:
                continue
            if self._screened_out(task, line, status):
                continue
            taken.append(item)
            if room is not None and len(taken) >= room:
                break
        return taken

    def _screened_out(self, task: TaskState, line: LineState, status: Any) -> bool:
        """Read a line with the screen before its first call. True when the screen set it aside: the line
        is recorded as screened and no call is ever made for it.

        Only a line with no call yet is read, once per run (a restart reads it again if the event was
        not written); a retry is never screened. A screen that fails on a line lets the line through,
        and the failure is noted once.
        """
        key = (task.task_id, line.line_id)
        if self.screen is None or status.outcome != "new" or key in self._screen_passed:
            return False
        try:
            reason = self.screen(line.spec)
        except Exception as error:  # a rule function must not stop the run
            text = f"the screen could not read a spec line ({type(error).__name__}: {redact(str(error))[:200]})"
            if text not in self._reported:
                self._warn(f"{text}; a line it cannot read goes to the writer")
            self._note_problem(text)
            reason = None
        if not reason:
            self._screen_passed.add(key)
            return False
        self._emit(
            "line_screened",
            {
                "task_id": task.task_id,
                "line": line.line_id,
                "item_id": line.item_id or line.line_id,
                "route": task.route,
                "class": "other",
                "reason": str(reason)[:300],
            },
        )
        return True

    def _purposes_still_waiting(self, route_name: str) -> set[str]:
        """The purposes of a route whose next call still does not fit. One whose call now fits, or never will, is let go."""
        waiting: set[str] = set()
        for (route, purpose), memo in list(self._blocked.items()):
            if route != route_name:
                continue
            if self.ledger.check(self.state, route, purpose, memo["need"]).waits:
                waiting.add(purpose)
            else:
                del self._blocked[(route, purpose)]  # the money is free, or the call is refused when it is built again
        return waiting

    def _count_held_back(
        self, route_name: str, items: Sequence[tuple[TaskState, LineState, Any]], approved: Mapping[str, Request]
    ) -> None:
        """Count the lines of a route that wait for room under a cap, for the progress line (not the ones just prepared)."""
        for scope in [scope for scope in self._held_back if scope[0] == route_name]:
            del self._held_back[scope]
        blocked = {purpose: memo["cap"] for (route, purpose), memo in self._blocked.items() if route == route_name}
        if not blocked:
            return
        for task, line, status in items:
            purpose = self._purpose_of(task, status)
            if purpose not in blocked:
                continue
            call_id = ids.call_id(task.task_id, line.line_id, status.next_attempt)
            if call_id in approved or call_id in self._parked or self.state.is_screened(task.task_id, line.line_id):
                continue
            entry = self._held_back.setdefault((route_name, purpose), {"cap": blocked[purpose], "count": 0})
            entry["count"] += 1

    def _admit(
        self,
        route_name: str,
        writer: Writer,
        route: RouteConfig,
        group: Sequence[tuple[TaskState, LineState, Any]],
        approved: dict[str, Request],
    ) -> None:
        """Build the calls for a group of due lines and record each one the caps can hold.

        A call that fits is recorded as prepared (`call_prepared`), which takes its hold. A call that would
        fit once the calls in flight have finished waits: nothing is recorded, its line stays due, and the
        lines behind it in the same (route, purpose) wait with it. A call that could not fit even then is
        refused for good (`budget_refused`) and set aside for this run.
        """
        specs: list[CallSpec] = []
        sizes: dict[str, int | None] = {}
        retry_of: dict[str, str | None] = {}
        origin: dict[str, tuple[TaskState, LineState, Any]] = {}
        for task, line, status in group:
            try:
                spec, size = self._call_spec(
                    task,
                    line,
                    status.next_attempt,
                    status.feedback,
                    boost=status.boost,
                    repair_of=status.repair_of,
                    route_name=route_name,
                )
            except (ImageError, ImageRoutingError) as error:
                self._record_unpreparable(task, line, status, str(error))
                continue
            specs.append(spec)
            sizes[spec.call_id] = size
            retry_of[spec.call_id] = status.retry_of
            origin[spec.call_id] = (task, line, status)
        if not specs:
            return
        for spec, request in self._prepare_group(writer, specs):
            if isinstance(request, str):
                task, line, status = origin[spec.call_id]
                self._record_unpreparable(task, line, status, request)
                continue
            too_long = self._long_prompt_problem(route, request)
            if too_long:
                task, line, status = origin[spec.call_id]
                self._record_unpreparable(task, line, status, too_long, KIND_LONG_PROMPT)
                continue
            scope = (route_name, spec.purpose)
            if scope in self._blocked:
                continue  # behind a call that did not fit
            estimate = self.ledger.estimate(
                route_name,
                request.est_input_tokens,
                request.max_output_tokens,
                tier=writer.hold_tier(),
                on=self._today_date(),
            )
            decision = self.ledger.check(self.state, route_name, spec.purpose, estimate)
            if decision.waits:
                cap = _cap_name(decision.reason, route_name, spec.purpose, decision.purpose_cap, decision.route_cap)
                self._blocked[scope] = {"need": estimate, "cap": cap}
                continue
            if not decision.ok:
                self._parked.add(spec.call_id)
                self._emit(
                    "budget_refused",
                    {
                        "call_id": spec.call_id,
                        "task_id": spec.task_id,
                        "line": spec.line,
                        "attempt": spec.attempt,
                        **decision.numbers(),
                        "message": decision.message(),
                    },
                )
                continue
            self._emit(
                "call_prepared",
                {
                    "call_id": spec.call_id,
                    "task_id": spec.task_id,
                    "line": spec.line,
                    "attempt": spec.attempt,
                    "route": route_name,
                    "purpose": spec.purpose,
                    "est_usd": estimate,
                    "est_input_tokens": request.est_input_tokens,
                    "max_output_tokens": request.max_output_tokens,
                    "image_size": sizes[spec.call_id],
                    "feedback": list(spec.feedback),
                    "retry_of": retry_of[spec.call_id],
                    **self._rules_fields(route_name),
                    **({"repair_of": spec.repair_of} if spec.repair_of is not None else {}),
                },
            )
            self._prepared_here.add(spec.call_id)
            approved[spec.call_id] = request

    def _prepare_group(self, writer: Writer, specs: Sequence[CallSpec]) -> list[tuple[CallSpec, Request | str]]:
        """Build the requests for a group of calls.

        A call the writer cannot build comes back with the problem text in place of a
        request, so one bad call does not take the rest of its group with it.
        """
        try:
            return list(zip(specs, writer.prepare(specs)))
        except NotImplementedError:
            raise
        except Exception:
            pass
        pairs: list[tuple[CallSpec, Request | str]] = []
        for spec in specs:
            try:
                pairs.append((spec, writer.prepare([spec])[0]))
            except NotImplementedError:
                raise
            except Exception as error:
                pairs.append((spec, f"{type(error).__name__}: {error}"))
        return pairs

    def _long_prompt_problem(self, route: RouteConfig, request: Request) -> str | None:
        """Why a call is refused for its length, or None when it is short enough.

        Past a provider's long-prompt threshold higher rates apply that the price table does
        not model, so a call whose estimated input passes it is refused before it is sent.
        """
        limit = self.prices.long_prompt_limit(route.model)
        if limit is None or request.est_input_tokens <= limit:
            return None
        return (
            f"the prompt is estimated at {request.est_input_tokens:,} tokens, over the {limit:,}-token "
            f"long-prompt threshold of {route.model}, where higher rates apply that the price table does not model"
        )

    def _record_unpreparable(
        self, task: TaskState, line: LineState, status: Any, problem: str, kind: str = KIND_BAD_REQUEST
    ) -> None:
        """A call that cannot be built (its picture cannot be read) fails at once, without retries."""
        call_id = ids.call_id(task.task_id, line.line_id, status.next_attempt)
        route_name = self._call_route(task, status)
        self._emit(
            "call_prepared",
            {
                "call_id": call_id,
                "task_id": task.task_id,
                "line": line.line_id,
                "attempt": status.next_attempt,
                "route": route_name,
                "purpose": self._purpose_of(task, status),
                "est_usd": 0.0,
                "est_input_tokens": 0,
                "max_output_tokens": 0,
                "image_size": None,
                "feedback": [],
                "retry_of": status.retry_of,
                **self._rules_fields(route_name),
                **({"repair_of": status.repair_of} if status.repair_of is not None else {}),
            },
        )
        self._emit(
            "call_failed",
            {
                "call_id": call_id,
                "handle_id": None,
                "error": redact(f"call could not be prepared: {problem}")[:300],
                "retryable": False,
                "billed": False,
                "kind": kind,
                "usage": {},
                "usd": 0.0,
                "price_table_date": self.prices.date,
                "minutes": 0.0,
            },
        )

    def _rebuild_request(self, writer: Writer, call_id: str) -> Request | None:
        """The request of a call prepared by an earlier run, rebuilt with the limit it was prepared with."""
        call = self.state.calls[call_id]
        task = self.state.tasks.get(call.task_id)
        line = task.lines.get(call.line) if task else None
        if task is None or line is None:
            return None
        try:
            spec, _ = self._call_spec(
                task,
                line,
                call.attempt,
                call.feedback,
                max_output_tokens=call.max_output_tokens or None,
                repair_of=call.repair_of,
                route_name=call.route,
            )
            return writer.prepare([spec])[0]
        except NotImplementedError:
            raise
        except Exception as error:
            self._warn(f"call {call_id}: {describe_error(error)}")
            return None

    def _submit_pending(self, route_name: str, writer: Writer, approved: dict[str, Request]) -> None:
        """Submit every call that is prepared but not yet submitted."""
        pending_ids = [call.call_id for call in self.state.pending_calls(route_name)]
        if not pending_ids:
            return
        requests: list[Request] = []
        inherited: list[Request] = []
        for call_id in pending_ids:
            request = approved.get(call_id) or self._rebuild_request(writer, call_id)
            if request is None:
                self._emit_failed_unpreparable(call_id)
                continue
            requests.append(request)
            if call_id not in self._prepared_here or call_id in self._send_unsure:
                inherited.append(request)
        # Calls prepared by an earlier run may have been submitted just before it stopped, and so
        # may calls whose submit failed on the way back from the provider. Every batch the writer
        # can find for them is taken up again, so none is sent twice. The writer is told when the
        # calls were prepared (nothing was sent before that) and which batches are already recorded.
        since = self._earliest_prepared(inherited)
        while inherited:
            try:
                found = writer.recover(inherited, since=since, recorded=frozenset(self.state.batches))
            except NotImplementedError:
                raise
            except Exception as error:
                # Sending them again could pay twice; leave them for the next pass.
                self._submit_failures += 1
                self._warn(f"looking for earlier work on {route_name} failed ({self._submit_failures}): {describe_error(error)}")
                if self._submit_failures >= self._max_failures:
                    self._stop_reason = "submit_failing"
                return
            # Only calls still waiting for a handle count, so each pass takes in at least one more
            # and the loop ends.
            covered = set(found.call_ids) & {r.call_id for r in inherited} if found is not None else set()
            if found is None or not covered:
                break
            self._emit(
                "batch_submitted",
                {
                    "handle_id": found.handle_id,
                    "handle": found.to_dict(),
                    "route": route_name,
                    "call_ids": [cid for cid in pending_ids if cid in covered],
                    "recovered": True,
                },
                durable=True,
            )
            requests = [r for r in requests if r.call_id not in covered]
            inherited = [r for r in inherited if r.call_id not in covered]
            self._send_unsure -= covered
        if not requests:
            return
        for group in writer.split(requests):
            try:
                handle = writer.submit(group)
            except NotImplementedError:
                raise
            except RouteRejected as error:
                self._halt_route(route_name, error.kind, str(error), call_id=group[0].call_id if group else None)
                return
            except RouteUnavailable as error:
                self._note_closed(route_name, str(error), permanent=False)
                return
            except Exception as error:
                # The provider may have taken the batch even though the answer never came back.
                self._send_unsure.update(request.call_id for request in group)
                self._submit_failures += 1
                self._warn(f"submit to {route_name} failed ({self._submit_failures}): {describe_error(error)}")
                if self._submit_failures >= self._max_failures:
                    self._stop_reason = "submit_failing"
                return
            self._submit_failures = 0
            self._send_unsure.difference_update(request.call_id for request in group)
            # A provider batch id is made durable at once: a restart polls the batch instead of sending its calls
            # again. The handle of a one-call route lives in this process only (a restart sends its calls again
            # whether or not the id was kept), and such a route hands over one handle for every few calls it frees,
            # so the record waits for the store's own commit like any other event.
            self._emit(
                "batch_submitted",
                {
                    "handle_id": handle.handle_id,
                    "handle": handle.to_dict(),
                    "route": route_name,
                    "call_ids": [request.call_id for request in group],
                    "recovered": False,
                },
                durable=self.config.routes[route_name].kind != "api",
            )

    def _earliest_prepared(self, requests: Sequence[Request]) -> datetime | None:
        """When the earliest of these calls was prepared: nothing was sent for them before then.

        None when any of the times is missing or unreadable, so a writer never works from a
        moment later than the truth.
        """
        moments = []
        for request in requests:
            call = self.state.calls.get(request.call_id)
            try:
                moments.append(_parse_ts(call.prepared_ts))  # type: ignore[union-attr]
            except (AttributeError, ValueError):
                return None
        return min(moments) if moments else None

    def _emit_failed_unpreparable(self, call_id: str) -> None:
        self._emit(
            "call_failed",
            {
                "call_id": call_id,
                "handle_id": None,
                "error": "call could not be rebuilt after a restart (its task or picture is missing)",
                "retryable": False,
                "billed": False,
                "kind": KIND_UNEXPECTED,
                "usage": {},
                "usd": 0.0,
                "price_table_date": self.prices.date,
                "minutes": 0.0,
            },
        )

    # ------------------------------------------------------------------ poll and collect

    def _calls_waiting(self, call_ids: Sequence[str]) -> bool:
        """True while any of these calls has been submitted and has no result yet."""
        return any(self.state.calls[call_id].status == "submitted" for call_id in call_ids if call_id in self.state.calls)

    def _warn_changed(self, key: str, message: str) -> None:
        """Print a warning unless the same one was the last printed for this key."""
        if self._last_warning.get(key) != message:
            self._last_warning[key] = message
            self._warn(message)

    def _flush_state(self) -> bool:
        """Make everything recorded so far durable. False when the store could not take it now."""
        try:
            self.state_store.flush(force=True)
        except RateLimitedError as error:
            self._warn(str(error))
            return False
        except StoreError as error:
            self._warn(f"could not flush the state store: {error}")
            return False
        return True

    def _release_ready(self) -> None:
        """Let writers clean up after handles whose results are durably recorded."""
        if not self._to_release or not self._flush_state():
            return
        waiting, self._to_release = self._to_release, []
        for writer, handle in waiting:
            self._release_handle(writer, handle)

    def _drain_notes(self) -> None:
        """Record what writers asked to have remembered (a cache they made), and charge its storage.

        The storage charge is written first: if the run stops between the two, the cache is not
        remembered and gets made again, which costs a little more rather than leaving a fee
        unrecorded.
        """
        for writer in self.writers.values():
            for kind, payload in writer.drain_notes():
                if kind == "cache_created":
                    self._charge_cache_storage(payload)
                self._emit(kind, payload, durable=True)

    def _charge_cache_storage(self, note: Mapping[str, Any]) -> None:
        """Charge the storage fee for the stretch of a cache's life a note covers, as a ledger line of its own.

        A new cache is billed from its creation to its expiry; a cache kept alive longer, from its
        earlier expiry to the new one. The fee is the cache's tokens times the hours times the
        storage rate per million tokens per hour of the day it is charged, and goes to the purpose
        most of the calls that use the cache carry.
        """
        route = str(note.get("route", ""))
        name = str(note.get("name", ""))
        started = _parse_moment(note.get("extended_from") or note.get("created"))
        ends = _parse_moment(note.get("expires"))
        tokens = note.get("tokens")
        if route not in self.config.routes:
            return
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens <= 0 or started is None or ends is None:
            self._warn(f"the storage of cache {name} on {route} cannot be charged: its size or its dates are not known")
            return
        if ends <= started:
            return  # the cache was not kept any longer: nothing more to pay for
        hours = (ends - started).total_seconds() / 3600.0
        today = self._today_date()
        try:
            rate = self.ledger.storage_rate(route, on=today)
            usd = self.ledger.storage_cost(route, tokens, hours, on=today)
        except PriceError as error:
            self._warn(f"the storage of cache {name} on {route} cannot be charged: {error}")
            return
        self._emit(
            "cache_storage",
            {
                "route": route,
                "purpose": self._storage_purpose(route, name),
                "key": note.get("key"),
                "name": name,
                "model": note.get("model"),
                "tokens": tokens,
                "tokens_estimated": bool(note.get("tokens_estimated", False)),
                "hours": round(hours, 6),
                "rate": rate,
                "usd": usd,
                "from": note.get("extended_from") or note.get("created"),
                "to": note.get("expires"),
                "price_table_date": self.prices.date,
            },
            durable=True,
        )

    def _storage_purpose(self, route_name: str, cache_name: str) -> str:
        """The purpose a cache's storage is charged to: the one most of the calls using the cache carry.

        Batches still waiting that refer to the cache come first; a cache made for a batch that is
        not recorded yet counts the calls waiting on the route; failing that, every call on the route.
        """
        counts: Counter[str] = Counter()
        for batch in self.state.batches.values():
            data = batch.handle.get("data") or {}
            if batch.route == route_name and not batch.collected and data.get("cache_name") == cache_name:
                counts.update(self.state.calls[call_id].purpose for call_id in batch.call_ids if call_id in self.state.calls)
        if not counts:
            counts.update(call.purpose for call in self.state.pending_calls(route_name))
        if not counts:
            counts.update(call.purpose for call in self.state.calls.values() if call.route == route_name)
        if not counts:
            return "production"
        return max(counts, key=lambda purpose: (counts[purpose], -PURPOSES.index(purpose)))

    def _stage_poll(self, *, api_only: bool = False) -> None:
        for batch in list(self.state.batches_to_poll()):
            writer = self.writers.get(batch.route)
            if writer is None:
                continue
            route = self.config.routes.get(batch.route)
            one_call = route is not None and route.kind == "api"
            if api_only and not one_call:
                continue
            handle = Handle.from_dict(batch.handle)
            try:
                status = writer.poll(handle)
            except NotImplementedError:
                raise
            except Exception as error:
                self._warn_changed(batch.handle_id, f"polling {batch.handle_id} failed: {describe_error(error)}")
                continue
            if status.state == "in_progress":
                continue
            returned = failed = 0
            if status.state in ("ended", "partial"):
                try:
                    results = writer.results(handle)
                except NotImplementedError:
                    raise
                except Exception as error:
                    self._warn_changed(batch.handle_id, f"collecting {batch.handle_id} failed: {describe_error(error)}")
                    continue
                returned, failed = self._collect(batch.handle_id, batch.call_ids, batch.submitted_ts, results)
                if any(result.kind == KIND_MISMATCH for result in results):
                    text = f"batch {batch.handle_id} was taken up again after a crash but did not hold its calls; they are sent again"
                    self._note_problem(text)
                    self._warn(text)
                if status.state == "partial":
                    if returned or failed:
                        # Results already paid for are made durable as they land; those of a one-call route at
                        # the pace the store commits its other events, since they come in about one a call.
                        if one_call:
                            self._flush_results()
                        else:
                            self._flush_state()
                    if self._calls_waiting(batch.call_ids):
                        continue
                    # Every call is in, so the batch is done even though the writer has not said so.
                else:
                    failed += self._fail_unfinished(
                        batch.handle_id, batch.call_ids, "the call is missing from the batch results", KIND_UNEXPECTED
                    )
            else:
                note = f": {status.note}" if status.note else ""
                kind = {"expired": KIND_EXPIRED, "canceled": KIND_CANCELED}.get(status.state, KIND_PROVIDER)
                failed = self._fail_unfinished(
                    batch.handle_id, batch.call_ids, f"the batch {status.state}{note}", kind
                )
            self._emit("batch_collected", {"handle_id": batch.handle_id, "returned": returned, "failed": failed})
            if one_call:
                # Nothing is kept for it at the provider, and its results are recorded: it can be forgotten now,
                # without a flush of its own for every call.
                self._release_handle(writer, handle)
            else:
                self._to_release.append((writer, handle))
        self._release_ready()

    def _flush_results(self) -> bool:
        """Make what is recorded so far durable, but not more often than every `commit.seconds_per_commit`.

        A one-call route hands in a result about every call; making each one durable on its own would be a
        commit to a remote store as often as that. What is still pending when the time is up goes with the next
        pass, the store's own commits, or the end of the run.
        """
        now = self._clock()
        if now - self._last_results_flush < self.config.commit.seconds_per_commit:
            return False
        self._last_results_flush = now
        return self._flush_state()

    def _release_handle(self, writer: Writer, handle: Handle) -> None:
        try:
            writer.release(handle)
        except NotImplementedError:
            raise
        except Exception as error:
            self._warn(f"clean-up after {handle.handle_id} failed: {describe_error(error)}")

    def _collect(self, handle_id: str, call_ids: Sequence[str], submitted_ts: str, results: Sequence[CallResult]) -> tuple[int, int]:
        by_id = {result.call_id: result for result in results}
        returned = failed = 0
        for call_id in call_ids:
            call = self.state.calls.get(call_id)
            result = by_id.get(call_id)
            if call is None or call.status != "submitted" or result is None:
                continue
            if self._record_result(handle_id, submitted_ts, call.call_id, result):
                returned += 1
            else:
                failed += 1
        return returned, failed

    def _fail_unfinished(self, handle_id: str, call_ids: Sequence[str], error: str, kind: str = KIND_PROVIDER) -> int:
        count = 0
        for call_id in call_ids:
            call = self.state.calls.get(call_id)
            if call is None or call.status != "submitted":
                continue
            self._emit(
                "call_failed",
                {
                    "call_id": call_id,
                    "handle_id": handle_id,
                    "error": error,
                    "retryable": True,
                    "billed": False,
                    "kind": kind,
                    "usage": {},
                    "usd": 0.0,
                    "price_table_date": self.prices.date,
                    "minutes": 0.0,
                },
            )
            count += 1
        return count

    def _record_result(self, handle_id: str, submitted_ts: str, call_id: str, result: CallResult) -> bool:
        """Turn one call result into a call_returned or call_failed event. True if it returned rows."""
        call = self.state.calls[call_id]
        task = self.state.tasks[call.task_id]
        if self.config.routes[call.route].kind == "api":
            self._api_freed = True  # a worker is free for the next call
        usd = self.ledger.cost(call.route, result.usage, tier=result.tier, on=self._today_date())
        minutes = result.minutes
        if minutes is None:
            minutes = max(0.0, (self._now() - _parse_ts(submitted_ts)).total_seconds() / 60.0)
        common: dict[str, Any] = {
            "call_id": call_id,
            "handle_id": handle_id,
            "usage": result.usage.to_dict(),
            "usd": usd,
            "price_table_date": self.prices.date,
            "minutes": round(minutes, 4),
        }
        if result.tier:
            common["tier"] = result.tier
        if result.error:
            kind = result.kind or (KIND_PROVIDER if result.retryable else KIND_UNEXPECTED)
            self._emit(
                "call_failed",
                {
                    **common,
                    "error": redact(result.error)[:PROVIDER_ERROR_CHARS],
                    "retryable": result.retryable,
                    "billed": usd > 0,
                    "kind": kind,
                },
            )
            if kind in STOP_ROUTE_KINDS:
                self._halt_route(call.route, kind, result.error, call_id=call_id)
            return False
        output, problem = self._normalized(task, call.line, call.route, result.output)
        rows: list[dict[str, Any]] = []
        skips: list[dict[str, Any]] = []
        unconverted: list[Any] = []
        if problem is None:

            def explain(wrappers: Sequence[Mapping[str, Any]]) -> str:
                text, unconverted[:] = self._wrapper_problem(task, call.line, call.route, wrappers, answer=result.output)
                return text

            rows, skips, problem = parse_output(output, explain_wrapper=explain)
        if problem:
            details: dict[str, Any] = {"unconverted_rows": unconverted} if unconverted else {}
            self._emit(
                "call_failed",
                {
                    **common,
                    "error": f"{UNUSABLE_PREFIX}{problem}"[:PROVIDER_ERROR_CHARS],
                    "retryable": True,
                    "billed": True,
                    "kind": KIND_INVALID_OUTPUT,
                    "output": (result.output or "")[:2000],
                    **details,
                },
            )
            return False
        stamped, flags = self._stamp(call.call_id, task, call.line, call.image_size, rows, route_name=call.route)
        linked = {"repair_of": call.repair_of} if call.repair_of is not None else {}  # the rows repair this row
        self._emit(
            "call_returned",
            {**common, "rows": stamped, "row_flags": flags, "skips": skips, "stop_reason": result.stop_reason, **linked},
        )
        return True

    def _normalized(self, task: TaskState, line_id: str, route_name: str, output: str | None) -> tuple[str | None, str | None]:
        """The writer's answer in the form `parse_output` reads, and a problem text when it cannot be.

        This is where a rule set's `normalize` function is applied: once, for every provider.
        """
        normalize = self.rules.normalize if self.rules is not None else None
        if normalize is None or output is None or not output.strip():
            return output, None
        try:
            converted = normalize(output, task.lines[line_id].spec, self.config.routes[route_name].model)
        except Exception as error:  # a rule function must not stop the run
            return None, f"the answer could not be turned into rows ({type(error).__name__}: {error})"
        if converted is None:
            return None, "the answer could not be turned into rows"
        return converted, None

    def _wrapper_problem(
        self,
        task: TaskState,
        line_id: str,
        route_name: str,
        wrappers: Sequence[Mapping[str, Any]],
        *,
        answer: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Why a writer's wrapper was not turned into rows: the problem text and the rows that failed.

        A rule set's `normalize` function gives the whole wrapper back when one of its rows cannot
        be converted. To name the rows, each is handed to the function alone, in a wrapper of its
        own: the ones that do not come back as rows are the ones to fix. The second value holds
        up to three of them as they were written (as text, cut short, when one is very long), for the log.

        When the rule set can say what is wrong with each row (its `explain` function, handed the
        writer's `answer`), those lines follow the sentence, as many whole lines as the error text has
        room for (it is what the retry is told, and it stays within `PROVIDER_ERROR_CHARS`).
        """
        normalize = self.rules.normalize if self.rules is not None else None
        spec = task.lines[line_id].spec
        model = self.config.routes[route_name].model
        total = 0
        names: list[str] = []
        shown: list[Any] = []
        for wrapper in wrappers:
            for position, row in enumerate(wrapper["rows"], 1):
                total += 1
                if normalize is None or _converts_alone(normalize, wrapper, row, spec, model):
                    continue
                row_id = row.get("id") if isinstance(row, Mapping) else None
                names.append(row_id if isinstance(row_id, str) and row_id else f"row {position}")
                text = json.dumps(row, ensure_ascii=False)
                shown.append(row if len(text) <= UNCONVERTED_ROW_CHARS else text[:UNCONVERTED_ROW_CHARS] + " ...")
        if names:
            listed = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
            text = (
                f"the answer is the writer's wrapper, and {len(names)} of its {total} rows cannot be turned into "
                f"stored rows ({listed}); check each of them, field by field, against the format in the rules"
            )
            advice = self._why_rows_fail(answer, spec, model)
            if advice:
                room = PROVIDER_ERROR_CHARS - len(UNUSABLE_PREFIX) - len(text) - len(ROW_ADVICE_LEAD) - 1
                block = _fit_lines(advice, room)
                if block:
                    text += f".{ROW_ADVICE_LEAD}{block}"
        elif total == 0:
            text = "the answer is the writer's wrapper with no rows in it and no skip"
        elif normalize is None:
            text = f"the answer is the writer's wrapper holding {total} rows, and the rules have no function that turns it into rows"
        else:
            text = f"the answer is the writer's wrapper holding {total} rows, and the rules did not turn it into stored rows"
        return text, shown[:3]

    def _why_rows_fail(self, answer: str | None, spec: Any, model: str) -> list[str]:
        """What the rule set says is wrong with each row of an answer it could not convert (empty when it says nothing)."""
        explain = self.rules.explain if self.rules is not None else None
        if explain is None or not answer or not answer.strip():
            return []
        try:
            lines = explain(answer, spec, model)
            return [lines] if isinstance(lines, str) else [str(line) for line in lines]
        except Exception:  # a rule function must not stop the run
            return []

    def _stamp(
        self,
        call_id: str,
        task: TaskState,
        line_id: str,
        image_size: int | None,
        rows: Sequence[dict[str, Any]],
        *,
        route_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        """Stamp each row a call returned with its writer label and source label.

        The writer label names the route the call was made on (`route_name`; the task's own route when omitted),
        so a row a repair wrote on another route (`repair.routes`) carries that route's provider, model, effort
        and rules, not those of the route that wrote the row it replaces.
        """
        route_name = route_name or task.route
        route = self.config.routes[route_name]
        line = task.lines[line_id]
        rules = self.rules_for(route_name)
        writer = WriterLabel(
            provider=route.provider,
            model=route.model,
            route=route.kind,
            effort=self._effort_of(task, route_name),
            date=self._today(),
            task_id=task.task_id,
            pool_version=task.pool_version,
            image_size=image_size,
            rules_version=rules.version if rules is not None else None,
            package_version=__version__,
            rules_profile=rules.profile if rules is not None else None,
        )
        source = SourceLabel(
            dataset=task.dataset or task.source,
            item_id=line.item_id or line.line_id,
            license=line.license or self.config.source(task.source).license or "",
        )
        stamped = []
        flags: dict[str, list[str]] = {}
        for index, row in enumerate(rows):
            row_id = ids.row_id(call_id, index)
            if reserved_keys_in(row):
                flags[row_id] = ["model_supplied_reserved_keys"]
            stamped.append(stamp_row(row, row_id=row_id, writer=writer, source=source))
        return stamped, flags

    # ------------------------------------------------------------------ assess

    def _stage_assess(self) -> None:
        for call in self.state.calls_with_unrecorded_skips():
            first = call.skips[0]
            self._emit(
                "row_set_aside",
                {
                    "call_id": call.call_id,
                    "class": str(first.get("class", "other")),
                    "reason": str(first.get("reason", ""))[:300],
                    "skips": call.skips,
                },
            )
        for row in self.state.rows_failed_unrejected():
            self._reject_after_assessment(row, row.errors)
        waiting = self.state.rows_awaiting_assessment()
        if self.assess_call is None:
            for row in waiting:
                try:
                    assessment = self.assess(row.record, self._assess_context(row))
                except Exception as error:  # a rule function must not stop the run
                    assessment = self._assessor_failure(error)
                self._settle_assessment(row, assessment)
            return
        # A call-level function sees every row one call returned, so checks that compare the
        # rows of a call (a series' nesting, one genre for the whole series) can run.
        by_call: dict[str, list[RowState]] = {}
        for row in waiting:
            by_call.setdefault(row.call_id, []).append(row)
        for call_id, rows in by_call.items():
            call = self.state.calls[call_id]
            context = self._assess_context(rows[0])
            records = [self.state.rows[row_id].record for row_id in call.row_ids]
            try:
                assessments = list(self.assess_call(records, context))
            except Exception as error:  # a rule function must not stop the run
                assessments = [self._assessor_failure(error)] * len(records)
            if len(assessments) != len(records):
                raise ControllerError(
                    f"the call-level assess function returned {len(assessments)} assessments "
                    f"for the {len(records)} rows of call {call_id}"
                )
            by_row = dict(zip(call.row_ids, assessments))
            for row in rows:
                self._settle_assessment(row, by_row[row.row_id])

    def _assessor_failure(self, error: Exception) -> Assessment:
        """The assessment of a row the checker could not read: failed, with the error as the reason.

        A row of a shape the checker does not expect (a field of the wrong type, say) is a bad row,
        not a reason to stop a run; it is rejected and written again like any row that fails a check.
        """
        text = f"the assessor could not check the row ({type(error).__name__}: {redact(str(error))[:200]})"
        if text not in self._reported:
            self._warn(f"{text}; a row it cannot check is rejected and written again")
        self._note_problem(text)
        return Assessment(passed=False, errors=(text,))

    def _assess_context(self, row: RowState) -> AssessContext:
        call = self.state.calls[row.call_id]
        task = self.state.tasks[row.task_id]
        line = task.lines[row.line]
        return AssessContext(
            task_id=task.task_id,
            source=task.source,
            batch=task.batch,
            line=row.line,
            attempt=call.attempt,
            route=row.route,
            pool_version=task.pool_version,
            spec=line.spec,
            item_id=line.item_id,
            assignment=assignment_data(assignment_for(line.spec, line.ordinal, self.rules)),
        )

    def _settle_assessment(self, row: RowState, assessment: Assessment) -> None:
        """Record what an assess function decided about one row."""
        if assessment.set_aside:
            self._emit(
                "row_set_aside",
                {
                    "row_id": row.row_id,
                    "call_id": row.call_id,
                    "class": str(assessment.set_aside.get("class", "other")),
                    "reason": str(assessment.set_aside.get("reason", ""))[:300],
                },
            )
            return
        errors = [str(error) for error in assessment.errors]
        self._emit(
            "row_assessed",
            {
                "row_id": row.row_id,
                "call_id": row.call_id,
                "passed": bool(assessment.passed),
                "flags": [str(flag) for flag in assessment.flags],
                "errors": errors,
            },
        )
        if not assessment.passed:
            self._reject_after_assessment(self.state.rows[row.row_id], errors)

    def _reject_after_assessment(self, row: RowState, errors: Sequence[str]) -> None:
        call = self.state.calls[row.call_id]
        self._emit(
            "row_rejected",
            {
                "row_id": row.row_id,
                "call_id": row.call_id,
                "by": "assessor",
                "reason": "failed assessment",
                "errors": list(errors),
                # A row a repair wrote is never written again: the repair is the one chance the row gets, and a
                # repair that fails the checker sends the row it was made from back to the owner.
                "retry": call.attempt < self.config.max_attempts and call.repair_of is None,
            },
        )

    # ------------------------------------------------------------------ review

    def _stage_review(self) -> None:
        self._ingest_verdicts()
        self._withdraw_packets()
        self._decide_rows()
        self._ingest_hand_decisions()
        self._reopen_stuck_rows()  # after the owner's decisions: a row the owner resolved is never reopened
        self._settle_repairs()
        self._request_repairs()  # after the owner's decisions: a row the owner resolved is never repaired
        self._assign_and_publish()
        self._burn_images()
        self._write_hand_queue()

    def _ingest_verdicts(self) -> None:
        open_packets = self.state.open_packets()
        withdrawn = self.state.withdrawn_packets()
        if not open_packets and not withdrawn:
            return
        existing = set(self.state_store.list(layout.review_prefix()))
        for packet in withdrawn:
            path = layout.verdicts_path(packet.reviewer_id, packet.packet_id)
            if path in existing:
                # The reviewer answered a packet that was taken back: its rows were sent to other reviewers.
                self._note_problem(f"{path}: the packet was withdrawn before this file was pushed, so its verdicts are ignored")
        for packet in open_packets:
            path = layout.verdicts_path(packet.reviewer_id, packet.packet_id)
            if path not in existing:
                continue
            try:
                text = self.state_store.read_text(path)
            except NotFound:
                continue
            verdicts, line_problems = parse_verdict_lines(text)
            for problem in line_problems:
                self._note_problem(f"{path}: {problem}")
            allowed = [item["row_id"] for item in packet.items]
            for verdict in verdicts:
                problems = validate_verdict(verdict, allowed_row_ids=allowed)
                if problems:
                    self._note_problem(f"{path}: row {verdict.get('row_id')!r}: {'; '.join(problems)}")
                    continue
                row = self.state.rows.get(verdict["row_id"])
                if row is None or not any(
                    a.open and a.packet_id == packet.packet_id and a.reviewer_id == packet.reviewer_id for a in row.assignments
                ):
                    continue
                self._emit(
                    "verdict_received",
                    {
                        "packet_id": packet.packet_id,
                        "row_id": verdict["row_id"],
                        "reviewer_id": packet.reviewer_id,
                        "family": packet.family,
                        "verdict": verdict["verdict"],
                        "reasons": list(verdict.get("reasons", [])),
                        "notes": str(verdict.get("notes", "")),
                    },
                )

    # -- packets of a reviewer who is switched off

    def _withdraw_packets(self) -> None:
        """Take back the packets of a reviewer who is switched off, or no longer configured, and has not answered.

        A packet is addressed to one reviewer. When the configuration now has that reviewer `enabled: false`,
        or does not name it, and no verdict file has been pushed for the packet, nobody is going to answer it:
        `packet_withdrawn` is recorded, its files are deleted from the state store (the controller owns
        `packets/`, so the pictures go too) and its rows wait for a review again, to be given to a reviewer
        that is on. A reviewer whose verdict file exists is never withdrawn, even when the file does not
        cover every row: that file was read above and its reviewer is waited for. A file that turns up for a
        withdrawn packet afterwards is ignored (see `_ingest_verdicts`).
        """
        self._sweep_withdrawn_files()
        review = self.config.review
        orphaned = [packet for packet in self.state.open_packets() if not review.is_active(packet.reviewer_id)]
        if not orphaned:
            return
        pushed: set[str] = set()
        for reviewer_id in sorted({packet.reviewer_id for packet in orphaned}):
            pushed.update(self.state_store.list(layout.review_prefix(reviewer_id)))
        taken: dict[str, list[int]] = {}  # reviewer id -> [packets, rows] withdrawn in this pass
        gone: list[PacketState] = []
        for packet in orphaned:
            if layout.verdicts_path(packet.reviewer_id, packet.packet_id) in pushed:
                continue
            row_ids = self._rows_waiting_on(packet)
            # The event first, then the files: a run that stops in between finds the leftovers (see below).
            self._emit("packet_withdrawn", {"packet_id": packet.packet_id, "reviewer_id": packet.reviewer_id, "row_ids": row_ids})
            gone.append(packet)
            counts = taken.setdefault(packet.reviewer_id, [0, 0])
            counts[0] += 1
            counts[1] += len(row_ids)
        self._delete_packet_files(gone)
        for reviewer_id, (packets, rows) in taken.items():
            why = "switched off" if review.reviewer(reviewer_id) is not None else "no longer in the configuration"
            self._print(
                f"[rps] withdrew {packets} packet(s) with {rows} row(s) addressed to reviewer {reviewer_id}, "
                f"which is {why} and sent no verdicts; the rows go to the other reviewers"
            )

    def _rows_waiting_on(self, packet: PacketState) -> list[str]:
        """The rows of a packet that are still waiting for its reviewer's verdict, sorted."""
        waiting = []
        for item in packet.items:
            row = self.state.rows.get(item["row_id"])
            if row is not None and any(a.open and a.packet_id == packet.packet_id for a in row.assignments):
                waiting.append(item["row_id"])
        return sorted(waiting)

    def _delete_packet_files(self, packets: Sequence[PacketState]) -> None:
        """Delete the packets' files (`packet.json` and the pictures) from the state store.

        One listing for each reviewer the packets were addressed to, not one for each packet.
        """
        folders: dict[str, list[str]] = {}
        for packet in packets:
            folders.setdefault(packet.reviewer_id, []).append(f"{layout.packet_dir(packet.reviewer_id, packet.packet_id)}/")
        for reviewer_id, prefixes in folders.items():
            wanted = tuple(prefixes)
            for path in self.state_store.list(layout.packets_prefix(reviewer_id)):
                if path.startswith(wanted):
                    self.state_store.delete(path)

    def _sweep_withdrawn_files(self) -> None:
        """Once a run, delete whatever a withdrawn packet still has in the state store.

        A packet is withdrawn by recording the event and then deleting its files, so a run that stopped
        between the two left files that nothing will read, among them the pictures of rows.
        """
        if self._withdrawn_swept:
            return
        self._withdrawn_swept = True
        self._delete_packet_files(self.state.withdrawn_packets())

    def _writer_family(self, row: RowState) -> str:
        return self.config.review.family_of(row.record["writer"]["provider"])

    @staticmethod
    def _not_yet_asked(row: RowState, reviewers: Sequence[ReviewerConfig]) -> list[ReviewerConfig]:
        """The reviewers the row has not been sent to: no reviewer reads the same row twice.

        The family rule sees to that for as long as a reviewer's family is what it was when the row was
        sent to it. This keeps it true when the configuration has since moved a reviewer to another family.
        """
        asked = row.reviewers_used()
        return [reviewer for reviewer in reviewers if reviewer.id not in asked]

    def _settlement(self, row: RowState) -> Settlement:
        """What the review policy, as the configuration has it now, makes of a row's verdicts so far.

        A row an audit reviewer rejected counts every verdict it has; any other row counts the verdicts
        of its own reviews, not its audit. A keep from a reviewer that may not keep alone (`solo_keep`
        off) never accepts a row by itself.
        """
        counted = row.live_verdicts(audit=None) if row.reopened else row.live_verdicts()
        return settle(
            self.config.review.policy,
            flagged=row.flagged,
            verdicts=[v["verdict"] for v in counted],
            reopened=row.reopened,
            solo=[self.config.review.may_keep_alone(v["reviewer_id"]) for v in counted],
        )

    def _decide_rows(self) -> None:
        reviewers = self.config.review.enabled_reviewers()
        for row in self.state.rows_with_audit_reject():
            self._emit(
                "row_escalated",
                {
                    "row_id": row.row_id,
                    "call_id": row.call_id,
                    "reason": "audit_reject",
                    "round": len(row.main_verdicts()) + 1,
                    "families_used": sorted(row.families_used()),
                },
            )
        for row in self.state.rows_awaiting_decision():
            outcome = self._settlement(row)
            history = [f"{v['reviewer_id']}: {v['verdict']}" for v in row.verdicts]
            if outcome.outcome == ACCEPT:
                self._emit("row_accepted", {"row_id": row.row_id, "call_id": row.call_id, "by": "reviewers", "reason": outcome.reason})
            elif outcome.outcome == DISCARD:
                errors = [f"{v['reviewer_id']}: {', '.join(v['reasons'])} {v['notes']}".strip() for v in row.live_verdicts(audit=None) if v["verdict"] == "reject"]
                self._emit(
                    "row_rejected",
                    {
                        "row_id": row.row_id,
                        "call_id": row.call_id,
                        "by": "reviewers",
                        "reason": outcome.reason,
                        "errors": errors,
                        "retry": False,
                    },
                )
            elif outcome.outcome == HAND:
                self._emit("row_to_hand", {"row_id": row.row_id, "call_id": row.call_id, "reason": outcome.reason, "verdicts": history})
            elif outcome.outcome == FURTHER:
                key = f"{row.row_id}:{len(row.assignments)}"
                candidate = pick_further_reviewer(
                    self._not_yet_asked(row, reviewers), row.families_used(), self._writer_family(row), key
                )
                if candidate is None:
                    self._emit(
                        "row_to_hand",
                        {"row_id": row.row_id, "call_id": row.call_id, "reason": NO_FURTHER_REVIEWER, "verdicts": history},
                    )
                else:
                    self._emit(
                        "row_escalated",
                        {
                            "row_id": row.row_id,
                            "call_id": row.call_id,
                            "reason": "in_question",
                            "round": len(row.main_verdicts()) + 1,
                            "families_used": sorted(row.families_used()),
                        },
                    )

    # -- hand queue

    def _ingest_hand_decisions(self) -> None:
        waiting = {row.row_id: row for row in self.state.hand_queue()}
        if not waiting:
            return
        for path in self.state_store.list(layout.CONTROL_HAND_PREFIX):
            if not path.endswith(".jsonl") or not waiting:
                continue
            try:
                text = self.state_store.read_text(path)
            except NotFound:
                continue
            decisions, line_problems = parse_verdict_lines(text)
            for problem in line_problems:
                self._note_problem(f"{path}: {problem}")
            for decision in decisions:
                row_id = decision.get("row_id")
                row = waiting.get(row_id) if isinstance(row_id, str) else None
                if row is None:
                    continue
                kind = decision.get("decision")
                if kind not in HAND_DECISIONS:
                    self._note_problem(f"{path}: row {decision.get('row_id')!r}: decision must be one of {list(HAND_DECISIONS)}")
                    continue
                payload: dict[str, Any] = {"row_id": row.row_id, "call_id": row.call_id, "decision": kind}
                if kind == "replace":
                    content = decision.get("row")
                    if not isinstance(content, dict) or not content:
                        self._note_problem(f"{path}: row {row.row_id!r}: a replacement needs a non-empty 'row' object")
                        continue
                    task = self.state.tasks[row.task_id]
                    try:
                        payload["row"] = stamp_human_row(
                            content,
                            row_id=f"{row.row_id}-h1",
                            task_id=row.task_id,
                            source=SourceLabel.from_dict(row.record["source_label"]),
                            date=self._today(),
                            pool_version=task.pool_version,
                        )
                    except ProvenanceError as error:
                        self._note_problem(f"{path}: row {row.row_id!r}: {error}")
                        continue
                elif kind == "review":
                    set_aside = decision.get("set_aside")
                    if not isinstance(set_aside, list) or not set_aside or not all(isinstance(name, str) and name for name in set_aside):
                        self._note_problem(
                            f"{path}: row {row.row_id!r}: a review needs 'set_aside', a non-empty list of reviewer ids"
                        )
                        continue
                    names = list(dict.fromkeys(set_aside))
                    judged = {v["reviewer_id"] for v in row.main_verdicts()}
                    unknown = [name for name in names if name not in judged]
                    if unknown:
                        self._note_problem(
                            f"{path}: row {row.row_id!r}: no verdict of its own reviews to set aside from {unknown}"
                        )
                        continue
                    counted = {v["reviewer_id"] for v in row.live_verdicts()}
                    if not any(name in counted for name in names):
                        # The line was carried out before: every verdict it names is set aside already, and the
                        # row is in the queue again for a verdict given since. That one is the owner's to answer.
                        continue
                    payload["set_aside"] = names
                self._emit("hand_resolved", payload)
                del waiting[row.row_id]

    def _reopen_stuck_rows(self) -> None:
        """Look again, on each pass, at the rows that went to the hand queue only for want of a reviewer.

        A row sent there because no reviewer from another family than the writer's (for a first review), or
        none from a family not yet used on it (for a further one), was available is not a row the reviewers
        failed to settle. It is reopened when both of these hold now: the review policy, as the configuration
        has it today, still asks for a review of the row (settling its verdicts again gives `need_first` or
        `further`), and the configuration has a reviewer that can take that review (one was enabled or added).
        `row_reopened` is then recorded and the row waits for a review again, so the usual assignment picks
        it up. A row the policy settles today is never reopened, whoever is available: one it would accept or
        discard, or one it hands to the owner (a row with a "fix" verdict when `fix_to_hand` is on, or one that
        has had `max_reviewers` reviewers) stays in the queue for the owner. Rows the owner has resolved are
        no longer in the queue and are never reopened (the owner's decisions were read first), and a row sent
        there for any other reason, three reviewers without a settlement for one, stays until the owner answers.
        """
        reviewers = self.config.review.enabled_reviewers()
        if not reviewers:
            return
        reopened = 0
        for row in self.state.hand_queue():
            reason = REOPEN_REASONS.get((row.hand or {}).get("reason", ""))
            if reason is None or self._settlement(row).outcome not in (NEED_FIRST, FURTHER):
                continue
            if not self._has_reviewer_for(row, reviewers):
                continue
            self._emit("row_reopened", {"row_id": row.row_id, "call_id": row.call_id, "reason": reason})
            reopened += 1
        if reopened:
            self._print(f"[rps] reopened {reopened} row(s) that waited in the hand queue for a reviewer that can take them")

    def _has_reviewer_for(self, row: RowState, reviewers: Sequence[ReviewerConfig]) -> bool:
        """True when one of `reviewers` could take the row's next review.

        The review a reopened row waits for is the first when it never had one and a further one otherwise,
        and the choice is the one `_plan_assignments` makes for it, so a row reopened here is never sent
        straight back to the hand queue.
        """
        family = self._writer_family(row)
        if any(not a.audit for a in row.assignments):
            key = f"{row.row_id}:{len(row.assignments)}"
            return pick_further_reviewer(self._not_yet_asked(row, reviewers), row.families_used(), family, key) is not None
        return pick_first_reviewer(reviewers, family, row.ordinal or 0, avoid=self._reviewers_of_original(row)) is not None

    def _reviewers_of_original(self, row: RowState) -> set[str]:
        """The reviewers (by id) that reviewed the row this one was written to replace; empty for any other row.

        The first review of a row a repair wrote prefers a reviewer that did not see the row it repairs.
        """
        original = self.state.rows.get(row.repair_of) if row.repair_of is not None else None
        return original.reviewers_used() if original is not None else set()

    # -- the repair step

    def _repair_allowed(self, route_name: str) -> bool:
        """True when the repair step is on for the route that would write rows again and that route is not closed
        for this run: it is switched on, has a cap for the purpose `repair`, and has not stopped (no key, rejected
        request). The route asked about is the repairing route (`repair.routes`), never the route that wrote the row."""
        return route_may_repair(self.config, route_name) and self.route_state(route_name)[0] != "closed"

    def _route_has_nothing_else_to_do(self, route_name: str) -> bool:
        """True when the controller has nothing left to do for a route before review: no call on it in flight or
        waiting to go (a repair call included), no call due on it (a spec line of a task of the route, or a repair
        call the route was sent), and no row it wrote still to be checked. Rows with the reviewers are not the
        controller's to move, so they do not count."""
        s = self.state
        if s.unfinished_calls(route_name):
            return False
        if any(row.route == route_name for row in (*s.rows_awaiting_assessment(), *s.rows_failed_unrejected())):
            return False
        if any(call.route == route_name for call in s.calls_with_unrecorded_skips()):
            return False
        return not any(self._call_route(task, status) == route_name for task, _line, status in self._calls_due())

    def _request_repairs(self) -> None:
        """Send the rows the reviewers sent back with a fix verdict to the route that is to write them again.

        The candidates are the rows in the hand queue for a fix verdict that the owner has not answered (the
        owner's decisions were read first), that were never sent back before and are not themselves a repair
        (`rps.repair.candidates`). A row is written again by the route that wrote it, or by the route
        `repair.routes` names for that route, and that repairing route, not the route that wrote the row, has to
        be on and have a cap for the purpose `repair` (`_repair_allowed`): a row of a route that is switched off
        is repaired through the map. A repairing route's candidates, from every route it repairs for, go
        together, in row-id order, when it has at least `repair.min_rows` of them, or fewer when the controller
        has nothing else to do for the route (`_route_has_nothing_else_to_do`): a batch route is sent its
        repairs in batches, and the last few rows of a job are not left waiting for a batch that will never fill.
        `repair_requested` is recorded for each row, naming the repairing route and the call that will write the
        row again; the call is prepared in the next pass, like any call, under that route's cap for `repair`.
        A run that stops between the events of a batch leaves some of its rows sent back and the others
        candidates still: the next run counts the others again, and they go with the next batch or when the
        route has nothing else to do, so the end of the job is the same.
        """
        if not self.config.repair.enabled:
            return
        by_route: dict[str, list[RowState]] = {}
        for row in repair_candidates(self.state.hand_queue()):
            repairing = self.config.repair.route_for(row.route)
            if self._repair_allowed(repairing):
                by_route.setdefault(repairing, []).append(row)
        for route_name in sorted(by_route):
            rows = by_route[route_name]
            if len(rows) < self.config.repair.min_rows and not self._route_has_nothing_else_to_do(route_name):
                continue
            for row in rows:
                attempt = self.state.next_attempt(row.task_id, row.line)
                call_id = ids.call_id(row.task_id, row.line, attempt)
                self._emit("repair_requested", {"row_id": row.row_id, "call_id": call_id, "route": route_name})
            others = sorted({row.route for row in rows} - {route_name})
            if others:
                text = f"sent {len(rows)} row(s) to {route_name} to be written again, with the reviewers' notes"
                text += f" (including rows written by {', '.join(others)})"
            else:
                text = f"sent {len(rows)} row(s) back to {route_name} to be written again, with the reviewers' notes"
            self._print(f"[rps] {text}")

    def _settle_repairs(self) -> None:
        """Send back to the hand queue the rows whose repair came to nothing, each with the reason.

        A repair comes to nothing when its call failed and may not be tried again, when the writer answered with
        no row, or when every row it wrote was rejected or set aside by the checker. A row whose call is still
        to be made (the first, or a retry), and whose repairing route no longer repairs (the step was switched
        off, or the route the row was sent back to was switched off or lost its repair cap, since the row was
        sent back), goes back too. Either way the row had its one repair: it stays for the owner.
        """
        sent = []
        for row, code, detail in self.state.repairs_failed(self.config.max_attempts):
            sent.append((row, repair_failure_reason(code, detail)))
        for task, _line, status in self.state.repairs_needing_calls(self.config.max_attempts):
            if self._repair_switched_off(task, status):
                sent.append((self.state.rows[status.repair_of], repair_failure_reason("switched_off")))
        for row, reason in sent:
            history = [f"{v['reviewer_id']}: {v['verdict']}" for v in row.verdicts]
            self._emit("row_to_hand", {"row_id": row.row_id, "call_id": row.call_id, "reason": reason, "verdicts": history})
        if sent:
            self._print(f"[rps] {len(sent)} row(s) sent back to be written again are in the hand queue again; the repair came to nothing")

    def _write_hand_queue(self) -> None:
        queue = sorted(self.state.hand_queue(), key=lambda row: row.row_id)
        lines = []
        for row in queue:
            task = self.state.tasks[row.task_id]
            line = task.lines[row.line]
            entry: dict[str, Any] = {
                "row_id": row.row_id,
                "task_id": row.task_id,
                "line": row.line,
                "route": row.route,
                "reason": (row.hand or {}).get("reason", ""),
                "flags": row.flags,
                "verdicts": row.verdicts,
                "task": {"line_id": line.line_id, "item_id": line.item_id, "image": line.image, "spec": line.spec},
                "row": row.record,
            }
            # The repair step, for the owner: a row a repair wrote says which row it was written to replace, and
            # a row whose repair came to nothing lists the calls and rows of that repair (see the events).
            if row.repair_of is not None:
                entry["repair_of"] = row.repair_of
            if row.repair is not None:
                entry["repair"] = {key: row.repair[key] for key in ("calls", "rows") if key in row.repair}
            lines.append(json.dumps(entry, ensure_ascii=True, allow_nan=False))
        text = "".join(item + "\n" for item in lines)
        if self._hand_written is None:
            try:
                self._hand_written = self.state_store.read_text(layout.HAND_QUEUE_PATH)
            except NotFound:
                self._hand_written = ""
        if text == self._hand_written:
            return
        self.state_store.write_text(layout.HAND_QUEUE_PATH, text)
        self._hand_written = text

    # -- assignment and packets

    def _upstream_idle(self) -> bool:
        s = self.state
        return not (
            self._pending_calls()
            or s.batches_to_poll()
            or self._calls_due()
            or s.rows_awaiting_assessment()
            or s.rows_failed_unrejected()
            or s.calls_with_unrecorded_skips()
        )

    def _calls_due(self) -> list[Any]:
        """Calls due (spec lines and repair calls), leaving out calls a budget refused and calls on closed routes.

        A route that is closed for the run (no key, switched off, stopped) cannot do
        them, so they do not keep the run waiting; a route that opens later does. The route of a call is
        the one it would be made on (`_call_route`).
        """
        status: dict[str, str] = {}
        due = []
        for item in self.state.lines_needing_calls(self.config.max_attempts):
            task, line, line_status = item
            if ids.call_id(task.task_id, line.line_id, line_status.next_attempt) in self._parked:
                continue
            if self._repair_switched_off(task, line_status):
                continue
            route_name = self._call_route(task, line_status)
            if route_name not in status:
                status[route_name] = self._route_status(route_name)
            if status[route_name] != "closed":
                due.append(item)
        return due

    def _pending_calls(self) -> list[Any]:
        """Calls prepared but not yet submitted, on routes that are not closed for the run."""
        status: dict[str, str] = {}
        pending = []
        for call in self.state.pending_calls():
            if call.route not in status:
                status[call.route] = self._route_status(call.route)
            if status[call.route] != "closed":
                pending.append(call)
        return pending

    def _plan_assignments(self) -> tuple[dict[str, list[tuple[RowState, int, bool]]], list[tuple[RowState, str]]]:
        """Which rows go to which reviewer next. Pure: records nothing.

        Returns (rows by reviewer id, rows that have no eligible reviewer and go to the
        hand queue). Every choice depends only on the row and the event history, so a
        restart makes the same choices. The first review of a row a repair wrote prefers a
        reviewer that did not review the row it was written to replace.
        """
        reviewers = self.config.review.enabled_reviewers()
        policy = self.config.review.policy
        plan: dict[str, list[tuple[RowState, int, bool]]] = {}
        to_hand: list[tuple[RowState, str]] = []
        if not reviewers:
            return plan, to_hand
        for row in self.state.rows_awaiting_first_review():
            reviewer = pick_first_reviewer(
                reviewers, self._writer_family(row), row.ordinal or 0, avoid=self._reviewers_of_original(row)
            )
            if reviewer is None:
                to_hand.append((row, NO_FIRST_REVIEWER))
                continue
            plan.setdefault(reviewer.id, []).append((row, 1, False))
        for row in self.state.rows_in_question():
            key = f"{row.row_id}:{len(row.assignments)}"
            reviewer = pick_further_reviewer(
                self._not_yet_asked(row, reviewers), row.families_used(), self._writer_family(row), key
            )
            if reviewer is None:
                to_hand.append((row, NO_FURTHER_REVIEWER))
                continue
            plan.setdefault(reviewer.id, []).append((row, len([a for a in row.assignments if not a.audit]) + 1, False))
        selected = lambda row_id: audit_selected(row_id, policy.audit_fraction, policy.audit_seed)  # noqa: E731
        for row in self.state.rows_needing_audit(selected):
            key = f"{row.row_id}:audit"
            reviewer = pick_further_reviewer(
                self._not_yet_asked(row, reviewers), row.families_used(), self._writer_family(row), key
            )
            if reviewer is not None:
                plan.setdefault(reviewer.id, []).append((row, len(row.assignments) + 1, True))
        return plan, to_hand

    def _assign_and_publish(self) -> None:
        plan, to_hand = self._plan_assignments()
        for row, reason in to_hand:
            self._emit("row_to_hand", {"row_id": row.row_id, "call_id": row.call_id, "reason": reason, "verdicts": []})
        if not plan:
            return
        packet_size = self.config.review.packet_size
        partial_ok = self._upstream_idle()
        for reviewer in self.config.review.enabled_reviewers():
            items = plan.get(reviewer.id, [])
            while items and (len(items) >= packet_size or partial_ok):
                chunk, items = items[:packet_size], items[packet_size:]
                self._publish_packet(reviewer.id, reviewer.family, chunk)

    def _publish_packet(self, reviewer_id: str, family: str, chunk: Sequence[tuple[RowState, int, bool]]) -> None:
        entries = []
        withheld = []
        for row, _round, _audit in chunk:
            task = self.state.tasks[row.task_id]
            line = task.lines[row.line]
            image = ext = None
            if line.image and not allowed_image(line.image_class, family):
                # The routing table governs reviewers too: this family reviews the row from its text.
                withheld.append(row.row_id)
            elif line.image:
                try:
                    image, ext, _size = self.image_loader(self.config.source(task.source), line)
                except ImageError as error:
                    self._warn(f"row {row.row_id}: no picture in the packet ({error})")
            entries.append(
                (row.record, {"line_id": line.line_id, "item_id": line.item_id, "spec": line.spec}, image, ext)
            )
        packet_id = ids.packet_id([self._assignment_key(reviewer_id, row, round_, audit) for row, round_, audit in chunk])
        brief_version = self._brief_version()
        packet = build_packet(
            packet_id=packet_id,
            reviewer_id=reviewer_id,
            brief_version=brief_version,
            created=self._timestamp(),
            rows=entries,
            withheld=withheld,
        )
        publish_packet(self.state_store, packet)
        # The packet's files must be durable before any event announces it.
        self.state_store.flush(force=True)
        items = []
        for entry, (row, round_, audit) in zip(packet.items(), chunk):
            # The rules each row was written with stay in this record: packet.json is the reviewer's blind view,
            # and a profile's name would say which provider wrote the row.
            label = row.record.get("writer") or {}
            written = {key: label[key] for key in ("rules_version", "rules_profile") if label.get(key) is not None}
            items.append({**entry, "round": round_, "audit": audit, **written})
        self._emit(
            "packet_published",
            {
                "packet_id": packet_id,
                "reviewer_id": reviewer_id,
                "family": family,
                "brief_version": brief_version,
                "items": items,
            },
        )

    @staticmethod
    def _assignment_key(reviewer_id: str, row: RowState, round_: int, audit: bool) -> str:
        """What identifies one row in a packet's id: the reviewer, the row, the round and whether it is an audit.

        A row that was taken back from a withdrawn packet adds how many times that happened, so that when
        it goes to the same reviewer again (one that was switched on again, say) its packet never has the
        id of the withdrawn one, whose files and event are still on record. A row never withdrawn keeps the
        key it has always had, so a crash between writing a packet and announcing it still finds the same id.
        """
        key = f"{reviewer_id}:{row.row_id}:{round_}:{int(audit)}"
        return f"{key}:w{row.withdrawals}" if row.withdrawals else key

    def _brief_version(self) -> str:
        """The brief version a packet carries.

        When writing rules are loaded (the rule pool, say) it is their version, which a reviewer
        compares with the pool's VERSION file; the configured `review.brief_version` applies
        only when no rules are loaded. It is the rules' own version whatever prompt profile a row
        was written with: the review brief is the same for every profile.
        """
        return self.rules.version if self.rules is not None else self.config.review.brief_version

    def _burn_images(self) -> None:
        """Delete the shared copy of a picture once its reviewer's verdict has been read."""
        for packet, row_ids in self.state.images_to_burn():
            wanted = set(row_ids)
            deleted = 0
            for item in packet.items:
                if item["row_id"] in wanted and item.get("image"):
                    self.state_store.delete(f"{layout.packet_dir(packet.reviewer_id, packet.packet_id)}/{item['image']}")
                    deleted += 1
            self._emit("packet_burned", {"packet_id": packet.packet_id, "row_ids": sorted(wanted), "deleted": deleted})

    # ------------------------------------------------------------------ ship

    def _stage_ship(self) -> None:
        """Hand the accepted-row files and the ledger to the ship store, and record what it has sent.

        A write to a store that sends in batches (the dataset repository takes one commit a minute) is
        not a shipment: the store holds it until its commit is due, which may be many passes later, and a
        commit that falls due inside a write sends what is pending at that moment and leaves the rest of
        the pass's writes for the next one. So a file is handed over once, for as long as its content
        stays the same, and it counts as shipped when the store no longer holds it, on whichever pass
        that turns out to be: an accepted-row file then gets its `shipped` event and the ledger files
        are marked as written. Handing the same content over again on every pass would never get it
        recorded, and would put a commit with nothing new in it out every interval.
        """
        self._settle_shipments()
        try:
            self._hand_over_files()
            if not self._staged_shipments and not self._ledger_staged:
                return
            self.ship_store.flush(force=False)
        except OwnershipError:
            raise  # a bug, not a failing store
        except RateLimitedError as error:
            self._warn(str(error))
            return
        except StoreError as error:
            # A commit can fall due inside a write as well as in the flush; either way the files stay handed over.
            self._ship_failures += 1
            self._warn(f"shipping failed ({self._ship_failures}): {error}")
            if self._ship_failures >= self._max_failures:
                self._stop_reason = "ship_failing"
            return
        self._ship_failures = 0
        self._settle_shipments()

    def _learn_what_the_store_holds(self) -> None:
        """Once a run, note which ledger files the ship store already holds exactly as they are now.

        A run that starts again knows nothing of what an earlier run sent. Without this it would hand the
        ledger over once more and wait a commit interval to send a commit with nothing new in it.
        """
        if self._ledger_probed:
            return
        self._ledger_probed = True
        for path, text in self._ledger_text().items():
            try:
                if self.ship_store.read_text(path) == text:
                    self._ledger_written[path] = text
            except (StoreError, ValueError):
                pass  # no such file yet, or it cannot be read now: the file is handed over as usual

    def _hand_over_files(self) -> None:
        """Write to the ship store what it has not been handed yet: changed accepted-row files and ledger files."""
        self._learn_what_the_store_holds()
        groups = self.state.accepted_rows_by_file()
        staged_ids = {key: row_ids for key, (_path, row_ids) in self._staged_shipments.items()}
        for task_id, route in self.state.files_to_ship(staged_ids):
            path = layout.accepted_path(route, task_id)
            rows = groups.get((task_id, route), [])
            if rows:
                self.ship_store.write_text(path, "".join(line + "\n" for line in rows_to_lines([row.record for row in rows])))
            else:
                self.ship_store.delete(path)
            self._staged_shipments[(task_id, route)] = (path, tuple(row.row_id for row in rows))
        for path, text in self._ledger_text().items():
            if text != self._ledger_staged.get(path, self._ledger_written.get(path)):
                self.ship_store.write_text(path, text)
                self._ledger_staged[path] = text

    def _settle_shipments(self) -> None:
        """Record what the ship store has sent of the files handed to it, by asking the store.

        The ledger files count as written, and an accepted-row file gets its `shipped` event, once the
        store no longer holds them. What a call returned is not evidence of that: the call that handed a
        file over returns long before a store that sends in batches sends it.
        """
        store = self.ship_store
        for path in sorted(self._ledger_staged):
            if not store.is_pending(path):
                self._ledger_written[path] = self._ledger_staged.pop(path)
        for key in sorted(self._staged_shipments):
            path, row_ids = self._staged_shipments[key]
            if store.is_pending(path):
                continue
            task_id, route = key
            self._emit("shipped", {"task_id": task_id, "route": route, "row_ids": list(row_ids), "path": path, "count": len(row_ids)})
            del self._staged_shipments[key]

    def _send_shipments_at_the_end(self) -> None:
        """At the end of a run, send what the ship store still holds and record it, so the next run need not."""
        if not self._staged_shipments and not self._ledger_staged:
            return
        try:
            self.ship_store.flush(force=True)
        except RateLimitedError as error:
            self._warn(str(error))
            return
        except StoreError as error:
            self._warn(f"could not flush a store: {error}")
            return
        self._settle_shipments()

    def _ledger_text(self) -> dict[str, str]:
        """The ledger files as text by path: the summary, and one line a task."""
        summary = json.dumps(self.ledger.summary(self.state), indent=1, sort_keys=True) + "\n"
        tasks = "".join(json.dumps(line, sort_keys=True, allow_nan=False) + "\n" for line in self.ledger.task_lines(self.state))
        return {layout.LEDGER_SUMMARY_PATH: summary, layout.LEDGER_TASKS_PATH: tasks}

    def _ledger_is_sent(self) -> bool:
        """True when the ship store has sent the ledger files as they are now."""
        return self._ledger_text() == self._ledger_written

    # ------------------------------------------------------------------ idle detection

    def _has_work(self) -> bool:
        """True when the controller could do something right now without waiting for anyone else.

        The cheap questions come first and the first yes ends it, so a run with calls in flight does not
        read every spec line of a long job on every pass to find out that it is not idle.
        """
        s = self.state
        if s.batches_to_poll() or self._pending_calls():
            return True
        if s.rows_awaiting_assessment() or s.rows_failed_unrejected() or s.calls_with_unrecorded_skips():
            return True
        if s.rows_awaiting_decision() or s.rows_with_audit_reject() or s.images_to_burn() or s.files_to_ship():
            return True
        if s.repairs_failed(self.config.max_attempts):
            return True
        if self._calls_due():
            return True
        plan, to_hand = self._plan_assignments()
        if to_hand:
            return True
        packet_size = self.config.review.packet_size
        return any(items and (len(items) >= packet_size or self._upstream_idle()) for items in plan.values())

    def is_idle(self) -> bool:
        """True when nothing is left to do now (and, if the run waits for review, nothing is out for review).

        Files waiting for the ship store's next commit are work still to be done: a run is idle once the
        store has sent the last change, which with a dataset repository is within one commit interval.
        """
        if self._has_work():
            return False
        if self.config.wait_for_review and self.state.open_packets():
            return False
        return self._ledger_is_sent()

    # ------------------------------------------------------------------ reporting

    def _refused_by(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """The calls a budget refused in this run, grouped by the cap that refused them: (cap, refusals).

        The cap is named as the refusal recorded it: the cap of a purpose on a route, or the route's
        total. The largest group comes first. A refusal the state no longer holds counts as "a budget".
        """
        groups: dict[str, list[dict[str, Any]]] = {}
        for call_id in sorted(self._parked):
            refusal = self.state.refusals.get(call_id) or {}
            cap = _cap_name(
                refusal.get("reason"), refusal.get("route"), refusal.get("purpose"), refusal.get("purpose_cap"), refusal.get("route_cap")
            )
            groups.setdefault(cap, []).append(refusal)
        return sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))

    @staticmethod
    def _refusal_detail(cap: str, refusals: Sequence[Mapping[str, Any]]) -> str:
        """One line for a group of refusals: how many, by which cap, how full it was and what the calls were held at.

        A refusal records what was spent and, from 0.2.5 on, how much of that was held for calls in flight; an
        older one has only the sum, which is told as it was.
        """
        text = f"{len(refusals)} by {cap}"
        total = cap.startswith("the total")
        key, held_key = ("route_spent", "route_held") if total else ("purpose_spent", "purpose_held")
        spent = [float(r[key]) for r in refusals if isinstance(r.get(key), (int, float))]
        estimates = [float(r["est_usd"]) for r in refusals if isinstance(r.get("est_usd"), (int, float))]
        if spent and all(isinstance(r.get(held_key), (int, float)) for r in refusals):
            held = [float(r[held_key]) for r in refusals]
            text += f": {_cap_money(max(s - h for s, h in zip(spent, held)))} spent"
            if max(held) > 0:
                text += f" and {_cap_money(max(held))} held by calls in flight"
        elif spent:
            text += f": {_cap_money(max(spent))} was spent or held by calls in flight"
        if estimates:
            low, high = min(estimates), max(estimates)
            held_for = _cap_money(low) if low == high else f"{_cap_money(low)} to {_cap_money(high)}"
            text += f"{',' if spent else ':'} each call needed room for {held_for}"
        return text

    def _waiting_for_room(self) -> list[tuple[str, int]]:
        """The calls waiting for room under a cap this pass, grouped by the cap: (cap, calls), the largest first."""
        counts: Counter[str] = Counter()
        for entry in self._held_back.values():
            counts[entry["cap"]] += entry["count"]
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def _waiting(self) -> dict[str, int]:
        s = self.state
        waiting = {
            "calls in flight": len(s.submitted_calls()),
            "packets with the reviewers": len(s.open_packets()),
            "rows in the hand queue": len(s.hand_queue()),
            "rows sent back to their writers": len(s.rows_being_repaired()),
        }
        for cap, count in self._waiting_for_room():
            waiting[f"calls waiting for room under {cap}"] = count
        for cap, refusals in self._refused_by():
            waiting[f"calls refused by {cap}"] = len(refusals)
        waiting["files for the store's next commit"] = len(self._staged_shipments) + len(self._ledger_staged)
        return waiting

    def _progress_line(self, elapsed: float | None, budget: float | None) -> str:
        progress = self.state.job_progress(self.config.max_attempts)
        spends = [self.state.spend(route) for route in self.config.routes]
        return format_progress(
            lines_total=progress["lines_total"],
            lines_done=progress["lines_done"],
            rows=len(self.state.rows),
            elapsed=elapsed,
            budget=budget,
            spent_usd=sum(spend.actual for spend in spends),
            held_usd=sum(spend.reserved for spend in spends),
            waiting=self._waiting(),
        )

    def status(self) -> dict[str, Any]:
        """Counts, progress, spend and the state of each route, as plain data."""
        routes = {}
        for name in self.config.routes:
            state, reason = self.route_state(name)
            routes[name] = {"state": state, "reason": reason}
        return {
            "run": self.instance,
            "progress": self.state.job_progress(self.config.max_attempts),
            "counts": self.state.counts(),
            "spend": self.ledger.status_rows(self.state),
            "routes": routes,
            "price_table_date": self.prices.date,
            "problems": list(self.problems),
        }

    def format_status(self) -> str:
        """A readable status: progress, state counts and spend per (route, purpose) against its cap."""
        self.start()
        counts = self.state.counts()
        lines = [
            f"run {self.instance}",
            self._progress_line(None, None),
            f"rows: {counts['rows']}  calls: {counts['calls']}",
            f"batches waiting: {counts['batches_waiting']}  packets open: {counts['packets_open']}  hand queue: {counts['hand_queue']}",
        ]
        if counts["screened_lines"]:
            lines.append(f"lines the screen set aside before any call: {counts['screened_lines']}")
        sent_back, written_again = counts["rows"].get("repairing", 0), counts["rows"].get("repaired", 0)
        if sent_back or written_again:
            lines.append(f"rows sent back to their writers: {sent_back} being written again, {written_again} written again")
        lines.extend(["", self.ledger.format_status(self.state)])
        waiting = self._waiting_for_room()
        if waiting:
            lines.append(f"{sum(count for _cap, count in waiting)} call(s) waiting for room under a cap")
            lines.extend(f"  {count} under {cap}" for cap, count in waiting)
        if self._parked:
            lines.append(f"{len(self._parked)} call(s) refused by a budget this run")
            lines.extend("  " + self._refusal_detail(cap, refusals) for cap, refusals in self._refused_by())
        for name in self.config.routes:
            state, reason = self.route_state(name)
            if state != "open":
                lines.append(f"route {name}: {'closed' if state == 'closed' else 'closed for now'} ({reason})")
        for note in self.prices.warnings():
            lines.append(f"note: {note}")
        return "\n".join(lines)
