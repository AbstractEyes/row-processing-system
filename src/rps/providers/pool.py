"""A writer that sends one call at a time from a pool of worker threads.

Synchronous routes have no provider batch to wait for. This base class gives them the
same four steps as a batch writer:

    submit(requests)   registers the calls under a handle and sends nothing yet
    poll(handle)       starts the sending (once the controller has recorded the handle
                       durably) and reports in_progress, partial or ended
    results(handle)    every call that has finished, as a result; may be called again
    release(handle)    forgets the finished calls once they are recorded

Each finished call is handed over as soon as the controller polls (state `partial`), so a
stop loses only the calls still in flight. The handle lives in this object's memory: after
a restart `poll` answers `failed` for it and the controller sends the calls again as
rebounds. Subclasses supply `send` (one call, returning a `CallResult`), and may supply
`gate` (seconds to wait before sending, for routes with sending hours).
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Sequence

from ..config import RouteConfig
from .base import CallResult, Handle, PollStatus, Request, Writer
from .common import failure_from_exception

# The longest a worker sleeps at the gate before looking again at the stop flag.
GATE_POLL_SECONDS = 30.0


@dataclass
class _Job:
    requests: dict[str, Request]
    results: dict[str, CallResult] = field(default_factory=dict)
    delivered: set[str] = field(default_factory=set)
    started: bool = False


class PoolWriter(Writer):
    """Base for synchronous routes. See the module description."""

    needs_rules = True
    # How long a worker waiting at the gate sleeps before it looks at the gate and the stop flag again.
    gate_poll_seconds = GATE_POLL_SECONDS

    def __init__(self, route_name: str, route: RouteConfig) -> None:
        super().__init__(route_name, route)
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._stop = threading.Event()

    # -- for subclasses

    def send(self, request: Request) -> CallResult:
        """Send one call and return its result. May be called from many threads at once."""
        raise NotImplementedError

    def gate(self) -> float | None:
        """Seconds to wait before sending a call, or None to send now (the default)."""
        return None

    # -- the writer steps

    def submit(self, requests: Sequence[Request]) -> Handle:
        handle_id = f"{self.route_name}-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._jobs[handle_id] = _Job(requests={request.call_id: request for request in requests})
        return Handle(handle_id=handle_id, call_ids=tuple(request.call_id for request in requests), mode="per_call")

    def poll(self, handle: Handle) -> PollStatus:
        with self._lock:
            job = self._jobs.get(handle.handle_id)
        if job is None:
            return PollStatus("failed", "the run that sent these calls stopped before they finished; their results are lost")
        self._start(job)
        with self._lock:
            finished, total = len(job.results), len(job.requests)
            fresh = len(job.results.keys() - job.delivered)
        if finished >= total:
            return PollStatus("ended")
        return PollStatus("partial" if fresh else "in_progress")

    def results(self, handle: Handle) -> list[CallResult]:
        with self._lock:
            job = self._jobs.get(handle.handle_id)
            if job is None:
                return []
            job.delivered.update(job.results)
            return [job.results[call_id] for call_id in job.requests if call_id in job.results]

    def release(self, handle: Handle) -> None:
        with self._lock:
            self._jobs.pop(handle.handle_id, None)

    def close(self) -> None:
        """Stop sending. Calls already on the wire finish in the background and are dropped."""
        self._stop.set()
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    # -- the workers

    def _start(self, job: _Job) -> None:
        with self._lock:
            if job.started or self._stop.is_set():
                return
            job.started = True
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.route.workers, thread_name_prefix=f"rps-{self.route_name}"
                )
            executor = self._executor
        for request in job.requests.values():
            executor.submit(self._work, job, request)

    def _work(self, job: _Job, request: Request) -> None:
        began = time.monotonic()
        try:
            wait = self.gate()
            while wait is not None and not self._stop.is_set():
                self._stop.wait(min(wait, self.gate_poll_seconds))
                wait = self.gate()
            if self._stop.is_set():
                return  # the run is ending; the call was never sent
            result = self.send(request)
        except Exception as error:  # a worker never dies; the failure becomes the call's result
            result = failure_from_exception(request.call_id, error)
        if result.minutes is None:
            result = replace(result, minutes=(time.monotonic() - began) / 60.0)
        if self._stop.is_set():
            return
        with self._lock:
            job.results[request.call_id] = result
