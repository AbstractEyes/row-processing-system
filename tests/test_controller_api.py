"""A one-call (api) route in the controller: one call for each free worker, a stop that loses only those calls,
a call a cap cannot hold yet that waits for a worker to finish, and a long run of rate limits that closes the
route for now instead of stopping it or using up attempts."""

from __future__ import annotations

import json
import threading
import time

import pytest

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


def pool_world(
    tmp_path, *, workers, send=ok_result, gate=lambda: None, clock=time.monotonic, production_cap=50, pricing_cap=50, pool_class=None, **overrides
):
    pool_class = pool_class or Pool
    config = make_config(
        routes={
            ROUTE: {
                "provider": "fake", "model": "fake-model", "kind": "api", "budget_usd": 100,
                "purpose_caps": {**{key: 50 for key in ROUTE_CAPS}, "production": production_cap, "pricing": pricing_cap},
                "workers": workers,
            }
        },
        **overrides,
    )

    def writers(name, route, *, provider, behavior, polls_until_ended):
        return pool_class(name, route, send, gate, clock)

    return World(tmp_path, config=config, writer_class=writers)


def one_hold(controller):
    """What one call of the Pool route holds: its estimate, from the price table."""
    return controller.ledger.estimate(ROUTE, 1000, controller.config.routes[ROUTE].max_output_tokens)


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


# ---------------------------------------------------------------------------- one call for each worker


def test_a_one_call_route_holds_one_call_for_each_of_its_workers_however_long_the_list_is(tmp_path):
    gate_open = threading.Event()
    world = pool_world(tmp_path, workers=2, gate=lambda: None if gate_open.is_set() else 0.002)
    holder, most, held = [], [], []

    def watch(event):
        if holder:
            most.append(holder[0].state.unfinished_calls(ROUTE))
            held.append(holder[0].state.spend(ROUTE).reserved)

    controller = world.controller(on_event=watch)
    holder.append(controller)
    controller.add_task(task_spec(lines(40), route=ROUTE))
    controller.step()
    assert len(world.events_of("call_prepared")) == 2  # two workers, one call each
    for _ in range(4):
        controller.step()
    assert len(world.events_of("call_prepared")) == 2  # nothing has finished, so nothing more is taken
    assert controller.status()["progress"]["lines_total"] == 40 and not controller.is_idle()
    gate_open.set()
    run_until(world, controller, lambda: settled(controller))
    assert len(world.shipped_rows()) == 40 and max(most) <= 2
    assert max(held) == pytest.approx(2 * one_hold(controller))  # never more than two calls' worth of money
    sizes = [len(event.payload["call_ids"]) for event in world.events_of("batch_submitted")]
    assert sum(sizes) == 40 and sizes[0] == 2 and max(sizes) <= 2
    assert controller.state.spend(ROUTE).reserved == pytest.approx(0)


def test_a_stop_loses_only_the_calls_the_workers_had_which_come_back_as_rebounds(tmp_path):
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
    assert len(failed) == 2 and {event.payload["kind"] for event in failed} == {"provider_error"}  # the two, not the forty
    assert [event.payload["attempt"] for event in world.events_of("call_prepared")].count(2) == 2
    assert len(world.shipped_rows()) == 40


def test_calls_past_the_workers_are_still_due_so_the_run_does_not_end_early(tmp_path):
    world = pool_world(tmp_path, workers=1, gate=lambda: 0.002)  # one worker, and it never sends
    controller = world.controller()
    controller.add_task(task_spec(lines(10), route=ROUTE))
    controller.step()
    assert len(controller.state.calls) == 1 and controller.state.job_progress(2)["lines_new"] == 9
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
    assert prepared == 2 and len(world.events_of("call_prepared")) == 2  # the workers are not given more calls while the route is paused
    for writer in controller.writers.values():
        writer.close()


# ---------------------------------------------------------------------------- calls a cap cannot hold yet


class CountingPool(Pool):
    """A pool that counts the requests it was asked to build."""

    def __init__(self, *args):
        super().__init__(*args)
        self.built = 0

    def prepare(self, calls):
        self.built += len(calls)
        return super().prepare(calls)


def close_all(controller):
    for writer in controller.writers.values():
        writer.close()


def test_a_call_a_cap_cannot_hold_yet_waits_for_a_worker_to_finish_and_is_never_refused(tmp_path):
    gate_open = threading.Event()
    one = one_hold(pool_world(tmp_path / "probe", workers=1).controller())
    world = pool_world(
        tmp_path / "run", workers=4, gate=lambda: None if gate_open.is_set() else 0.002, production_cap=2.5 * one
    )
    holder, most, held = [], [], []

    def watch(event):
        if holder:
            most.append(holder[0].state.unfinished_calls(ROUTE))
            held.append(holder[0].state.spend(ROUTE).reserved)

    controller = world.controller(on_event=watch)
    holder.append(controller)
    try:
        controller.add_task(task_spec(lines(12), route=ROUTE))
        controller.step()
        # Four workers, but only two holds fit the cap: the third call waits, and so do the rest.
        assert len(world.events_of("call_prepared")) == 2 and world.events_of("budget_refused") == []
        (waiting,) = controller._waiting_for_room()
        assert "production cap of fake-sync" in waiting[0] and waiting[1] == 10
        assert "10 calls waiting for room under the production cap of fake-sync" in controller._progress_line(None, None)
        assert not controller.is_idle() and controller.state.job_progress(2)["lines_new"] == 10
        gate_open.set()
        run_until(world, controller, lambda: settled(controller))
    finally:
        close_all(controller)
    # The calls cost a tiny part of their holds, so they go as the workers finish: all twelve, none refused.
    assert len(world.shipped_rows()) == 12 and world.events_of("budget_refused") == []
    assert max(most) <= 2 and max(held) <= 2 * one + 1e-9
    assert controller._waiting_for_room() == [] and controller.state.spend(ROUTE).reserved == pytest.approx(0)


