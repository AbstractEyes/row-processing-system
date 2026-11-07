"""The writer interface and a fake writer for tests.

A writer sends "write a row" calls to one model route. The controller drives it
through four steps:

    prepare(calls)    -> requests          build the provider requests (no network)
    submit(requests)  -> handle            send them; a batch id or per-call ids
    poll(handle)      -> PollStatus        is the batch finished?
    results(handle)   -> [CallResult]      raw outputs, usage and errors per call

`handle` is plain data that can be stored in an event and handed back after a
restart, so a submitted batch is polled rather than submitted again. `recover`
lets an adapter look for a batch it submitted just before a crash, using the
deterministic custom ids; the default finds nothing.

Writers read API keys from environment variables only and never log them.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..config import RouteConfig
from ..ids import custom_id as make_custom_id
from ..ledger import Usage


class ProviderError(Exception):
    """A provider call failed in a way that may succeed later."""


class SimulatedCrash(BaseException):
    """Raised by tests to stand in for the process dying at an exact point.

    It derives from BaseException so no ordinary error handler swallows it.
    """


@dataclass(frozen=True)
class CallSpec:
    """Everything a writer needs to build one request."""

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

    @property
    def custom_id(self) -> str:
        return make_custom_id(self.call_id)


@dataclass(frozen=True)
class Request:
    """A provider request, ready to submit. `payload` is JSON-serializable and holds no key."""

    call_id: str
    custom_id: str
    payload: dict[str, Any]
    est_input_tokens: int
    max_output_tokens: int


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
    """`state` is in_progress, ended, failed, expired or canceled."""

    state: str
    note: str | None = None


@dataclass(frozen=True)
class CallResult:
    """One call's outcome. `error` set means the call produced no usable output.

    `retryable` False marks an error a second attempt would not fix. `minutes` is the
    time the call took when the adapter knows it.
    """

    call_id: str
    output: str | None
    usage: Usage = field(default_factory=Usage)
    error: str | None = None
    retryable: bool = True
    minutes: float | None = None
    stop_reason: str | None = None


class Writer(ABC):
    """Sends calls to one route. Subclasses implement the four steps."""

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
        """Return the results for a finished handle. May be called again for the same handle."""

    def recover(self, requests: Sequence[Request]) -> Handle | None:
        """Find work submitted for these requests by a run that crashed before recording it.

        Returns a handle covering the requests that were found, or None.
        """
        return None


# --------------------------------------------------------------------------- fake


@dataclass
class FakeOutcome:
    """What a FakeWriter returns for one call (None fields take the defaults)."""

    output: str | None = None
    error: str | None = None
    retryable: bool = True
    usage: Usage | None = None
    minutes: float | None = None


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
                "has_image": call.image is not None,
                "model": call.model,
            }
            requests.append(
                Request(
                    call_id=call.call_id,
                    custom_id=call.custom_id,
                    payload=payload,
                    est_input_tokens=self.est_input_tokens,
                    max_output_tokens=call.max_output_tokens,
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
                )
            )
        return results

    def recover(self, requests: Sequence[Request]) -> Handle | None:
        wanted = {request.call_id for request in requests}
        for handle_id, batch in self.provider.batches.items():
            found = [call_id for call_id, _custom, _payload in batch["requests"] if call_id in wanted]
            if found:
                mode = "batch" if self.route.kind == "batch" else "per_call"
                return Handle(handle_id=handle_id, call_ids=tuple(found), mode=mode)
        return None
