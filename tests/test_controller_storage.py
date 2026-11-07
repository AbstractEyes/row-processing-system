"""Cache storage is charged to the ledger as lines of its own.

A provider that keeps the writing rules in a cache for you bills the hours it is kept. The writer
notes each cache with its size and the stretch of its life; the controller turns each note into a
`cache_storage` event that counts as spend under the route and purpose, and shows it in the ledger.
"""

from __future__ import annotations

import json

import pytest

from rps.config import ConfigError
from rps.events import Event
from rps.ledger import PriceTable
from rps.providers import FakeWriter, Handle
from rps.state import State

from harness import World, task_spec
from helpers import ROUTE_CAPS, make_config

NAME = "cachedContents/c1"
KEY = "fake-model|rules-v1"


def priced(storage=0.5):
    card = {"input": 1.0, "output": 2.0, "cache_read": 0.1}
    if storage is not None:
        card["cache_storage"] = storage
    return PriceTable.from_dict({"date": "2026-11-07", "models": {"fake-model": {"sync": dict(card), "batch": dict(card)}}})


def note(**fields):
    base = {"route": "fake-batch", "key": KEY, "name": NAME, "model": "fake-model", "tokens": 12_000}
    base.update(fields)
    return base


class CachingWriter(FakeWriter):
    """A fake writer that keeps a provider-side cache the way the Gemini writer does and says so in notes."""

    bills_cache_storage = True
    first_note = None
    extension = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.notes = []
        self.made = False
        self.extended = False

    def restore(self, notes):
        self.made = bool(notes)

    def drain_notes(self):
        notes, self.notes = self.notes, []
        return notes

    def submit(self, requests):
        handle = super().submit(requests)
        if not self.made:
            self.made = True
            first = type(self).first_note
            self.notes.append(("cache_created", first if first is not None else note(created="2026-11-07T09:00:00Z", expires="2026-11-07T15:00:00Z")))
        return Handle(handle.handle_id, handle.call_ids, handle.mode, data={"cache_name": NAME})

    def poll(self, handle):
        status = super().poll(handle)
        if type(self).extension is not None and not self.extended:
            self.extended = True
            self.notes.append(("cache_created", type(self).extension))
        return status


@pytest.fixture(autouse=True)
def fresh_notes():
    CachingWriter.first_note = None
    CachingWriter.extension = None
    yield
    CachingWriter.first_note = None
    CachingWriter.extension = None


def settled(tmp_path, lines=("001", "002", "003"), **world):
    world = World(tmp_path, writer_class=CachingWriter, prices=priced(), **world)
    controller = world.controller()
    controller.add_task(task_spec(lines))
    world.drive(controller)
    return world, controller


def test_the_storage_of_a_cache_is_charged_as_a_ledger_line_of_its_own(tmp_path):
    world, controller = settled(tmp_path)
    [event] = world.events_of("cache_storage")
    charge = event.payload
    assert (charge["route"], charge["purpose"], charge["name"], charge["key"]) == ("fake-batch", "production", NAME, KEY)
    assert charge["tokens"] == 12_000 and charge["hours"] == pytest.approx(6.0) and charge["rate"] == 0.5
    assert charge["usd"] == pytest.approx(0.036)  # 12,000 tokens x 6 hours x $0.50 per million tokens per hour
    assert (charge["from"], charge["to"]) == ("2026-11-07T09:00:00Z", "2026-11-07T15:00:00Z")
    assert charge["price_table_date"] == "2026-11-07" and charge["tokens_estimated"] is False
    # it is spent money under the route and purpose, on top of what the calls cost, and it is not a call
    calls_usd = sum(call.usd for call in controller.state.calls.values())
    spend = controller.state.spend("fake-batch", "production")
    assert spend.actual == pytest.approx(calls_usd + 0.036) and spend.calls == 3
    assert controller.state.storage_usd("fake-batch") == pytest.approx(0.036)
    assert controller.state.recompute_spend()[("fake-batch", "production")].actual == pytest.approx(spend.actual)


def test_the_ledger_files_and_the_status_show_the_storage_line(tmp_path):
    world, controller = settled(tmp_path)
    route = json.loads(world.ship_store.read_text("ledger/summary.json"))["routes"]["fake-batch"]
    assert route["storage_usd"] == pytest.approx(0.036) and route["purposes"]["production"]["storage_usd"] == pytest.approx(0.036)
    assert route["purposes"]["pricing"]["storage_usd"] == 0
    [stored] = route["storage"]
    assert stored["purpose"] == "production" and stored["name"] == NAME and stored["tokens"] == 12_000
    assert stored["hours"] == pytest.approx(6.0) and stored["usd"] == pytest.approx(0.036)
    assert route["spent_usd"] == pytest.approx(0.036 + sum(call.usd for call in controller.state.calls.values()))
    assert "cache storage on fake-batch: $0.0360 in 1 charge(s)" in controller.format_status()