def test_a_call_whose_own_hold_can_never_fit_is_refused_at_once_and_never_sent(tmp_path):
    one = one_hold(pool_world(tmp_path / "probe", workers=1).controller())
    world = pool_world(tmp_path / "run", workers=4, production_cap=0.5 * one)  # not even one call fits
    controller = world.controller()
    controller.add_task(task_spec(lines(6), route=ROUTE))
    summary = controller.run(time_budget_seconds=20)
    assert summary.reason == "idle" and summary.parked_by_budget == 6
    refused = world.events_of("budget_refused")
    assert len(refused) == 6 and world.events_of("call_prepared") == [] and world.events_of("batch_submitted") == []
    assert {e.payload["reason"] for e in refused} == {"purpose_cap"}
    assert all(e.payload["purpose_held"] == 0 and e.payload["purpose_spent"] == 0 for e in refused)
    assert controller._waiting_for_room() == []


def test_a_cap_that_holds_one_purpose_back_does_not_hold_back_the_others(tmp_path):
    one = one_hold(pool_world(tmp_path / "probe", workers=1).controller())
    world = pool_world(tmp_path / "run", workers=4, gate=lambda: 0.002, pricing_cap=1.5 * one)
    controller = world.controller()
    try:
        controller.add_task(task_spec(("p1", "p2", "p3"), batch="pricing-run", route=ROUTE, purpose="pricing"))
        controller.add_task(task_spec(("001", "002", "003"), route=ROUTE))
        controller.step()
        controller.step()
        prepared = [event.payload["line"] for event in world.events_of("call_prepared")]
        assert prepared == ["p1", "001", "002", "003"]  # one pricing call fits its cap; production carries on past the others
        assert world.events_of("budget_refused") == []
        assert [count for _cap, count in controller._waiting_for_room()] == [2]
    finally:
        close_all(controller)


def test_a_line_that_waits_is_not_built_again_until_the_money_it_needs_is_free(tmp_path):
    one = one_hold(pool_world(tmp_path / "probe", workers=1).controller())
    world = pool_world(
        tmp_path / "run", workers=4, gate=lambda: 0.002, production_cap=1.5 * one, pool_class=CountingPool
    )
    controller = world.controller()
    try:
        controller.add_task(task_spec(lines(8), route=ROUTE))
        controller.step()
        built = controller.writers[ROUTE].built
        assert len(world.events_of("call_prepared")) == 1 and built == 4  # four free workers built four calls, one fit
        for _ in range(5):
            controller.step()
        assert controller.writers[ROUTE].built == built  # nothing finished, so nothing is built again
        assert len(world.events_of("call_prepared")) == 1
    finally:
        close_all(controller)


def test_the_progress_line_keeps_money_spent_apart_from_money_held(tmp_path):
    gate_open = threading.Event()
    world = pool_world(tmp_path, workers=2, gate=lambda: None if gate_open.is_set() else 0.002)
    controller = world.controller()
    try:
        controller.add_task(task_spec(lines(6), route=ROUTE))
        controller.step()
        one = one_hold(controller)
        line = controller._progress_line(None, None)
        assert "$0.0000 spent" in line and f"${2 * one:.4f} held for calls in flight" in line
        gate_open.set()
        run_until(world, controller, lambda: settled(controller))
        line = controller._progress_line(None, None)
        assert "held" not in line and "$0.0012 spent" in line  # six calls of 100 tokens in and 50 out, at $1 and $2 a million
    finally:
        close_all(controller)


def test_a_finished_call_wakes_the_controller_so_the_free_worker_is_not_left_waiting_for_the_next_poll(tmp_path):
    def slow(request):
        time.sleep(0.05)
        return ok_result(request)

    world = pool_world(tmp_path, workers=1, send=slow, poll_seconds=30)
    controller = world.controller(sleep=None)  # the real pause between passes, which a finished call cuts short
    controller.add_task(task_spec(lines(6), route=ROUTE))
    started = time.monotonic()
    summary = controller.run(time_budget_seconds=25)
    # Six calls one after the other: waiting for a poll after each would take minutes.
    assert summary.reason == "idle" and time.monotonic() - started < 15
    assert len(controller.state.rows) == 6


def test_a_one_call_route_does_not_force_the_store_to_commit_for_each_handle_it_submits(tmp_path):
    gate_open = threading.Event()
    world = pool_world(tmp_path, workers=2, gate=lambda: None if gate_open.is_set() else 0.002)
    forced = []
    original = world.state_store.flush

    def counting(force=False):
        forced.append(force)
        return original(force=force)

    world.state_store.flush = counting
    controller = world.controller()
    try:
        controller.add_task(task_spec(lines(6), route=ROUTE))
        controller.step()
        assert len(world.events_of("batch_submitted")) == 1
        assert True not in forced  # the handle of a one-call route is not worth a commit of its own
    finally:
        close_all(controller)


def test_the_results_of_a_one_call_route_are_made_durable_no_more_often_than_the_store_commits(tmp_path):
    world = pool_world(tmp_path, workers=1)
    clock = Clock()
    controller = world.controller(clock=clock)
    controller.start()
    forced = []
    original = world.state_store.flush

    def counting(force=False):
        forced.append(force)
        return original(force=force)

    world.state_store.flush = counting
    assert controller.config.commit.seconds_per_commit == 5.0
    assert controller._flush_results() is True and controller._flush_results() is False  # the second is too soon
    clock.now += 4.9
    assert controller._flush_results() is False
    clock.now += 0.2
    assert controller._flush_results() is True
    assert forced == [True, True]
    close_all(controller)
