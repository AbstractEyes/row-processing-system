"""The writer interface and a fake writer for tests.

A writer sends "write a row" calls to one model route. The controller drives it
through these steps:

    prepare(calls)    -> requests          build the provider requests (no network)
    submit(requests)  -> handle            send them; a batch id or per-call ids
    poll(handle)      -> PollStatus        is the batch finished?
    results(handle)   -> [CallResult]      raw outputs, usage and errors per call
    release(handle)                        clean up once the results are safely recorded

`handle` is plain data that can be stored in an event and handed back after a
restart, so a submitted batch is polled rather than submitted again. `recover`
lets an adapter look for a batch it submitted just before a crash (by a digest of
the calls kept with the batch, or, where the provider keeps nothing of the kind, by
the batch's size and creation time); the default finds nothing.

A writer whose work arrives piece by piece (calls sent one at a time) answers a poll
with `partial` and hands over the results that are ready; the controller records
each one as it lands and polls again until the handle is `ended`.

`accepting` lets a writer say it cannot take calls now (no key, switched off, outside
its hours); the controller then leaves that route's calls alone. A result whose `kind`
is in `STOP_ROUTE_KINDS` (a bad request, a rejected key) stops the route for the run.

Writers read API keys from environment variables only and never log them.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar, Collection, Mapping, Sequence

from ..config import RouteConfig
from ..ids import custom_id as make_custom_id
from ..ledger import Usage
from ..prompting import WriterRules


class ProviderError(Exception):
    """A provider call failed in a way that may succeed later."""


class RouteUnavailable(RuntimeError):
    """A route cannot send calls (switched off, no key, outside its hours)."""


class RouteRejected(ProviderError):
    """A provider turned a request down in a way no retry fixes.

    `kind` is "bad_request" (the request itself is wrong) or "auth" (the key is rejected
    or the balance is spent). The controller stops the route for the rest of the run.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class SimulatedCrash(BaseException):
    """Raised by tests to stand in for the process dying at an exact point.

    It derives from BaseException so no ordinary error handler swallows it.
    """


@dataclass(frozen=True)
class Acceptance:
    """Whether a writer can take calls right now, and why not if it cannot.

    `later` is True when the route will open by itself (a provider's off-peak hours),
    so the controller waits for it; otherwise a closed route stays closed for the run
    (no key, switched off) and its calls are left alone.
    """

    ok: bool
    reason: str = ""
    later: bool = False


@dataclass(frozen=True)
class CallSpec:
    """Everything a writer needs to build one request.

    `task_text` is the user text of the call (the spec line, the runner's choices and any
    messages from a failed attempt); `rules` carries the system prompt, the row schema and
    the rules version. A picture arrives in `image` only after the routing table allowed
    it for this route, and `image_class` says what class it was.
    """

    call_id: str
    task_id: str
    line: str
    attempt: int
    route: str
    provider: str
    model: str
    kind: str
    effort: str | None
    max_output_tokens: int
    spec: Any
    pool_version: str
    purpose: str
    feedback: tuple[str, ...] = ()
    image: bytes | None = field(default=None, repr=False)
    image_ext: str | None = None
    image_class: str | None = None
    task_text: str | None = None
    rules: WriterRules | None = field(default=None, repr=False)
    # Set on a call of the repair step: the id of the row the call asks the writer to write again. Its
    # `feedback` is then the reviewers' notes and the row as it was written, and its `purpose` is `repair`.
    repair_of: str | None = None
    # The routing table the project asked when it allowed the picture (None: the package's table).
    image_policy: Mapping[str, Mapping[str, bool]] | None = field(default=None, repr=False, compare=False)

    @property
    def custom_id(self) -> str:
        return make_custom_id(self.call_id)


@dataclass(frozen=True)
class Request:
    """A provider request, ready to submit.

    `payload` is plain data the adapter's own submit step understands; it holds no key.
    `size_bytes` is the request's size on the wire when the adapter knows it (batches
    have size limits). `attempt` lets a synchronous writer space out retries.
    """

    call_id: str
    custom_id: str
    payload: dict[str, Any]
    est_input_tokens: int
    max_output_tokens: int
    attempt: int = 1
    size_bytes: int = 0


@dataclass(frozen=True)
class Handle:
    """What `submit` returns: enough to poll and collect later, even after a restart.

    `mode` is "batch" (one provider batch id) or "per_call" (calls made one by one).
    `data` holds anything else the adapter needs, as JSON-serializable values.
    """

    handle_id: str
    call_ids: tuple[str, ...]
    mode: str = "batch"
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"handle_id": self.handle_id, "call_ids": list(self.call_ids), "mode": self.mode, "data": dict(self.data)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Handle":
        return cls(
            handle_id=data["handle_id"],
            call_ids=tuple(data["call_ids"]),
            mode=data.get("mode", "batch"),
            data=dict(data.get("data", {})),
        )


@dataclass(frozen=True)
class PollStatus:
    """`state` is in_progress, partial, ended, failed, expired or canceled.

    `partial` means some results are ready to be taken with `results` while the handle
    is not finished.
    """

    state: str
    note: str | None = None


