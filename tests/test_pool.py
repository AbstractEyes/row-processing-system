"""The worker pool behind the synchronous routes."""

from __future__ import annotations

import threading
import time

from rps import ids
from rps.config import RouteConfig
from rps.ledger import Usage
from rps.providers import CallResult, Request
from rps.providers.pool import PoolWriter

TASK = ids.task_id("photos", "1", {"001": "a"})


def request(line: str) -> Request:
    call_id = ids.call_id(TASK, line, 1)
    return Request(call_id=call_id, custom_id=ids.custom_id(call_id), payload={"line": line}, est_input_tokens=10, max_output_tokens=10)


class Recording(PoolWriter):
    """Sends nothing anywhere: `send` is whatever the test gives it."""

    def __init__(self, send, *, gate=None, workers=4):
        super().__init__("sync", RouteConfig(provider="fake", model="m", kind="api", workers=workers, budget_usd=1))
        self._send = send
        self._gate = gate or (lambda: None)

    def prepare(self, calls):  # not used here
        raise NotImplementedError

    def send(self, request):
        return self._send(request)

    def gate(self):
        return self._gate()


def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def ok(request):
    return CallResult(call_id=request.call_id, output=f"out-{request.payload['line']}", usage=Usage(input_tokens=1))


def test_nothing_is_sent_until_the_first_poll_and_every_call_is_sent_once():
    sent = []
    writer = Recording(lambda r: sent.append(r.call_id) or ok(r))
    handle = writer.submit([request("001"), request("002"), request("003")])
    assert handle.mode == "per_call" and len(handle.call_ids) == 3 and sent == []
    wait_for(lambda: writer.poll(handle).state == "ended")
    assert sorted(sent) == sorted(handle.call_ids)
    results = writer.results(handle)
    assert [r.call_id for r in results] == list(handle.call_ids)  # in request order
    assert all(r.minutes is not None and r.minutes >= 0 for r in results)
    writer.poll(handle)
    assert sorted(sent) == sorted(handle.call_ids)  # polling again does not send again
    writer.close()


def test_the_writer_calls_wake_each_time_a_call_finishes_and_a_wake_that_fails_changes_nothing():
    woken = []
    writer = Recording(ok)
    writer.wake = lambda: woken.append(len(woken) + 1)
    handle = writer.submit([request("001"), request("002"), request("003")])
    wait_for(lambda: writer.poll(handle).state == "ended")
    wait_for(lambda: len(woken) == 3)  # one for each call, after its result could be read
    assert woken == [1, 2, 3]
    broken = Recording(ok)

    def fails():
        raise RuntimeError("the controller is gone")

    broken.wake = fails
    handle = broken.submit([request("001")])
    wait_for(lambda: broken.poll(handle).state == "ended")
    assert [r.output for r in broken.results(handle)] == ["out-001"]  # the worker did not die
    for pool in (writer, broken):
        pool.close()


def test_finished_calls_are_offered_as_partial_before_the_handle_ends():
    release = threading.Event()

    def send(r):
        if r.payload["line"] == "002":
            release.wait(10)
        return ok(r)

    writer = Recording(send)
    handle = writer.submit([request("001"), request("002")])
    wait_for(lambda: writer.poll(handle).state == "partial")
    [first] = writer.results(handle)
    assert first.output == "out-001"
    assert writer.poll(handle).state == "in_progress"  # nothing new since the results were taken
    release.set()
    wait_for(lambda: writer.poll(handle).state == "ended")
    assert [r.call_id for r in writer.results(handle)] == list(handle.call_ids)  # asking again gives everything
    writer.close()


def test_an_error_in_send_becomes_that_calls_failed_result_and_the_others_go_on():
    class Boom(Exception):
        status_code = 503  # (a rate limit, 429, is not a failure: see test_pool_rate_limits)

    def send(r):
        if r.payload["line"] == "002":
            raise Boom("slow down")
        return ok(r)

    writer = Recording(send)
    handle = writer.submit([request("001"), request("002")])
    wait_for(lambda: writer.poll(handle).state == "ended")
    first, second = writer.results(handle)
    assert first.output == "out-001"
    assert second.output is None and second.kind == "provider_error" and second.retryable is True
    writer.close()


def test_the_pool_runs_at_most_the_configured_number_of_calls_at_once():
    active = 0
    peak = 0
    lock = threading.Lock()

    def send(r):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return ok(r)

    writer = Recording(send, workers=2)
    handle = writer.submit([request(f"{n:03d}") for n in range(1, 9)])
    wait_for(lambda: writer.poll(handle).state == "ended")
    assert peak == 2
    writer.close()


def test_calls_wait_at_the_gate_and_go_when_it_opens():
    open_now = threading.Event()
    sent = []
    writer = Recording(lambda r: sent.append(r.call_id) or ok(r), gate=lambda: None if open_now.is_set() else 0.01)
    handle = writer.submit([request("001")])
    writer.poll(handle)
    time.sleep(0.1)
    assert sent == [] and writer.poll(handle).state == "in_progress"
    open_now.set()
    wait_for(lambda: writer.poll(handle).state == "ended")
    assert len(sent) == 1
    writer.close()


def test_closing_stops_calls_that_have_not_been_sent():
    sent = []
    writer = Recording(lambda r: sent.append(r.call_id) or ok(r), gate=lambda: 0.01)
    handle = writer.submit([request("001"), request("002")])
    writer.poll(handle)
    writer.close()
    time.sleep(0.1)
    assert sent == [] and writer.poll(handle).state in ("in_progress", "failed")


def test_a_handle_the_writer_never_saw_is_failed_so_its_calls_are_sent_again():
    from rps.providers import Handle

    writer = Recording(ok)
    status = writer.poll(Handle("elsewhere", ("c1",), mode="per_call"))
    assert status.state == "failed" and "lost" in status.note
    assert writer.results(Handle("elsewhere", ("c1",), mode="per_call")) == []


def test_release_forgets_a_finished_handle():
    writer = Recording(ok)
    handle = writer.submit([request("001")])
    wait_for(lambda: writer.poll(handle).state == "ended")
    writer.release(handle)
    assert writer.poll(handle).state == "failed"
    writer.close()


def test_handle_ids_are_unique_across_writers():
    one, two = Recording(ok), Recording(ok)
    assert one.submit([request("001")]).handle_id != two.submit([request("001")]).handle_id
