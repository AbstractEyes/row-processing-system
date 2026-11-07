"""Replay of the events added with the real adapters: halts, caches, kinds, tiers, classes and ordinals."""

from rps import ids
from rps.events import Event
from rps.state import State

from helpers import Builder


def prepared(b, task_id, line, attempt=1, est=0.01):
    call_id = ids.call_id(task_id, line, attempt)
    b.add(
        "call_prepared",
        {"call_id": call_id, "task_id": task_id, "line": line, "attempt": attempt, "route": "fake-batch", "purpose": "production", "est_usd": est},
    )
    return call_id


def test_a_task_line_remembers_its_picture_class_and_its_place_in_the_task():
    b = Builder()
    task_id = ids.task_id("photos", "9", {"001": "a", "002": "b", "003": "c"})
    b.add(
        "task_created",
        {
            "task_id": task_id, "source": "photos", "batch": "9", "variant": "", "route": "fake-batch", "purpose": "production",
            "effort": "low", "pool_version": "1", "dataset": "photos",
            "lines": [
                {"line_id": "001", "spec": "a", "item_id": "i1", "image": "a.jpg", "license": "L", "image_class": "coco"},
                {"line_id": "002", "spec": "b", "item_id": "i2", "image": None, "license": "L"},
                {"line_id": "003", "spec": "c", "item_id": "i3", "image": "c.jpg", "license": "L", "image_class": "ffhq"},
            ],
        },
    )
    lines = State.from_events(b.events).tasks[task_id].lines
    assert [(line.line_id, line.ordinal, line.image_class) for line in lines.values()] == [("001", 0, "coco"), ("002", 1, None), ("003", 2, "ffhq")]


def test_a_call_keeps_the_kind_of_its_failure_and_the_tier_it_was_billed_at():
    b = Builder()
    task_id = b.task()
    first, second = prepared(b, task_id, "001"), prepared(b, task_id, "002")
    b.add("batch_submitted", {"handle_id": "h1", "handle": {}, "route": "fake-batch", "call_ids": [first, second]})
    b.add("call_failed", {"call_id": first, "handle_id": "h1", "error": "cut off", "retryable": True, "billed": True, "kind": "truncated", "usd": 0.002, "tier": "peak"})
    b.add("call_returned", {"call_id": second, "handle_id": "h1", "usage": {}, "usd": 0.001, "tier": "offpeak", "rows": [], "skips": []})
    state = State.from_events(b.events)
    assert state.calls[first].kind == "truncated" and state.calls[first].tier == "peak"
    assert state.calls[second].tier == "offpeak" and state.calls[second].kind is None


def test_a_line_whose_call_was_cut_off_is_due_a_retry_with_a_larger_limit_and_no_advice():
    b = Builder()
    task_id = b.task(lines={"001": "a cat"})
    call_id = prepared(b, task_id, "001")
    b.add("batch_submitted", {"handle_id": "h1", "handle": {}, "route": "fake-batch", "call_ids": [call_id]})
    b.add("call_failed", {"call_id": call_id, "handle_id": "h1", "error": "cut off at the limit", "retryable": True, "billed": True, "kind": "truncated", "usd": 0.002})
    status = State.from_events(b.events).line_status(task_id, "001", 2)
    assert status.outcome == "retry" and status.boost is True and status.feedback == ()


def test_a_line_whose_call_failed_another_way_is_retried_with_the_error_as_advice():
    b = Builder()
    task_id = b.task(lines={"001": "a cat"})
    call_id = prepared(b, task_id, "001")
    b.add("batch_submitted", {"handle_id": "h1", "handle": {}, "route": "fake-batch", "call_ids": [call_id]})
    b.add("call_failed", {"call_id": call_id, "handle_id": "h1", "error": "unusable output: not json", "retryable": True, "billed": True, "kind": "invalid_output", "usd": 0.002})
    status = State.from_events(b.events).line_status(task_id, "001", 2)
    assert status.outcome == "retry" and status.boost is False and status.feedback == ("unusable output: not json",)


def test_halts_and_caches_are_replayed_once_each_and_survive_a_second_replay():
    b = Builder()
    b.add("route_halted", {"route": "fake-batch", "kind": "auth", "reason": "key rejected", "call_id": "c1"})
    b.add("cache_created", {"route": "fake-batch", "key": "m|v1", "name": "cachedContents/a", "expires": "2026-11-07T18:00:00Z"})
    b.add("cache_created", {"route": "fake-batch", "key": "m|v1", "name": "cachedContents/a", "expires": "2026-11-07T23:00:00Z"})
    state = State.from_events(b.events + b.events)
    assert [h["kind"] for h in state.halts] == ["auth"]
    assert state.caches[("fake-batch", "m|v1")]["expires"] == "2026-11-07T23:00:00Z"
    assert state.snapshot()["caches"] == {"fake-batch|m|v1": state.caches[("fake-batch", "m|v1")]}
    assert state.snapshot()["halts"][0]["reason"] == "key rejected"


def test_a_malformed_halt_or_cache_event_is_counted_and_skipped():
    state = State()
    state.apply(Event(ts="2026-11-07T09:00:00.000000Z", actor="a", kind="route_halted", payload={}))
    state.apply(Event(ts="2026-11-07T09:00:00.000000Z", actor="a", kind="cache_created", payload={"route": "r"}))
    assert len(state.bad_events) == 2 and not state.halts and not state.caches
