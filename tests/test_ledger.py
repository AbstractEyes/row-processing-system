import json
from datetime import date

import pytest

from rps.ledger import Ledger, PriceError, PriceTable, Usage, effective_purpose
from rps.state import State

from helpers import Builder, make_config


def table():
    return PriceTable.from_dict(
        {
            "date": "2026-11-06",
            "batch_discount": 0.5,
            "models": {
                "fake-model": {"input": 1.0, "output": 2.0, "cache_write": 1.25, "cache_read": 0.1},
                "old-model": {"input": 1.0, "output": 1.0, "valid_until": "2026-11-01"},
            },
        }
    )


def ledger(**config_overrides):
    return Ledger(make_config(**config_overrides), table())


def small_caps_config(pricing=0.05, rebound=0.05, production=0.10, total=0.12):
    return make_config(
        routes={
            "fake-batch": {
                "provider": "fake", "model": "fake-model", "kind": "batch", "budget_usd": total,
                "purpose_caps": {"pricing": pricing, "rebound": rebound, "production": production},
            }
        }
    )


def test_cost_counts_every_token_bucket_and_halves_batches():
    prices = table()
    usage = Usage(input_tokens=1_000_000, cache_write_tokens=1_000_000, cache_read_tokens=1_000_000,
                  output_tokens=500_000, reasoning_tokens=500_000)
    # 1.0 + 1.25 + 0.1 + (0.5 + 0.5) * 2.0 = 4.35
    assert prices.cost("fake-model", usage) == pytest.approx(4.35)
    assert prices.cost("fake-model", usage, batch=True) == pytest.approx(2.175)
    assert prices.cost("fake-model", Usage()) == 0


def test_estimate_is_an_upper_bound_on_cost():
    prices = table()
    assert prices.estimate("fake-model", 1000, 500) == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)
    assert prices.estimate("fake-model", 1000, 500, batch=True) == pytest.approx((1000 + 1000) / 1e6 * 0.5)
    usage = Usage(input_tokens=800, cache_read_tokens=100, output_tokens=300)
    assert prices.cost("fake-model", usage) <= prices.estimate("fake-model", 1000, 500)


def test_unknown_model_cannot_be_priced():
    with pytest.raises(PriceError):
        table().cost("mystery", Usage(input_tokens=1))
    config = make_config(routes={"r": {"provider": "x", "model": "mystery", "budget_usd": 1}})
    with pytest.raises(PriceError):
        Ledger(config, table()).validate_prices()


def test_the_packaged_price_table_loads_and_has_a_date():
    prices = PriceTable.default()
    date.fromisoformat(prices.date)
    assert "fake-model" in prices.models
    assert prices.batch_discount == 0.5


def test_price_table_loads_from_a_file_and_rejects_bad_ones(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"date": "2026-11-06", "models": {"m": {"input": 1, "output": 2}}}), encoding="utf-8")
    assert PriceTable.load(path).price("m").output == 2
    path.write_text(json.dumps({"models": {}}), encoding="utf-8")
    with pytest.raises(PriceError):
        PriceTable.load(path)
    path.write_text(json.dumps({"date": "not-a-date", "models": {}}), encoding="utf-8")
    with pytest.raises(PriceError):
        PriceTable.load(path)
    with pytest.raises(PriceError):
        PriceTable.load(tmp_path / "missing.json")


def test_price_warnings_flag_old_tables_and_expired_prices():
    notes = table().warnings(today=date(2026, 12, 20))
    assert any("days old" in note for note in notes)
    assert any("old-model" in note for note in notes)
    assert table().warnings(today=date(2026, 11, 7)) == [
        "price for old-model was valid until 2026-11-01"
    ]


def test_a_retry_is_always_charged_to_the_rebound_purpose():
    assert effective_purpose("production", 1) == "production"
    assert effective_purpose("pricing", 1) == "pricing"
    for purpose in ("test", "pricing", "probe", "production", "rebound"):
        assert effective_purpose(purpose, 2) == "rebound"
        assert effective_purpose(purpose, 3) == "rebound"
    with pytest.raises(ValueError):
        effective_purpose("fun", 1)


