"""A worker pool and the provider's rate limit: a call turned away is sent again, a run of them pauses the route."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from rps import ids
from rps.config import RouteConfig
from rps.ledger import Usage
from rps.providers import (
    Acceptance,
    AnthropicSyncWriter,
    CallResult,
    DeepSeekWriter,
    GeminiSyncWriter,
    OpenAISyncWriter,
    Request,
)
from rps.providers.common import failure_from_exception, is_rate_limit
from rps.providers.openai_api import parse_response
from rps.providers.pool import (
    RATE_LIMIT_CALL_TRIES,
    RATE_LIMIT_PARK_AFTER,
    RATE_LIMIT_PAUSE_MAX_SECONDS,
    RATE_LIMIT_PAUSE_SECONDS,
    RATE_LIMIT_REASON,
    PoolWriter,
)

from adapter_helpers import ANSWER, ApiError, Namespace, call, route

TASK = ids.task_id("photos", "1", {"001": "a"})
PAUSED = Acceptance(False, RATE_LIMIT_REASON, later=True)


def request(line: str) -> Request:
    call_id = ids.call_id(TASK, line, 1)
    return Request(call_id=call_id, custom_id=ids.custom_id(call_id), payload={"line": line}, est_input_tokens=10, max_output_tokens=10)


def turned_away(request: Request) -> CallResult:
    return CallResult(
        call_id=request.call_id, output=None, error="RateLimitError (HTTP 429): slow down", retryable=True,
        kind="provider_error", rate_limited=True,
    )


def ok(request: Request) -> CallResult:
    return CallResult(call_id=request.call_id, output=f"out-{request.payload['line']}", usage=Usage(input_tokens=1))


class Clock:
    """Monotonic seconds that move only when the test moves them."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Ticking:
    """Monotonic seconds that move one second each time they are read, so a pause passes by itself."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            self.now += 1.0
            return self.now


class Limited(PoolWriter):
    gate_poll_seconds = 0.002  # workers look at the pause often, so the tests are quick
    resume_jitter_seconds = 0.0

    def __init__(self, send, *, workers=4, clock=None):
        super().__init__("sync", RouteConfig(provider="fake", model="m", kind="api", workers=workers, budget_usd=1))
        self._send = send
        if clock is not None:
            self.clock = clock

    def prepare(self, calls):  # not used here
        raise NotImplementedError

    def send(self, request):
        return self._send(request)

    def accepting(self):
        return self.unless_paused(Acceptance(True))


def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.002)


# ---------------------------------------------------------------------------- telling a rate limit from other errors


def test_a_429_that_is_not_a_spent_balance_is_a_rate_limit():
    assert is_rate_limit(ApiError(429, "Rate limit reached for gpt-6-luna", code="rate_limit_exceeded"))
    assert is_rate_limit(ApiError(429))
    assert not is_rate_limit(ApiError(500)) and not is_rate_limit(ApiError(503)) and not is_rate_limit(ApiError(400))
    assert not is_rate_limit(ApiError(429, "You exceeded your current quota", code="insufficient_quota"))
    assert not is_rate_limit(ApiError(402))
    assert not is_rate_limit(RuntimeError("boom"))

    class RateLimitError(Exception):  # an SDK's error class, here without a status
        pass

    assert is_rate_limit(RateLimitError("slow down"))


def test_a_rate_limited_failure_is_still_a_retryable_provider_error_and_carries_the_flag():
    limited = failure_from_exception("c1", ApiError(429, "slow down"))
    assert (limited.kind, limited.retryable, limited.rate_limited, limited.output) == ("provider_error", True, True, None)
    busy = failure_from_exception("c1", ApiError(503))
    assert (busy.kind, busy.retryable, busy.rate_limited) == ("provider_error", True, False)
    quota = failure_from_exception("c1", ApiError(429, "no credit", code="insufficient_quota"))
    assert (quota.kind, quota.retryable, quota.rate_limited) == ("auth", False, False)


def test_an_openai_response_that_failed_for_the_rate_limit_is_flagged():
    body = {"status": "failed", "output": [], "error": {"code": "rate_limit_exceeded", "message": "slow down"}}
    result = parse_response("c1", body)
    assert result.rate_limited and result.kind == "provider_error" and result.retryable
    other = parse_response("c1", {"status": "failed", "output": [], "error": {"code": "server_error", "message": "oops"}})
    assert not other.rate_limited


# ---------------------------------------------------------------------------- the pool


def test_a_call_turned_away_by_the_rate_limit_is_sent_again_and_is_not_recorded_as_failed():
    tries = {}

    def send(r):
        tries[r.call_id] = tries.get(r.call_id, 0) + 1
        return turned_away(r) if tries[r.call_id] <= 2 else ok(r)

    writer = Limited(send, workers=1, clock=Clock())
    handle = writer.submit([request("001")])
    wait_for(lambda: writer.poll(handle).state == "ended")
    [result] = writer.results(handle)
    assert result.output == "out-001" and result.error is None and tries[result.call_id] == 3
    assert writer.accepting().ok  # two in a row are not a run
    writer.close()


def test_a_run_of_rate_limited_calls_pauses_the_route_and_it_goes_on_when_the_pause_ends():
    clock = Clock()
    limited = threading.Event()
    limited.set()
    sent = []

    def send(r):
        sent.append(r.call_id)
        return turned_away(r) if limited.is_set() else ok(r)

    writer = Limited(send, workers=2, clock=clock)
    handle = writer.submit([request(f"{n:03d}") for n in range(1, 4)])
    writer.poll(handle)
    wait_for(lambda: not writer.accepting().ok)
    assert writer.accepting() == PAUSED and writer.rate_limit_pause_left() == RATE_LIMIT_PAUSE_SECONDS
    time.sleep(0.05)
    count = len(sent)
    time.sleep(0.1)
    assert len(sent) == count  # while the route is paused, nothing is sent
    assert writer.poll(handle).state == "in_progress" and not writer.results(handle)  # and nothing has failed
    limited.clear()
    clock.now += RATE_LIMIT_PAUSE_SECONDS + 1
    wait_for(lambda: writer.poll(handle).state == "ended")
    assert [r.output for r in writer.results(handle)] == ["out-001", "out-002", "out-003"]
    assert writer.accepting().ok
    writer.close()


def test_each_pause_in_a_row_is_twice_as_long_up_to_a_limit_and_an_answer_starts_over():
    clock = Clock()
    limited = threading.Event()
    limited.set()
    writer = Limited(lambda r: turned_away(r) if limited.is_set() else ok(r), workers=1, clock=clock)
    writer.submit([request(f"{n:03d}") for n in range(1, 40)])
    handle = writer.submit([request(f"{n:03d}") for n in range(40, 60)])  # (the first job is never started: one worker, one job)
    writer.poll(handle)
    pauses = []
    for _ in range(7):
        wait_for(lambda: writer.rate_limit_pause_left() > 0)
        pauses.append(writer.rate_limit_pause_left())
        clock.now += pauses[-1] + 1
    assert pauses == [60, 120, 240, 480, 900, 900, 900] and RATE_LIMIT_PAUSE_MAX_SECONDS == 900
    limited.clear()
    clock.now += 1000
    wait_for(lambda: writer.poll(handle).state == "ended")
    limited.set()
    again = writer.submit([request(f"{n:03d}") for n in range(60, 70)])
    writer.poll(again)
    wait_for(lambda: writer.rate_limit_pause_left() > 0)
    assert writer.rate_limit_pause_left() == RATE_LIMIT_PAUSE_SECONDS  # an answer in between started the count over
    writer.close()


def test_calls_already_on_the_wire_when_the_pause_began_do_not_make_it_longer():
    clock = Clock()
    barrier = threading.Barrier(10)
    sent = []

    def send(r):
        sent.append(r.call_id)
        if len(sent) <= 10:
            barrier.wait(5)  # the first ten are on the wire together and come back turned away together
        return turned_away(r)

    writer = Limited(send, workers=10, clock=clock)
    handle = writer.submit([request(f"{n:03d}") for n in range(1, 11)])
    writer.poll(handle)
    wait_for(lambda: len(sent) >= 10)
    time.sleep(0.3)
    assert writer.rate_limit_pause_left() == RATE_LIMIT_PAUSE_SECONDS  # one pause, not two or three
    writer.close()


def test_a_call_that_is_turned_away_again_and_again_is_handed_up_as_a_failure_after_all():
    tries = []

    def send(r):
        tries.append(r.call_id)
        return turned_away(r)

    writer = Limited(send, workers=1, clock=Ticking())
    handle = writer.submit([request("001")])
    wait_for(lambda: writer.poll(handle).state == "ended")
    [result] = writer.results(handle)
    assert len(tries) == RATE_LIMIT_CALL_TRIES
    assert result.output is None and result.rate_limited and result.kind == "provider_error" and result.retryable
    writer.close()


def test_closing_the_pool_ends_workers_that_are_waiting_out_a_pause():
    clock = Clock()
    sent = []
    writer = Limited(lambda r: sent.append(r.call_id) or turned_away(r), workers=2, clock=clock)
    handle = writer.submit([request(f"{n:03d}") for n in range(1, 8)])
    writer.poll(handle)
    wait_for(lambda: writer.rate_limit_pause_left() > 0)
    time.sleep(0.05)
    count = len(sent)
    writer.close()
    clock.now += 10_000  # the pause is over, but the pool is closed: nothing more is sent
    time.sleep(0.1)
    assert len(sent) == count


def test_a_rate_limit_error_from_the_openai_client_pauses_the_route_and_it_reopens(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "a-key-for-the-tests-only")
    state = {"limited": True}

    def create(**body):
        if state["limited"]:
            raise ApiError(429, "Rate limit reached for gpt-6-luna on tokens per min", code="rate_limit_exceeded")
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": ANSWER}]}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    clock = Clock()
    writer = OpenAISyncWriter("luna-sync", route("openai", "gpt-6-luna", "api", workers=4), client=Namespace(responses=Namespace(create=create)))
    writer.clock, writer.gate_poll_seconds, writer.resume_jitter_seconds = clock, 0.002, 0.0
    requests = writer.prepare([call(f"{n:03d}", provider="openai", model="gpt-6-luna", kind="api") for n in range(1, 9)])
    handle = writer.submit(requests)
    writer.poll(handle)
    wait_for(lambda: not writer.accepting().ok)
    assert writer.accepting() == PAUSED
    state["limited"] = False
    clock.now += RATE_LIMIT_PAUSE_SECONDS + 1
    wait_for(lambda: writer.poll(handle).state == "ended")
    results = writer.results(handle)
    assert len(results) == 8 and all(r.error is None and r.output == ANSWER for r in results)
    assert writer.accepting().ok
    writer.close()


WRITERS = [
    ("anthropic", lambda: AnthropicSyncWriter("haiku-sync", route("anthropic", "claude-haiku-5-5", "api")), "ANTHROPIC_API_KEY"),
    ("openai", lambda: OpenAISyncWriter("luna-sync", route("openai", "gpt-6-luna", "api")), "OPENAI_API_KEY"),
    ("gemini", lambda: GeminiSyncWriter("flash-sync", route("gemini", "gemini-3.8-flash", "api")), "GEMINI_API_KEY"),
    (
        "deepseek",
        lambda: DeepSeekWriter(
            "deepseek-sync", route("deepseek", "deepseek-flash", "api", enabled=True), now=lambda: datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        ),
        "DEEPSEEK_API_KEY",
    ),
]


@pytest.mark.parametrize("name, make, key", WRITERS, ids=[w[0] for w in WRITERS])
def test_every_one_call_writer_says_closed_for_now_while_paused_and_a_missing_key_still_closes_it(monkeypatch, name, make, key):
    monkeypatch.setenv(key, "a-key-for-the-tests-only")
    writer = make()
    clock = Clock()
    writer.clock = clock
    assert writer.accepting().ok
    for _ in range(RATE_LIMIT_PARK_AFTER):
        writer._note_rate_limit()
    assert writer.accepting() == PAUSED
    clock.now += RATE_LIMIT_PAUSE_SECONDS + 1
    assert writer.accepting().ok
    monkeypatch.delenv(key)
    closed = writer.accepting()
    assert not closed.ok and not closed.later and key in closed.reason  # a missing key is not a wait
    for _ in range(RATE_LIMIT_PARK_AFTER):
        writer._note_rate_limit()
    assert writer.accepting() == closed  # and a pause does not hide it
