"""The controller: the loop that turns spec lines into reviewed, shipped rows.

One pass (`step`) runs these stages in order. Every stage works out what to do from
the state rebuilt from events and records what it did as events, so a stage that is
interrupted and run again repeats only the unfinished part.

    plan       tasks from the planner become task_created events
    submit     due calls are prepared (budget checked first) and submitted as batches
    poll       submitted batches are polled; finished ones are collected into rows
    assess     each written row goes through the assess function
    review     verdicts are read, rows are settled by the review policy, rows that
               need a reviewer are published in packets, the owner's hand decisions
               are read, and the pictures of answered rows are burned
    ship       accepted rows and the ledger go to the ship store

`run` repeats passes until there is nothing left to do, a stop file appears, the time
budget runs out or the run is interrupted. It never ends a run by crashing in the
middle of a write that matters: a batch id is made durable before the controller
waits for it, packet files are durable before the event that announces them, and
shipped events are written only after the files they describe are durable.

Reviewers see a blind view of each row; the controller keeps the row to writer
mapping in its events and matches verdicts back by row id.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import __version__, ids, layout
from .assess import AssessCallFn, AssessContext, AssessFn, Assessment, default_assess
from .config import Config, ConfigError
from .events import EventLog, Event, replay, utc_now
from .failures import (
    KIND_BAD_REQUEST,
    KIND_CANCELED,
    KIND_EXPIRED,
    KIND_INVALID_OUTPUT,
    KIND_PROVIDER,
    KIND_UNEXPECTED,
    STOP_ROUTE_KINDS,
)
from .images import ImageError, load_picture
from .keys import redact
from .layout import controller_actor
from .ledger import Ledger, PriceTable, effective_purpose
from .prompting import WriterRules, assignment_data, assignment_for, load_writer_rules, render_task_text
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
from .review.packets import build_packet, parse_verdict_lines, publish_packet, validate_verdict
from .review.policy import (
    ACCEPT,
    DISCARD,
    FURTHER,
    HAND,
    audit_selected,
    pick_first_reviewer,
    pick_further_reviewer,
    settle,
)
from .routing import ImageRoutingError, check_image_allowed
from .state import LineState, RowState, State, TaskState
from .store.base import ActorStore, NotFound, RateLimitedError, Store, StoreError
from .store.remote import describe_error
from .tasks import FilePlanner, Planner, TaskSpec

ImageLoader = Callable[[Any, Any], "tuple[bytes, str, int]"]
HAND_DECISIONS = ("accept", "discard", "replace")


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
    waiting: Mapping[str, int] | None = None,
) -> str:
    """One progress line: job size, done, time spent and time left.

    With `elapsed` of None (a status print that belongs to no run) the time parts are left out.
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
    busy = [f"{count} {name}" for name, count in (waiting or {}).items() if count]
    if busy:
        parts.append("waiting: " + ", ".join(busy))
    return " | ".join(parts)


