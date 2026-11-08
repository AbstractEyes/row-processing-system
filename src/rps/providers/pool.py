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
`gate` (seconds to wait before sending, for routes with sending hours). When a call
finishes the writer calls `wake`, which the controller sets, so the free worker is given
its next call at once and not at the controller's next poll.

Rate limits. The provider's own client already retries a rate-limited call a few times,
pausing as the provider asks (the route's `max_retries`). A call that is still turned away
after that comes back from `send` with `rate_limited` set: it did not run and was not billed,
so the pool does not record it as a failed attempt. It waits and sends the call again. After
`RATE_LIMIT_PARK_AFTER` of these in a row, with no call getting an answer in between, the
whole route is paused: every worker holds still for `RATE_LIMIT_PAUSE_SECONDS` (twice as long
for each pause in a row, up to `RATE_LIMIT_PAUSE_MAX_SECONDS`), and `accepting` answers
"closed for now" so the controller prepares no more calls until the pause ends. A route paused
this way is never stopped for the run: the pause ends by itself, and one call that gets an
answer clears the count. A call turned away `RATE_LIMIT_CALL_TRIES` times in a row is handed
up as a failed call after all (retried as a rebound), so one call that can never go through
does not hold the route for ever.
"""

from __future__ import annotations

import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Callable, Sequence

from ..config import RouteConfig
from .base import Acceptance, CallResult, Handle, PollStatus, Request, Writer
from .common import failure_from_exception

# The longest a worker sleeps at the gate before looking again at the stop flag.
GATE_POLL_SECONDS = 30.0

# Rate limits (see the module description).
RATE_LIMIT_PARK_AFTER = 5
RATE_LIMIT_PAUSE_SECONDS = 60.0
RATE_LIMIT_PAUSE_MAX_SECONDS = 900.0
RATE_LIMIT_CALL_TRIES = 8
RATE_LIMIT_RESUME_JITTER_SECONDS = 5.0  # after a pause each worker waits a random part of this longer
RATE_LIMIT_REASON = "the provider's rate limit is reached; the route pauses and sends again by itself"


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
    # How long, at most, a worker waits beyond the end of a rate-limit pause (a different time for each).
    resume_jitter_seconds = RATE_LIMIT_RESUME_JITTER_SECONDS

    def __init__(self, route_name: str, route: RouteConfig) -> None:
        super().__init__(route_name, route)
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._executor: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        # Monotonic seconds; replaceable in tests.
        self.clock: Callable[[], float] = time.monotonic
        self._rate_streak = 0  # rate-limited sends in a row
        self._pauses = 0  # pauses in a row (each is twice as long as the one before)
        self._paused_until = 0.0

    # -- for subclasses

    def send(self, request: Request) -> CallResult:
        """Send one call and return its result. May be called from many threads at once."""
        raise NotImplementedError

    def gate(self) -> float | None:
        """Seconds to wait before sending a call, or None to send now (the default)."""
        return None

    def unless_paused(self, base: Acceptance) -> Acceptance:
        """`base`, or "closed for now" while the route is paused for the provider's rate limit.

        Subclasses return this from `accepting`, after their own checks, so the controller sends
        no more calls to a route that is waiting out a rate limit and opens it again by itself.
        """
        if not base.ok or self.rate_limit_pause_left() <= 0:
            return base
        return Acceptance(False, RATE_LIMIT_REASON, later=True)

    def rate_limit_pause_left(self) -> float:
        """Seconds left of the pause after a run of rate-limited answers (0 when the route is not paused)."""
        with self._lock:
            return max(0.0, self._paused_until - self.clock())

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

    def _ready(self) -> bool:
        """Wait until the gate is open and the route is not paused for a rate limit.

        False when the pool is stopping: the call is then never sent. After a rate-limit pause each
        worker waits a little longer, a different moment for each, so the workers do not all send at
        the same instant and meet the limit again together.
        """
        paused = False
        while not self._stop.is_set():
            wait = self.gate()
            pause = self.rate_limit_pause_left()
            if wait is None and pause <= 0:
                if paused and self.resume_jitter_seconds > 0:
                    self._stop.wait(random.uniform(0.0, self.resume_jitter_seconds))
                    return not self._stop.is_set()
                return True
            paused = paused or pause > 0
            self._stop.wait(min(max(wait or 0.0, pause), self.gate_poll_seconds))
        return False

    def _work(self, job: _Job, request: Request) -> None:
        began = time.monotonic()
        tries = 0
        while True:
            if not self._ready():
                return  # the run is ending; the call was never sent
            try:
                result = self.send(request)
            except Exception as error:  # a worker never dies; the failure becomes the call's result
                result = failure_from_exception(request.call_id, error)
            tries += 1
            if result.rate_limited:
                self._note_rate_limit()
                if tries < RATE_LIMIT_CALL_TRIES:
                    continue  # turned away, not run: after the pause the same call is sent again
            elif result.error is None:
                self._note_answer()
            break
        if result.minutes is None:
            result = replace(result, minutes=(time.monotonic() - began) / 60.0)
        if self._stop.is_set():
            return
        with self._lock:
            job.results[request.call_id] = result
        self._notify()

    def _notify(self) -> None:
        """Tell the controller a call has finished, so its worker gets the next one without waiting for a poll."""
        wake = self.wake
        if wake is not None:
            try:
                wake()
            except Exception:  # a worker never dies
                pass

    def _note_rate_limit(self) -> None:
        """A call was turned away for the rate limit; a run of them pauses the whole route.

        Calls that were already on the wire when the pause began come back turned away too; they
        do not count, or one burst of them would double the pause again and again.
        """
        with self._lock:
            now = self.clock()
            if now < self._paused_until:
                return
            self._rate_streak += 1
            if self._rate_streak < RATE_LIMIT_PARK_AFTER:
                return
            self._rate_streak = 0
            self._pauses += 1
            pause = min(RATE_LIMIT_PAUSE_SECONDS * 2 ** (self._pauses - 1), RATE_LIMIT_PAUSE_MAX_SECONDS)
            self._paused_until = now + pause

    def _note_answer(self) -> None:
        """A call got an answer: the rate limit is not in the way, so the count and the pauses start over."""
        with self._lock:
            self._rate_streak = 0
            self._pauses = 0