def test_a_call_inside_every_cap_is_allowed():
    b = Builder()
    state = State.from_events(b.events)
    decision = ledger().check(state, "fake-batch", "production", 0.5)
    assert decision.ok and decision.reason is None


def test_a_call_over_its_purpose_cap_is_refused_with_the_numbers():
    config = small_caps_config()
    led = Ledger(config, table())
    b = Builder()
    task = b.task(purpose="pricing")
    b.prepare(task, "001", purpose="pricing", est=0.04)
    state = State.from_events(b.events)
    decision = led.check(state, "fake-batch", "pricing", 0.02)
    assert not decision.ok and decision.reason == "purpose_cap"
    numbers = decision.numbers()
    assert numbers["route"] == "fake-batch" and numbers["purpose"] == "pricing"
    assert numbers["est_usd"] == pytest.approx(0.02)
    assert numbers["purpose_spent"] == pytest.approx(0.04)
    assert numbers["purpose_cap"] == pytest.approx(0.05)
    assert numbers["route_spent"] == pytest.approx(0.04)
    assert numbers["route_cap"] == pytest.approx(0.12)
    assert "pricing" in decision.message()


def test_a_pricing_cap_stops_pricing_calls_while_production_continues():
    led = Ledger(small_caps_config(), table())
    b = Builder()
    pricing_task = b.task(batch="p", lines={"001": "x", "002": "y"}, purpose="pricing")
    for line in ("001", "002"):
        b.prepare(pricing_task, line, purpose="pricing", est=0.025)
    state = State.from_events(b.events)
    # The pricing cap ($0.05) is now fully held by two in-flight calls.
    assert not led.check(state, "fake-batch", "pricing", 0.01).ok
    assert led.check(state, "fake-batch", "pricing", 0.01).reason == "purpose_cap"
    # Production has its own cap and carries on.
    assert led.check(state, "fake-batch", "production", 0.02).ok
    assert led.check(state, "fake-batch", "production", 0.04).ok


def test_rebounds_draw_from_their_own_cap():
    led = Ledger(small_caps_config(rebound=0.03, production=0.20, total=0.50), table())
    b = Builder()
    task = b.task(lines={"001": "x"})
    first = b.prepare(task, "001", attempt=1, purpose="production", est=0.04)
    b.submit([first])
    b.failed(first, billed=True, usd=0.04)
    retry = b.prepare(task, "001", attempt=2, purpose=effective_purpose("production", 2), est=0.02)
    state = State.from_events(b.events)
    # The retry's cost sits under rebound, not production.
    assert state.spend("fake-batch", "rebound").total == pytest.approx(0.02)
    assert state.spend("fake-batch", "production").total == pytest.approx(0.04)
    # A second rebound would pass the rebound cap of $0.03, even though production has room.
    assert not led.check(state, "fake-batch", "rebound", 0.02).ok
    assert led.check(state, "fake-batch", "rebound", 0.02).reason == "purpose_cap"
    assert led.check(state, "fake-batch", "production", 0.10).ok
    # Rebound spend never used up the production cap.
    assert state.spend("fake-batch", "production").total < 0.20
    del retry


def test_the_route_total_stops_every_purpose():
    led = Ledger(small_caps_config(pricing=0.10, rebound=0.10, production=0.10, total=0.12), table())
    b = Builder()
    task = b.task(lines={"001": "x"})
    b.prepare(task, "001", purpose="production", est=0.09)
    state = State.from_events(b.events)
    # Production has $0.01 left under its own cap and the route has $0.03 left.
    assert led.check(state, "fake-batch", "pricing", 0.04).reason == "route_cap"
    assert led.check(state, "fake-batch", "pricing", 0.03).ok
    assert led.check(state, "fake-batch", "production", 0.02).reason == "purpose_cap"
    assert not led.route_stopped(state, "fake-batch")