def parse_output(text: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Split a model's output into (rows, skip objects, problem).

    The output is a JSON object or a list of objects; an object with "skip": true is a
    skip object, any other object is a row. A leading code fence is tolerated.
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
    for item in items:
        if not isinstance(item, dict):
            return [], [], "the output holds something other than JSON objects"
        (skips if item.get("skip") is True else rows).append(item)
    if not rows and not skips:
        return [], [], "the output holds no row"
    return rows, skips, None


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


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
    returned together (and wins when both are given). `rules` are the writing rules
    (system prompt, row schema, version); when omitted they are read from the
    configuration's `rules` section if it has one. `clock` (monotonic seconds), `now`
    (UTC datetime) and `sleep` can be replaced in tests; `on_event` is called after
    every event is written and applied.
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
        rules: WriterRules | None = None,
        planner: Planner | None = None,
        image_loader: ImageLoader | None = None,
        instance: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        printer: Callable[[str], Any] = print,
        on_event: Callable[[Event], None] | None = None,
        progress_seconds: float = 30.0,
        stop_check_seconds: float = 15.0,
        max_consecutive_failures: int = 5,
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
        self.assess: AssessFn = assess or default_assess
        self.assess_call: AssessCallFn | None = assess_call
        checker = self.rules.assessor if self.rules is not None else None
        if checker is not None and assess is None and assess_call is None:
            # The rule set's own checker, unless the caller brought one.
            if callable(getattr(checker, "assess_call", None)):
                self.assess_call = checker.assess_call
            if callable(checker):
                self.assess = checker
        self.planner: Planner | None = planner or (FilePlanner(config.tasks_file) if config.tasks_file else None)
        self.image_loader: ImageLoader = image_loader or self._default_image_loader
        self._clock = clock
        self._sleep = sleep
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
        self._prepared_here: set[str] = set()
        self._halted: dict[str, str] = {}  # routes stopped for this run, with the reason
        self._closed_notes: set[tuple[str, str]] = set()
        self._to_release: list[tuple[Writer, Handle]] = []
        self._submit_failures = 0
        self._ship_failures = 0
        self._last_warning: dict[str, str] = {}
        self._stop_reason: str | None = None
        self._last_stop_check = float("-inf")
        self._ledger_written: tuple[str, str] | None = None
        self._hand_written: str | None = None

    # ------------------------------------------------------------------ setup

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
            if self.rules is not None and route.is_enabled:
                self.writers[name].check_rules(self.rules)
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

    def _emit(self, kind: str, payload: dict[str, Any], *, durable: bool = False) -> Event:
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
        self._stage_plan()
        self._stage_submit()
        self._drain_notes()
        self._stage_poll()
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

    def _stage_plan(self) -> None:
        if self.planner is None:
            return
        for spec in self.planner(self.state):
            self.add_task(spec)

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
    ) -> tuple[CallSpec, int | None]:
        """Build the call for a spec line, with the size of the picture it carries (if any).

        A picture is loaded only after the routing table has allowed it for the route
        (`ImageRoutingError` otherwise). With `boost` the token limit is raised by the
        route's truncation factor: the previous answer was cut off at the limit. A call
        rebuilt after a restart passes the limit it was first prepared with.
        """
        route = self.config.routes[task.route]
        image = ext = None
        size = None
        if line.image:
            check_image_allowed(line.image_class, route.provider)
            image, ext, size = self.image_loader(self.config.source(task.source), line)
        if not max_output_tokens:
            max_output_tokens = route.max_output_tokens
            if boost:
                max_output_tokens = max(max_output_tokens + 1, int(max_output_tokens * route.truncation_factor))
        spec = CallSpec(
            call_id=ids.call_id(task.task_id, line.line_id, attempt),
            task_id=task.task_id,
            line=line.line_id,
            attempt=attempt,
            route=task.route,
            provider=route.provider,
            model=route.model,
            kind=route.kind,
            effort=task.effort or route.effort,
            max_output_tokens=max_output_tokens,
            spec=line.spec,
            pool_version=task.pool_version,
            purpose=effective_purpose(task.purpose, attempt),
            feedback=tuple(feedback),
            image=image,
            image_ext=ext,
            image_class=line.image_class if line.image else None,
            task_text=render_task_text(line.spec, feedback, ordinal=line.ordinal, rules=self.rules),
            rules=self.rules,
        )
        return spec, size

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
        text = redact(reason)[:300]
        self._emit("route_halted", {"route": route_name, "kind": kind, "reason": text, "call_id": call_id})
        self._note_problem(f"route {route_name} stopped for this run ({kind}): {text}")
        self._print(f"[rps] route {route_name} stopped for this run ({kind}): {text}")

    def _stage_submit(self) -> None:
        if self._stop_reason:
            return
        for route_name, writer in self.writers.items():
            route = self.config.routes.get(route_name)
            if route is None or self._route_status(route_name) != "open":
                continue
            approved: dict[str, Request] = {}
            due = [
                (task, line, status)
                for task, line, status in self.state.lines_needing_calls(self.config.max_attempts)
                if task.route == route_name
                and ids.call_id(task.task_id, line.line_id, status.next_attempt) not in self._parked
            ]
            for group in _chunks(due, route.max_batch_calls):
                specs: list[CallSpec] = []
                sizes: dict[str, int | None] = {}
                retry_of: dict[str, str | None] = {}
                origin: dict[str, tuple[TaskState, LineState, Any]] = {}
                for task, line, status in group:
                    try:
                        spec, size = self._call_spec(task, line, status.next_attempt, status.feedback, boost=status.boost)
                    except (ImageError, ImageRoutingError) as error:
                        self._record_unpreparable(task, line, status, str(error))
                        continue
                    specs.append(spec)
                    sizes[spec.call_id] = size
                    retry_of[spec.call_id] = status.retry_of
                    origin[spec.call_id] = (task, line, status)
                if not specs:
                    continue
                for spec, request in self._prepare_group(writer, specs):
                    if isinstance(request, str):
                        task, line, status = origin[spec.call_id]
                        self._record_unpreparable(task, line, status, request)
                        continue
                    estimate = self.ledger.estimate(
                        route_name, request.est_input_tokens, request.max_output_tokens, on=self._today_date()
                    )
                    decision = self.ledger.check(self.state, route_name, spec.purpose, estimate)
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
                        },
                    )
                    self._prepared_here.add(spec.call_id)
                    approved[spec.call_id] = request
            self._submit_pending(route_name, writer, approved)

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

    def _record_unpreparable(self, task: TaskState, line: LineState, status: Any, problem: str) -> None:
        """A call that cannot be built (its picture cannot be read) fails at once, without retries."""
        call_id = ids.call_id(task.task_id, line.line_id, status.next_attempt)
        self._emit(
            "call_prepared",
            {
                "call_id": call_id,
                "task_id": task.task_id,
                "line": line.line_id,
                "attempt": status.next_attempt,
                "route": task.route,
                "purpose": effective_purpose(task.purpose, status.next_attempt),
                "est_usd": 0.0,
                "est_input_tokens": 0,
                "max_output_tokens": 0,
                "image_size": None,
                "feedback": [],
                "retry_of": status.retry_of,
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
                "kind": KIND_BAD_REQUEST,
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
                task, line, call.attempt, call.feedback, max_output_tokens=call.max_output_tokens or None
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
            if call_id not in self._prepared_here:
                inherited.append(request)
        if inherited:
            # Calls prepared by an earlier run may have been submitted just before it stopped.
            try:
                found = writer.recover(inherited)
            except NotImplementedError:
                raise
            except Exception as error:
                # Sending them again could pay twice; leave them for the next pass.
                self._submit_failures += 1
                self._warn(f"looking for earlier work on {route_name} failed ({self._submit_failures}): {describe_error(error)}")
                if self._submit_failures >= self._max_failures:
                    self._stop_reason = "submit_failing"
                return
            if found is not None:
                covered = set(found.call_ids)
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
                self._submit_failures += 1
                self._warn(f"submit to {route_name} failed ({self._submit_failures}): {describe_error(error)}")
                if self._submit_failures >= self._max_failures:
                    self._stop_reason = "submit_failing"
                return
            self._submit_failures = 0
            self._emit(
                "batch_submitted",
                {
                    "handle_id": handle.handle_id,
                    "handle": handle.to_dict(),
                    "route": route_name,
                    "call_ids": [request.call_id for request in group],
                    "recovered": False,
                },
                durable=True,
            )

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
            try:
                writer.release(handle)
            except NotImplementedError:
                raise
            except Exception as error:
                self._warn(f"clean-up after {handle.handle_id} failed: {describe_error(error)}")

    def _drain_notes(self) -> None:
        """Record what writers asked to have remembered (a cache they made)."""
        for writer in self.writers.values():
            for kind, payload in writer.drain_notes():
                self._emit(kind, payload, durable=True)

    def _stage_poll(self) -> None:
        for batch in list(self.state.batches_to_poll()):
            writer = self.writers.get(batch.route)
            if writer is None:
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
                if status.state == "partial":
                    if returned or failed:
                        self._flush_state()  # results already paid for are made durable as they land
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
            self._to_release.append((writer, handle))
        self._release_ready()

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
                    "error": redact(result.error)[:300],
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
        if problem is None:
            rows, skips, problem = parse_output(output)
        if problem:
            self._emit(
                "call_failed",
                {
                    **common,
                    "error": f"unusable output: {problem}",
                    "retryable": True,
                    "billed": True,
                    "kind": KIND_INVALID_OUTPUT,
                    "output": (result.output or "")[:2000],
                },
            )
            return False
        stamped, flags = self._stamp(call.call_id, task, call.line, call.image_size, rows)
        self._emit(
            "call_returned",
            {**common, "rows": stamped, "row_flags": flags, "skips": skips, "stop_reason": result.stop_reason},
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

    def _stamp(
        self, call_id: str, task: TaskState, line_id: str, image_size: int | None, rows: Sequence[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
        route = self.config.routes[task.route]
        line = task.lines[line_id]
        writer = WriterLabel(
            provider=route.provider,
            model=route.model,
            route=route.kind,
            effort=task.effort or route.effort,
            date=self._today(),
            task_id=task.task_id,
            pool_version=task.pool_version,
            image_size=image_size,
            rules_version=self.rules.version if self.rules is not None else None,
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
                self._settle_assessment(row, self.assess(row.record, self._assess_context(row)))
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
            assessments = list(self.assess_call(records, context))
            if len(assessments) != len(records):
                raise ControllerError(
                    f"the call-level assess function returned {len(assessments)} assessments "
                    f"for the {len(records)} rows of call {call_id}"
                )
            by_row = dict(zip(call.row_ids, assessments))
            for row in rows:
                self._settle_assessment(row, by_row[row.row_id])

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
                "retry": call.attempt < self.config.max_attempts,
            },
        )

    # ------------------------------------------------------------------ review

    def _stage_review(self) -> None:
        self._ingest_verdicts()
        self._decide_rows()
        self._ingest_hand_decisions()
        self._assign_and_publish()
        self._burn_images()
        self._write_hand_queue()

    def _ingest_verdicts(self) -> None:
        open_packets = self.state.open_packets()
        if not open_packets:
            return
        existing = set(self.state_store.list(layout.review_prefix()))
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

    def _writer_family(self, row: RowState) -> str:
        return self.config.review.family_of(row.record["writer"]["provider"])

    def _decide_rows(self) -> None:
        policy = self.config.review.policy
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
            verdicts = [v["verdict"] for v in (row.verdicts if row.reopened else row.main_verdicts())]
            outcome = settle(policy, flagged=row.flagged, verdicts=verdicts, reopened=row.reopened)
            history = [f"{v['reviewer_id']}: {v['verdict']}" for v in row.verdicts]
            if outcome.outcome == ACCEPT:
                self._emit("row_accepted", {"row_id": row.row_id, "call_id": row.call_id, "by": "reviewers", "reason": outcome.reason})
            elif outcome.outcome == DISCARD:
                errors = [f"{v['reviewer_id']}: {', '.join(v['reasons'])} {v['notes']}".strip() for v in row.verdicts if v["verdict"] == "reject"]
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
                candidate = pick_further_reviewer(reviewers, row.families_used(), self._writer_family(row), key)
                if candidate is None:
                    self._emit(
                        "row_to_hand",
                        {"row_id": row.row_id, "call_id": row.call_id, "reason": "no reviewer from an unused family is available", "verdicts": history},
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
                self._emit("hand_resolved", payload)
                del waiting[row.row_id]

    def _write_hand_queue(self) -> None:
        queue = sorted(self.state.hand_queue(), key=lambda row: row.row_id)
        lines = []
        for row in queue:
            task = self.state.tasks[row.task_id]
            line = task.lines[row.line]
            lines.append(
                json.dumps(
                    {
                        "row_id": row.row_id,
                        "task_id": row.task_id,
                        "line": row.line,
                        "route": row.route,
                        "reason": (row.hand or {}).get("reason", ""),
                        "flags": row.flags,
                        "verdicts": row.verdicts,
                        "task": {"line_id": line.line_id, "item_id": line.item_id, "image": line.image, "spec": line.spec},
                        "row": row.record,
                    },
                    ensure_ascii=True,
                    allow_nan=False,
                )
            )
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
        """Spec lines due a call, leaving out calls a budget refused and calls on closed routes.

        A route that is closed for the run (no key, switched off, stopped) cannot do
        them, so they do not keep the run waiting; a route that opens later does.
        """
        status: dict[str, str] = {}
        due = []
        for item in self.state.lines_needing_calls(self.config.max_attempts):
            task, line, line_status = item
            if ids.call_id(task.task_id, line.line_id, line_status.next_attempt) in self._parked:
                continue
            if task.route not in status:
                status[task.route] = self._route_status(task.route)
            if status[task.route] != "closed":
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
        restart makes the same choices.
        """
        reviewers = self.config.review.enabled_reviewers()
        policy = self.config.review.policy
        plan: dict[str, list[tuple[RowState, int, bool]]] = {}
        to_hand: list[tuple[RowState, str]] = []
        if not reviewers:
            return plan, to_hand
        for row in self.state.rows_awaiting_first_review():
            reviewer = pick_first_reviewer(reviewers, self._writer_family(row), row.ordinal or 0)
            if reviewer is None:
                to_hand.append((row, "no reviewer from another family than the writer is available"))
                continue
            plan.setdefault(reviewer.id, []).append((row, 1, False))
        for row in self.state.rows_in_question():
            key = f"{row.row_id}:{len(row.assignments)}"
            reviewer = pick_further_reviewer(reviewers, row.families_used(), self._writer_family(row), key)
            if reviewer is None:
                to_hand.append((row, "no reviewer from an unused family is available"))
                continue
            plan.setdefault(reviewer.id, []).append((row, len([a for a in row.assignments if not a.audit]) + 1, False))
        selected = lambda row_id: audit_selected(row_id, policy.audit_fraction, policy.audit_seed)  # noqa: E731
        for row in self.state.rows_needing_audit(selected):
            key = f"{row.row_id}:audit"
            reviewer = pick_further_reviewer(reviewers, row.families_used(), self._writer_family(row), key)
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
        for row, _round, _audit in chunk:
            task = self.state.tasks[row.task_id]
            line = task.lines[row.line]
            image = ext = None
            if line.image:
                try:
                    image, ext, _size = self.image_loader(self.config.source(task.source), line)
                except ImageError as error:
                    self._warn(f"row {row.row_id}: no picture in the packet ({error})")
            entries.append(
                (row.record, {"line_id": line.line_id, "item_id": line.item_id, "spec": line.spec}, image, ext)
            )
        packet_id = ids.packet_id([f"{reviewer_id}:{row.row_id}:{round_}:{int(audit)}" for row, round_, audit in chunk])
        packet = build_packet(
            packet_id=packet_id,
            reviewer_id=reviewer_id,
            brief_version=self.config.review.brief_version,
            created=self._timestamp(),
            rows=entries,
        )
        publish_packet(self.state_store, packet)
        # The packet's files must be durable before any event announces it.
        self.state_store.flush(force=True)
        items = []
        for entry, (row, round_, audit) in zip(packet.items(), chunk):
            items.append({**entry, "round": round_, "audit": audit})
        self._emit(
            "packet_published",
            {
                "packet_id": packet_id,
                "reviewer_id": reviewer_id,
                "family": family,
                "brief_version": self.config.review.brief_version,
                "items": items,
            },
        )

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
        dirty = self.state.files_to_ship()
        ledger_files = self._ledger_files()
        if not dirty and ledger_files is None:
            return
        shipments = []
        groups = self.state.accepted_rows_by_file()
        for task_id, route in dirty:
            path = layout.accepted_path(route, task_id)
            rows = groups.get((task_id, route), [])
            if rows:
                self.ship_store.write_text(path, "".join(line + "\n" for line in rows_to_lines([row.record for row in rows])))
            else:
                self.ship_store.delete(path)
            shipments.append((task_id, route, path, [row.row_id for row in rows]))
        if ledger_files is not None:
            summary, tasks = ledger_files
            self.ship_store.write_text(layout.LEDGER_SUMMARY_PATH, summary)
            self.ship_store.write_text(layout.LEDGER_TASKS_PATH, tasks)
        try:
            done = self.ship_store.flush(force=False)
        except RateLimitedError as error:
            self._warn(str(error))
            return
        except StoreError as error:
            self._ship_failures += 1
            self._warn(f"shipping failed ({self._ship_failures}): {error}")
            if self._ship_failures >= self._max_failures:
                self._stop_reason = "ship_failing"
            return
        self._ship_failures = 0
        if ledger_files is not None and done:
            self._ledger_written = ledger_files
        if not done:
            return
        for task_id, route, path, row_ids in shipments:
            self._emit("shipped", {"task_id": task_id, "route": route, "row_ids": row_ids, "path": path, "count": len(row_ids)})

    def _ledger_files(self) -> tuple[str, str] | None:
        """The ledger files as text, or None when they have not changed since they were written."""
        summary = json.dumps(self.ledger.summary(self.state), indent=1, sort_keys=True) + "\n"
        tasks = "".join(json.dumps(line, sort_keys=True, allow_nan=False) + "\n" for line in self.ledger.task_lines(self.state))
        current = (summary, tasks)
        return None if current == self._ledger_written else current

    # ------------------------------------------------------------------ idle detection

    def _actionable(self) -> int:
        """How much the controller could do right now without waiting for anyone else."""
        s = self.state
        plan, to_hand = self._plan_assignments()
        packet_size = self.config.review.packet_size
        publishable = sum(
            len(items) for items in plan.values() if len(items) >= packet_size or self._upstream_idle()
        )
        return (
            len(self._pending_calls())
            + len(s.batches_to_poll())
            + len(self._calls_due())
            + len(s.rows_awaiting_assessment())
            + len(s.rows_failed_unrejected())
            + len(s.calls_with_unrecorded_skips())
            + len(s.rows_awaiting_decision())
            + len(s.rows_with_audit_reject())
            + len(to_hand)
            + publishable
            + len(s.images_to_burn())
            + len(s.files_to_ship())
        )

    def is_idle(self) -> bool:
        """True when nothing is left to do now (and, if the run waits for review, nothing is out for review)."""
        if self._actionable():
            return False
        if self.config.wait_for_review and self.state.open_packets():
            return False
        return self._ledger_files() is None

    # ------------------------------------------------------------------ reporting

    def _waiting(self) -> dict[str, int]:
        s = self.state
        return {
            "calls in flight": len(s.submitted_calls()),
            "packets with the reviewers": len(s.open_packets()),
            "rows in the hand queue": len(s.hand_queue()),
            "calls refused by a budget": len(self._parked),
        }

    def _progress_line(self, elapsed: float | None, budget: float | None) -> str:
        progress = self.state.job_progress(self.config.max_attempts)
        spent = sum(self.state.spend(route).total for route in self.config.routes)
        return format_progress(
            lines_total=progress["lines_total"],
            lines_done=progress["lines_done"],
            rows=len(self.state.rows),
            elapsed=elapsed,
            budget=budget,
            spent_usd=spent,
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
            "",
            self.ledger.format_status(self.state),
        ]
        if self._parked:
            lines.append(f"{len(self._parked)} call(s) refused by a budget this run")
        for name in self.config.routes:
            state, reason = self.route_state(name)
            if state != "open":
                lines.append(f"route {name}: {'closed' if state == 'closed' else 'closed for now'} ({reason})")
        for note in self.prices.warnings():
            lines.append(f"note: {note}")
        return "\n".join(lines)