@dataclass(frozen=True)
class CallResult:
    """One call's outcome. `error` set means the call produced no usable output.

    `retryable` False marks an error a second attempt would not fix. `minutes` is the
    time the call took when the adapter knows it. `kind` says why the call failed (one
    of `FAILURE_KINDS`). `tier` names the price tier the call was billed at when the
    provider has tiers (DeepSeek's peak and off-peak hours). `rate_limited` is True when
    the provider turned the call away for its rate limit (HTTP 429): it did not run and
    was not billed, and a worker pool pauses and sends it again rather than recording a
    failed attempt.
    """

    call_id: str
    output: str | None
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    retryable: bool = True
    minutes: float | None = None
    stop_reason: str | None = None
    kind: str | None = None
    tier: str | None = None
    rate_limited: bool = False


def split_requests(
    requests: Sequence[Request], max_count: int, max_bytes: int | None = None
) -> list[list[Request]]:
    """Cut requests into groups of at most `max_count` requests and `max_bytes` bytes, in order.

    A single request larger than `max_bytes` still gets a group of its own; the provider
    will refuse it and say why.
    """
    groups: list[list[Request]] = []
    current: list[Request] = []
    size = 0
    for request in requests:
        too_many = len(current) >= max_count
        too_big = max_bytes is not None and current and size + request.size_bytes > max_bytes
        if too_many or too_big:
            groups.append(current)
            current, size = [], 0
        current.append(request)
        size += request.size_bytes
    if current:
        groups.append(current)
    return groups


class Writer(ABC):
    """Sends calls to one route. Subclasses implement the four steps."""

    # True for writers that send the writing rules to a real model; the controller then
    # refuses to start without rules.
    needs_rules: ClassVar[bool] = False
    # True for a writer that keeps the rules in a provider-side cache which is billed for
    # storage by the hour; the controller then charges that storage to the ledger and
    # refuses to start unless the price table has a storage rate for the route's model.
    bills_cache_storage: bool = False
    # Set by the controller. A writer whose calls finish on threads of its own calls it each time
    # one has, so the controller looks at once and gives the free worker its next call.
    wake: Callable[[], None] | None = None

    def __init__(self, route_name: str, route: RouteConfig) -> None:
        self.route_name = route_name
        self.route = route

    @abstractmethod
    def prepare(self, calls: Sequence[CallSpec]) -> list[Request]:
        """Build one request per call, in the same order."""

    @abstractmethod
    def submit(self, requests: Sequence[Request]) -> Handle:
        """Send the requests and return a handle for polling."""

    @abstractmethod
    def poll(self, handle: Handle) -> PollStatus:
        """Report whether the work behind the handle has finished."""

    @abstractmethod
    def results(self, handle: Handle) -> list[CallResult]:
        """Return the results that are ready. May be called again for the same handle."""

    def recover(
        self, requests: Sequence[Request], *, since: datetime | None = None, recorded: Collection[str] = ()
    ) -> Handle | None:
        """Find work submitted for these requests by a run that crashed before recording it.

        Returns a handle covering the requests that were found, or None. `since` is the
        earliest moment any of the requests was prepared, so nothing was sent for them
        before it; a writer that looks through the provider's list of batches can stop there.
        `recorded` holds the handle ids the controller already has: a batch with one of
        those ids belongs to other calls and is never taken. The controller asks again with
        the calls that are still unaccounted for until a writer finds nothing more.
        """
        return None

    def accepting(self) -> Acceptance:
        """Whether this route can take calls now. The default is always."""
        return Acceptance(True)

    def hold_tier(self) -> str | None:
        """The price tier every call of this route will be billed at, when the writer can promise it.

        The controller holds a call's estimated cost against the caps from the moment the call is
        prepared. A model with price tiers is held at its dearest tier unless the writer names one
        here; None (the default) means no promise.
        """
        return None

    def check_rules(self, rules: WriterRules) -> None:
        """Raise ConfigError when these writing rules cannot be sent through this route.

        Called once before a run starts, so a schema the provider would reject fails there
        and not on every request.
        """

    def split(self, requests: Sequence[Request]) -> list[list[Request]]:
        """Cut requests into the groups that become one handle each."""
        return split_requests(requests, self.route.max_batch_calls)

    def release(self, handle: Handle) -> None:
        """Clean up after a handle's results are recorded durably (delete uploaded files, say)."""

    def close(self) -> None:
        """Stop any background work. Called when a run ends."""

    def restore(self, notes: Sequence[Mapping[str, Any]]) -> None:
        """Hand back what this writer asked to have recorded earlier (see `drain_notes`)."""

    def drain_notes(self) -> list[tuple[str, dict[str, Any]]]:
        """Events the writer wants recorded, as (event kind, payload) pairs, then forgotten."""
        return []


# --------------------------------------------------------------------------- fake


