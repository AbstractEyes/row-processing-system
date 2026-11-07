"""A one-call (api) route in the controller: a window of calls in flight, a stop that loses only that window,
and a long run of rate limits that closes the route for now instead of stopping it or using up attempts."""

from __future__ import annotations

import json
import threading
import time

from rps.ledger import Usage
from rps.providers import Acceptance, CallResult, Request
from rps.providers.pool import RATE_LIMIT_PAUSE_SECONDS, RATE_LIMIT_REASON, PoolWriter

from harness import World, keep_all, task_spec
from helpers import ROUTE_CAPS, make_config

GOOD = json.dumps({"id": "ok", "thoughts": ["fine"]})
ROUTE = "fake-sync"


class Pool(PoolWriter):
    """A worker-pool writer for the controller tests: requests are built like the fake writer's, `send` is the test's."""

    needs_rules = False
    gate_poll_seconds = 0.002
    resume_jitter_seconds = 0.0

    def __init__(self, name, route, send, gate, clock):
        super().__init__(name, route)
        self._send, self._gate = send, gate
        self.clock = clock

    def prepare(self, calls):
        return [
            Request(
                call_id=c.call_id, custom_id=c.custom_id, payload={"line": c.line, "attempt": c.attempt},
                est_input_tokens=1000, max_output_tokens=c.max_output_tokens, attempt=c.attempt,
            )
            for c in calls
        ]

    def send(self, request):
        return self._send(request)

    def gate(self):
        return self._gate()

    def accepting(self):
        return self.unless_paused(Acceptance(True))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def ok_result(request):
    return CallResult(call_id=request.call_id, output=GOOD, usage=Usage(input_tokens=100, output_tokens=50))


def turned_away(request):
    return CallResult(
        call_id=request.call_id, output=None, error="RateLimitError (HTTP 429): slow down", retryable=True,
        kind="provider_error", rate_limited=True,
    )


def pool_world(tmp_path, *, workers, send=ok_result, gate=lambda: None, clock=time.monotonic):
    config = make_config(
        routes={
            ROUTE: {
                "provider": "fake", "model": "fake-model", "kind": "api", "budget_usd": 100,
                "purpose_caps": {key: 50 for key in ROUTE_CAPS}, "workers": workers,
            }
        }
    )

    def writers(name, route, *, provider, behavior, polls_until_ended):
        return Pool(name, route, send, gate, clock)

    return World(tmp_path, config=config, writer_class=writers)


def lines(count):
    return tuple(f"{number:03d}" for number in range(1, count + 1))


def settled(controller):
    return controller.is_idle() and not controller.state.open_packets()


def run_until(world, controller, predicate, seconds=15.0):
    """Pass after pass of the controller and the reviewers, with real time for the pool's workers, until the predicate holds."""
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "the run did not get there"
        controller.step()
        world.run_reviewers(keep_all)
        time.sleep(0.003)


# ---------------------------------------------------------------------------- the window


def test_a_one_call_route_is_fed_a_window_of_calls_at_a_time_however_long_the_list_is(tmp_path):
    gate_open = threading.Event()
    world = pool_world(tmp_path, workers=2, gate=lambda: None if gate_open.is_set() else 0.002)
    holder, most = [], []
    controller = world.controller(on_event=lambda event: most.append(holder[0].state.unfinished_calls(ROUTE)) if holder else None)
    holder.append(controller)
    controller.add_task(task_spec(lines(40), route=ROUTE))
    controller.step()
    assert len(world.events_of("call_prepared")) == 8  # two workers, four calls each
    for _ in range(4):
        controller.step()
    assert len(world.events_of("call_prepared")) == 8  # nothing has finished, so nothing more is taken
    assert controller.status()["progress"]["lines_total"] == 40 and not controller.is_idle()
    gate_open.set()
    run_until(world, controller, lambda: settled(controller))
    assert len(world.shipped_rows()) == 40 and max(most) <= 8
    sizes = [len(event.payload["call_ids"]) for event in world.events_of("batch_submitted")]
    assert sum(sizes) == 40 and sizes[0] == 8 and all(size >= 2 for size in sizes[:-1])  # a refill takes a quarter of the window or more