def test_storage_goes_to_the_purpose_most_of_the_calls_that_use_the_cache_carry(tmp_path):
    world = World(tmp_path, writer_class=CachingWriter, prices=priced())
    controller = world.controller()
    controller.add_task(task_spec(("001",), batch="a", purpose="production"))
    controller.add_task(task_spec(("101", "102"), batch="b", purpose="pricing"))
    world.drive(controller)
    [event] = world.events_of("cache_storage")
    assert event.payload["purpose"] == "pricing"
    assert controller.state.storage_usd("fake-batch", "pricing") == pytest.approx(0.036)
    assert controller.state.storage_usd("fake-batch", "production") == 0


def test_a_cache_kept_alive_is_charged_only_for_the_hours_added(tmp_path):
    CachingWriter.extension = note(extended_from="2026-11-07T15:00:00Z", expires="2026-11-07T19:00:00Z")
    world, controller = settled(tmp_path, polls_until_ended=1)
    charges = [event.payload for event in world.events_of("cache_storage")]
    assert [(c["from"], c["to"]) for c in charges] == [
        ("2026-11-07T09:00:00Z", "2026-11-07T15:00:00Z"),
        ("2026-11-07T15:00:00Z", "2026-11-07T19:00:00Z"),
    ]
    assert [c["hours"] for c in charges] == [pytest.approx(6.0), pytest.approx(4.0)]
    assert controller.state.storage_usd("fake-batch") == pytest.approx(0.036 + 0.024)


def test_a_cache_that_was_not_kept_any_longer_costs_nothing_more(tmp_path):
    CachingWriter.extension = note(extended_from="2026-11-07T15:00:00Z", expires="2026-11-07T15:00:00Z")
    world, _controller = settled(tmp_path, polls_until_ended=1)
    assert len(world.events_of("cache_storage")) == 1


def test_a_restart_does_not_charge_a_cache_again(tmp_path):
    world, controller = settled(tmp_path)
    before = controller.state.storage_usd("fake-batch")
    second = world.controller()
    second.run()
    assert len(world.events_of("cache_storage")) == 1 and second.state.storage_usd("fake-batch") == pytest.approx(before)
    # replaying the same events twice gives the same ledger
    events = world.events()
    state = State.from_events(events)
    for event in events:
        state.apply(event)
    assert len(state.storage) == 1 and state.storage_usd("fake-batch") == pytest.approx(before)
    assert state.snapshot() == State.from_events(events).snapshot()


def test_a_note_without_a_size_or_dates_is_reported_and_not_charged(tmp_path):
    CachingWriter.first_note = note(tokens=None, created="2026-11-07T09:00:00Z", expires="2026-11-07T15:00:00Z")
    world, _controller = settled(tmp_path)
    assert not world.events_of("cache_storage") and len(world.events_of("cache_created")) == 1  # still remembered
    assert any("cannot be charged" in line and NAME in line for line in world.printed)
    CachingWriter.first_note = note(created="soon", expires="2026-11-07T15:00:00Z")
    world, _controller = settled(tmp_path / "second")
    assert not world.events_of("cache_storage")


def test_the_storage_counts_against_the_caps_like_any_other_spend(tmp_path):
    config = make_config(
        routes={
            "fake-batch": {
                "provider": "fake", "model": "fake-model", "kind": "batch", "budget_usd": 10.0,
                "purpose_caps": {**ROUTE_CAPS, "production": 0.05},
            }
        }
    )
    _world, controller = settled(tmp_path, config=config)
    spend = controller.state.spend("fake-batch", "production")
    assert spend.total > 0.036
    assert controller.ledger.check(controller.state, "fake-batch", "production", 0.005).ok
    refused = controller.ledger.check(controller.state, "fake-batch", "production", 0.02)
    assert not refused.ok and refused.reason == "purpose_cap"  # it would fit, were the storage not counted


def test_a_route_that_keeps_a_billed_cache_needs_a_storage_price_for_its_model(tmp_path):
    world = World(tmp_path, writer_class=CachingWriter, prices=priced(storage=None))
    with pytest.raises(ConfigError, match="cache_storage"):
        world.controller().start()
    off = make_config(
        routes={"fake-batch": {"provider": "fake", "model": "fake-model", "kind": "batch", "enabled": False, "budget_usd": 1.0}}
    )
    world = World(tmp_path / "off", config=off, writer_class=CachingWriter, prices=priced(storage=None))
    world.controller().start()  # a switched-off route is not asked


def test_a_cache_storage_event_with_missing_fields_is_counted_as_a_bad_event_not_applied():
    state = State()
    state.apply(Event(ts="2026-11-07T09:00:00.000000Z", actor="controller-test", kind="cache_storage", payload={"route": "fake-batch"}))
    assert state.storage == [] and len(state.bad_events) == 1