@dataclass
class FakeOutcome:
    """What a FakeWriter returns for one call (None fields take the defaults)."""

    output: str | None = None
    error: str | None = None
    retryable: bool = True
    usage: Usage | None = None
    minutes: float | None = None
    kind: str | None = None
    tier: str | None = None


class FakeProvider:
    """A pretend remote service shared by fake writers.

    Keeping it outside the writer lets a test build a second controller after a
    simulated crash and have it see the batches the first one submitted.
    """

    def __init__(self) -> None:
        self.batches: dict[str, dict[str, Any]] = {}
        self.submit_count = 0
        self.submitted_calls: list[str] = []
        self.poll_counts: dict[str, int] = {}
        self.crash_after_submit = False
        self._counter = 0

    def next_handle_id(self) -> str:
        self._counter += 1
        return f"fake-batch-{self._counter:04d}"


def default_outcome(payload: Mapping[str, Any]) -> FakeOutcome:
    """One row per call, built from the spec line."""
    row = {
        "id": f"{payload['line']}-{payload['attempt']}",
        "spec": payload["spec"],
        "thoughts": [f"a thought about {payload['spec']}"],
    }
    return FakeOutcome(output=json.dumps(row))


class FakeWriter(Writer):
    """A writer that needs no network, for tests and dry runs.

    `behavior` turns a request payload into a `FakeOutcome` (by default one row per
    call). `polls_until_ended` is how many polls report "in_progress" first.
    """

    def __init__(
        self,
        route_name: str,
        route: RouteConfig,
        *,
        provider: FakeProvider | None = None,
        behavior: Callable[[Mapping[str, Any]], FakeOutcome] | None = None,
        polls_until_ended: int = 0,
        est_input_tokens: int = 1000,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(route_name, route)
        self.provider = provider or FakeProvider()
        self.behavior = behavior or default_outcome
        self.polls_until_ended = polls_until_ended
        self.est_input_tokens = est_input_tokens
        self.usage = usage or Usage(input_tokens=1000, output_tokens=200)

    def prepare(self, calls: Sequence[CallSpec]) -> list[Request]:
        requests = []
        for call in calls:
            payload = {
                "call_id": call.call_id,
                "task_id": call.task_id,
                "line": call.line,
                "attempt": call.attempt,
                "spec": call.spec,
                "feedback": list(call.feedback),
                "repair_of": call.repair_of,
                "has_image": call.image is not None,
                "model": call.model,
                "max_output_tokens": call.max_output_tokens,
                "task_text": call.task_text,
            }
            requests.append(
                Request(
                    call_id=call.call_id,
                    custom_id=call.custom_id,
                    payload=payload,
                    est_input_tokens=self.est_input_tokens,
                    max_output_tokens=call.max_output_tokens,
                    attempt=call.attempt,
                )
            )
        return requests

    def submit(self, requests: Sequence[Request]) -> Handle:
        provider = self.provider
        provider.submit_count += 1
        handle_id = provider.next_handle_id()
        provider.batches[handle_id] = {"requests": [(r.call_id, r.custom_id, dict(r.payload)) for r in requests]}
        provider.submitted_calls.extend(r.call_id for r in requests)
        if provider.crash_after_submit:
            raise SimulatedCrash("the process died right after the provider accepted the batch")
        mode = "batch" if self.route.kind == "batch" else "per_call"
        return Handle(handle_id=handle_id, call_ids=tuple(r.call_id for r in requests), mode=mode)

    def poll(self, handle: Handle) -> PollStatus:
        provider = self.provider
        if handle.handle_id not in provider.batches:
            return PollStatus("failed", "unknown batch")
        count = provider.poll_counts.get(handle.handle_id, 0) + 1
        provider.poll_counts[handle.handle_id] = count
        if handle.mode == "batch" and count <= self.polls_until_ended:
            return PollStatus("in_progress")
        return PollStatus("ended")

    def results(self, handle: Handle) -> list[CallResult]:
        batch = self.provider.batches.get(handle.handle_id)
        if batch is None:
            return []
        results = []
        wanted = set(handle.call_ids)
        for call_id, _custom_id, payload in batch["requests"]:
            if call_id not in wanted:
                continue
            outcome = self.behavior(payload)
            results.append(
                CallResult(
                    call_id=call_id,
                    output=outcome.output,
                    usage=outcome.usage or self.usage,
                    error=outcome.error,
                    retryable=outcome.retryable,
                    minutes=outcome.minutes,
                    kind=outcome.kind,
                    tier=outcome.tier,
                )
            )
        return results

    def recover(
        self, requests: Sequence[Request], *, since: datetime | None = None, recorded: Collection[str] = ()
    ) -> Handle | None:
        wanted = {request.call_id for request in requests}
        for handle_id, batch in self.provider.batches.items():
            if handle_id in recorded:
                continue
            found = [call_id for call_id, _custom, _payload in batch["requests"] if call_id in wanted]
            if found:
                mode = "batch" if self.route.kind == "batch" else "per_call"
                return Handle(handle_id=handle_id, call_ids=tuple(found), mode=mode)
        return None