def test_a_stop_loses_only_the_calls_in_the_window_which_come_back_as_rebounds(tmp_path):
    gate_open = threading.Event()
    world = pool_world(tmp_path, workers=2, gate=lambda: None if gate_open.is_set() else 0.002)
    first = world.controller()
    first.add_task(task_spec(lines(40), route=ROUTE))
    first.step()
    assert len(world.events_of("batch_submitted")) == 1
    for writer in first.writers.values():
        writer.close()  # the process dies: its pool and what was in it are gone
    second = world.controller()
    gate_open.set()
    run_until(world, second, lambda: settled(second))
    failed = world.events_of("call_failed")
    assert len(failed) == 8 and {event.payload["kind"] for event in failed} == {"provider_error"}  # the window, not the forty
    assert [event.payload["attempt"] for event in world.events_of("call_prepared")].count(2) == 8
    assert len(world.shipped_rows()) == 40


def test_calls_past_the_window_are_still_due_so_the_run_does_not_end_early(tmp_path):
    world = pool_world(tmp_path, workers=1, gate=lambda: 0.002)  # a window of four, and the worker never sends
    controller = world.controller()
    controller.add_task(task_spec(lines(10), route=ROUTE))
    controller.step()
    assert len(controller.state.calls) == 4 and controller.state.job_progress(2)["lines_new"] == 6
    assert not controller.is_idle()
    for writer in controller.writers.values():
        writer.close()


# ---------------------------------------------------------------------------- rate limits


def test_a_long_run_of_rate_limits_closes_the_route_for_now_and_uses_up_no_attempt(tmp_path):
    clock, state = Clock(), {"limited": True}
    world = pool_world(tmp_path, workers=4, send=lambda r: turned_away(r) if state["limited"] else ok_result(r), clock=clock)
    controller = world.controller()
    controller.add_task(task_spec(lines(12), route=ROUTE))
    run_until(world, controller, lambda: controller.route_state(ROUTE)[0] == "later")
    assert controller.route_state(ROUTE) == ("later", RATE_LIMIT_REASON)
    controller.step()  # the next pass says so
    # Not stopped, not failed: the run waits, and no call has lost an attempt.
    assert not world.events_of("route_halted") and not world.events_of("call_failed")
    assert not controller.is_idle()
    assert any(f"route {ROUTE} is closed for now" in line and RATE_LIMIT_REASON in line for line in world.printed)
    assert f"route {ROUTE}: closed for now" in controller.format_status()
    assert controller.status()["routes"][ROUTE]["state"] == "later"
    # The provider lets up and the pause ends: the same calls go through.
    state["limited"] = False
    clock.now += RATE_LIMIT_PAUSE_SECONDS + 1
    run_until(world, controller, lambda: settled(controller))
    assert len(world.shipped_rows()) == 12 and not world.events_of("call_failed") and not world.events_of("route_halted")
    assert {call.attempt for call in controller.state.calls.values()} == {1}
    assert controller.route_state(ROUTE)[0] == "open"


def test_a_route_that_is_paused_for_a_rate_limit_takes_no_more_calls(tmp_path):
    clock = Clock()
    world = pool_world(tmp_path, workers=2, send=turned_away, clock=clock)
    controller = world.controller()
    controller.add_task(task_spec(lines(40), route=ROUTE))
    run_until(world, controller, lambda: controller.route_state(ROUTE)[0] == "later")
    prepared = len(world.events_of("call_prepared"))
    for _ in range(10):
        controller.step()
        time.sleep(0.002)
    assert prepared == 8 and len(world.events_of("call_prepared")) == 8  # the window is not refilled while the route is paused
    for writer in controller.writers.values():
        writer.close()