def test_a_route_stops_when_its_budget_is_spent():
    led = Ledger(small_caps_config(production=0.10, total=0.10), table())
    b = Builder()
    task = b.task(lines={"001": "x"})
    call = b.prepare(task, "001", purpose="production", est=0.10)
    b.submit([call])
    b.returned(call, usd=0.10)
    state = State.from_events(b.events)
    assert led.route_stopped(state, "fake-batch")
    assert not led.check(state, "fake-batch", "production", 0.0001).ok


def test_spending_exactly_up_to_a_cap_is_allowed():
    led = Ledger(small_caps_config(production=0.10, total=0.30), table())
    b = Builder()
    task = b.task(lines={"001": "x"})
    b.prepare(task, "001", purpose="production", est=0.06)
    state = State.from_events(b.events)
    assert led.check(state, "fake-batch", "production", 0.04).ok  # 0.06 + 0.04 == the cap
    assert not led.check(state, "fake-batch", "production", 0.0401).ok


def test_a_purpose_with_no_cap_is_refused():
    led = Ledger(small_caps_config(), table())  # no cap for test or probe
    state = State.from_events([])
    decision = led.check(state, "fake-batch", "test", 0.001)
    assert not decision.ok and decision.reason == "purpose_cap" and decision.purpose_cap == 0


def test_free_calls_pass_even_with_a_zero_cap():
    led = Ledger(small_caps_config(), table())
    state = State.from_events([])
    assert led.check(state, "fake-batch", "test", 0.0).ok


def test_status_table_shows_spend_per_route_and_purpose_against_its_cap():
    led = Ledger(small_caps_config(), table())
    b = Builder()
    task = b.task(lines={"001": "x"})
    call = b.prepare(task, "001", purpose="production", est=0.02)
    b.submit([call])
    b.returned(call, usd=0.015)
    state = State.from_events(b.events)
    rows = led.status_rows(state)
    production = next(r for r in rows if r["purpose"] == "production")
    assert production["spent"] == pytest.approx(0.015)
    assert production["cap"] == pytest.approx(0.10)
    assert production["remaining"] == pytest.approx(0.085)
    total = next(r for r in rows if r["purpose"] == "(route total)")
    assert total["cap"] == pytest.approx(0.12)
    text = led.format_status(state)
    for purpose in ("test", "pricing", "probe", "rebound", "production", "(route total)"):
        assert purpose in text
    assert "prices dated 2026-11-06" in text
    assert "$0.0150" in text


def test_task_lines_carry_usage_cost_attempts_and_row_counts():
    led = Ledger(make_config(), table())
    b = Builder()
    task = b.task(lines={"001": "x", "002": "y"})
    c1, c2 = b.prepare(task, "001"), b.prepare(task, "002")
    b.submit([c1, c2])
    (r1,) = b.returned(c1, usd=0.01)
    b.failed(c2, billed=True, usd=0.002)
    b.assessed(r1, c1)
    state = State.from_events(b.events)
    (line,) = led.task_lines(state)
    assert line["task_id"] == task and line["route"] == "fake-batch" and line["model"] == "fake-model"
    assert line["calls"] == 2 and line["attempts"] == 2
    assert line["input_tokens"] == 100 and line["output_tokens"] == 50
    assert line["usd"] == pytest.approx(0.012)
    assert line["usd_by_purpose"] == {"production": pytest.approx(0.012)}
    assert line["price_table_date"] == "2026-11-06"
    assert line["rows_out"] == 1 and line["rows_passed"] == 1 and line["rows_accepted"] == 0
    summary = led.summary(state)
    assert summary["price_table_date"] == "2026-11-06"
    assert summary["routes"]["fake-batch"]["purposes"]["production"]["spent_usd"] == pytest.approx(0.012)
