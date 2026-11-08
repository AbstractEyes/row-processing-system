import json
from datetime import datetime, timedelta, timezone

import pytest

from rps import layout
from rps.events import EVENT_KINDS, Event, EventError, EventLog, parse_event_text, replay
from rps.layout import controller_actor
from rps.store import ActorStore

RUN = "20261107T090000Z-aaaa"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 7, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


def make_log(store, run=RUN, **kwargs):
    return EventLog(ActorStore(store, controller_actor(run)), controller_actor(run), now=Clock(), **kwargs)


def test_event_line_has_the_documented_fields(local_store):
    log = make_log(local_store)
    event = log.append("task_created", {"task_id": "t1"})
    line = local_store.read_text(layout.events_path(RUN)).strip()
    assert json.loads(line) == {
        "ts": "2026-11-07T09:00:01.000000Z",
        "actor": f"controller-{RUN}",
        "kind": "task_created",
        "payload": {"task_id": "t1"},
    }
    assert event.kind == "task_created"
    assert "\n" not in event.to_line()


def test_required_kinds_exist():
    required = {
        "task_created", "call_prepared", "batch_submitted", "call_returned", "call_failed", "row_assessed",
        "packet_published", "verdict_received", "row_accepted", "row_rejected", "row_set_aside", "shipped",
    }
    assert required <= set(EVENT_KINDS)


def test_unknown_kind_and_bad_payload_are_rejected(local_store):
    log = make_log(local_store)
    with pytest.raises(EventError):
        log.append("made_up_kind", {})
    with pytest.raises(EventError):
        log.append("task_created", {"bad": float("nan")})
    with pytest.raises(EventError):
        log.append("task_created", {"bad": object()})
    assert not local_store.exists(layout.events_path(RUN))


def test_only_a_controller_may_write_events(local_store):
    from rps.layout import reviewer_actor

    with pytest.raises(EventError):
        EventLog(local_store, reviewer_actor("desk"))


def test_replay_returns_events_in_order_across_files(local_store):
    first = make_log(local_store, "20261107T090000Z-aaaa")
    second = make_log(local_store, "20261107T100000Z-bbbb")
    first.append("task_created", {"n": 1})
    first.append("task_created", {"n": 2})
    second.append("task_created", {"n": 3})
    result = replay(local_store)
    assert [e.payload["n"] for e in result.events] == [1, 2, 3]
    assert result.problems == []
    assert len(result.paths) == 2


def test_torn_last_line_is_skipped_and_reported(local_store):
    log = make_log(local_store)
    log.append("task_created", {"n": 1})
    log.append("task_created", {"n": 2})
    path = local_store.root / layout.events_path(RUN)
    with open(path, "ab") as handle:
        handle.write(b'{"ts":"2026-11-07T09:00:09.000000Z","actor":"controller-x","kind":"call_pre')
    result = replay(local_store)
    assert [e.payload["n"] for e in result.events] == [1, 2]
    assert len(result.problems) == 1
    problem = result.problems[0]
    assert problem.torn is True
    assert problem.line_number == 3
    assert "torn last line" in result.report()


def test_a_run_that_continues_after_a_torn_line_keeps_every_good_event(local_store):
    log = make_log(local_store)
    log.append("task_created", {"n": 1})
    path = local_store.root / layout.events_path(RUN)
    with open(path, "ab") as handle:
        handle.write(b'{"ts":"torn')
    log.append("task_created", {"n": 2})
    result = replay(local_store)
    assert [e.payload["n"] for e in result.events] == [1, 2]
    assert len(result.problems) == 1
    assert result.problems[0].torn is False  # not the last line any more, but still skipped and reported


def test_unreadable_lines_in_the_middle_are_skipped_and_reported():
    good = Event("2026-11-07T09:00:00.000000Z", "controller-x", "task_created", {"n": 1}).to_line()
    text = "\n".join([good, "not json", '{"ts": 1}', good, ""])
    events, problems = parse_event_text(text, "events/x")
    assert len(events) == 2
    assert [p.line_number for p in problems] == [2, 3]
    assert not any(p.torn for p in problems)


def test_final_line_without_newline_but_complete_is_accepted():
    good = Event("2026-11-07T09:00:00.000000Z", "controller-x", "task_created", {"n": 1}).to_line()
    events, problems = parse_event_text(good + "\n" + good, "events/x")
    assert len(events) == 2 and problems == []


def test_crlf_line_endings_are_tolerated():
    good = Event("2026-11-07T09:00:00.000000Z", "controller-x", "task_created", {"n": 1}).to_line()
    events, problems = parse_event_text(good + "\r\n" + good + "\r\n", "events/x")
    assert len(events) == 2 and problems == []


def test_unicode_and_line_separators_survive_a_round_trip(local_store):
    log = make_log(local_store)
    text = "café ☃ line separator 😀 quote\" backslash\\ newline\n tab\t"
    log.append("task_created", {"text": text})
    result = replay(local_store)
    assert result.events[0].payload["text"] == text


def test_log_rolls_to_a_new_part_file_past_the_size_limit(local_store):
    log = make_log(local_store, max_bytes=1500)
    for n in range(40):
        log.append("task_created", {"n": n, "pad": "x" * 100})
    parts = layout.sort_event_paths(local_store.list("events/"))
    assert len(parts) > 3
    assert all(len(local_store.read_bytes(p)) <= 1500 for p in parts)
    result = replay(local_store)
    assert [e.payload["n"] for e in result.events] == list(range(40))


def test_a_log_for_an_existing_run_continues_after_its_last_part(local_store):
    log = make_log(local_store, max_bytes=1500)
    for n in range(20):
        log.append("task_created", {"n": n, "pad": "x" * 100})
    again = make_log(local_store, max_bytes=1500)
    again.append("task_created", {"n": 20, "pad": "x" * 100})
    assert [e.payload["n"] for e in replay(local_store).events] == list(range(21))


def test_durable_kinds_flush_the_store(local_store):
    class Counting:
        def __init__(self, inner):
            self.inner = inner
            self.flushes = []

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def flush(self, force=False):
            self.flushes.append(force)
            return True

    store = Counting(local_store)
    log = EventLog(store, controller_actor(RUN), now=Clock())
    log.append("task_created", {})
    assert store.flushes == []
    log.append("batch_submitted", {"handle_id": "b1"})
    assert store.flushes == [True]
    log.append("call_returned", {}, durable=True)
    assert store.flushes == [True, True]
    log.append("batch_submitted", {"handle_id": "b2"}, durable=False)  # said no for this one: even a durable kind waits
    assert store.flushes == [True, True]
    log.append("task_created", {}, durable=None)  # left to the kind
    log.append("batch_submitted", {"handle_id": "b3"}, durable=None)
    assert store.flushes == [True, True, True]


def test_on_append_sees_each_event_after_it_is_written(local_store):
    seen = []
    log = make_log(local_store, on_append=lambda event: seen.append((event.kind, len(replay(local_store).events))))
    log.append("task_created", {})
    log.append("task_created", {})
    assert seen == [("task_created", 1), ("task_created", 2)]
