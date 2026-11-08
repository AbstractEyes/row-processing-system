"""Budgets enforced by the controller: a cap per (route, purpose) and a total per route."""

from collections import Counter

import pytest

from rps.config import PURPOSES
from rps.ledger import PriceTable, Usage
from rps.providers import FakeOutcome, FakeWriter

from harness import World, task_spec
from helpers import make_config

# With these prices a call is estimated at (1000 input tokens x $1 + 500 output tokens x $2) / 1M
# = $0.002, and the fake writer's usual usage (1000 in, 200 out) costs (1000 + 400) / 1M = $0.0014.
EST = 0.002
COST = 0.0014
PRICES = PriceTable.from_dict(
    {"date": "2026-11-06", "batch_discount": 1.0, "models": {"fake-model": {"input": 1.0, "output": 2.0}}}
)


def budget_config(*, total=0.05, **caps):
    """One batch route whose purposes have no money unless a cap is given."""
    full = {purpose: 0.0 for purpose in PURPOSES}
    full.update(caps)
    return make_config(
        routes={
            "fake-batch": {
                "provider": "fake",
                "model": "fake-model",
                "kind": "batch",
                "effort": "low",
                "budget_usd": total,
                "purpose_caps": full,
                "max_output_tokens": 500,
            }
        }
    )


def bench(tmp_path, **caps):
    total = caps.pop("total", 0.05)
    return World(tmp_path, config=budget_config(total=total, **caps), prices=PRICES)


def unbilled_json(payload):
    """An attempt that comes back unusable but is billed in full the first time."""
    if payload["attempt"] == 1:
        return FakeOutcome(output="this is not json", usage=Usage(input_tokens=1000, output_tokens=500))
    return FakeOutcome(output='{"id": "ok", "thoughts": ["fine"]}')


# ------------------------------------------------------------------ purposes


def test_every_call_carries_a_purpose_and_is_charged_to_it(tmp_path):
    world = bench(tmp_path, test=0.02, pricing=0.02, probe=0.02, production=0.02)
    controller = world.controller()
    for purpose in ("test", "pricing", "probe", "production"):
        controller.add_task(task_spec(("001", "002"), batch=f"{purpose}-batch", purpose=purpose))
    world.drive(controller)
    prepared = world.events_of("call_prepared")
    assert Counter(e.payload["purpose"] for e in prepared) == {"test": 2, "pricing": 2, "probe": 2, "production": 2}
    assert all(e.payload["purpose"] in PURPOSES for e in prepared)
    for purpose in ("test", "pricing", "probe", "production"):
        spend = controller.state.spend("fake-batch", purpose)
        assert spend.calls == 2 and spend.actual == pytest.approx(2 * COST) and spend.reserved == pytest.approx(0)
    assert controller.state.spend("fake-batch", "rebound").calls == 0
    assert controller.state.spend("fake-batch").calls == 8
    assert world.events_of("budget_refused") == []


def test_a_pricing_cap_stops_pricing_calls_while_production_continues(tmp_path):
    world = bench(tmp_path, pricing=0.004, production=0.02, rebound=0.01)
    controller = world.controller()
    controller.add_task(task_spec(("p1", "p2", "p3", "p4", "p5"), batch="pricing-run", purpose="pricing"))
    controller.add_task(task_spec(("001", "002", "003", "004"), batch="main"))
    controller.step()
    # The two pricing calls in flight hold the whole pricing cap, but they may cost less than they hold:
    # the other three wait for them, and nothing is refused yet.
    assert Counter(e.payload["purpose"] for e in world.events_of("call_prepared")) == {"pricing": 2, "production": 4}
    assert world.events_of("budget_refused") == []
    world.drive(controller)
    summary = controller.run()

    prepared = world.events_of("call_prepared")
    assert Counter(e.payload["purpose"] for e in prepared) == {"pricing": 2, "production": 4}
    # The two calls cost $0.0028 between them, and a third would take the cap to $0.0048: now it is refused for good.
    refused = world.events_of("budget_refused")
    assert {e.payload["line"] for e in refused} == {"p3", "p4", "p5"}
    for event in refused:
        numbers = event.payload
        assert (numbers["route"], numbers["purpose"], numbers["reason"]) == ("fake-batch", "pricing", "purpose_cap")
        assert numbers["est_usd"] == pytest.approx(EST)
        assert numbers["purpose_cap"] == pytest.approx(0.004)
        assert numbers["purpose_spent"] == pytest.approx(2 * COST) and numbers["purpose_held"] == pytest.approx(0)
        assert numbers["route_cap"] == pytest.approx(0.05) and numbers["attempt"] == 1
        assert "pricing" in numbers["message"] and "cap" in numbers["message"]

    # a refused call never reaches the provider, and production was not held back
    assert len(world.provider.submitted_calls) == 6
    assert not {e.payload["call_id"] for e in refused} & set(world.provider.submitted_calls)
    assert controller.state.spend("fake-batch", "pricing").calls == 2
    assert controller.state.spend("fake-batch", "production").calls == 4
    assert controller.state.counts()["rows"] == {"accepted": 6}
    assert summary.reason == "idle" and summary.parked_by_budget == 3
    assert summary.progress["lines_new"] == 3  # the refused lines are not done, and they are not lost


def test_a_purpose_with_no_cap_is_refused_before_anything_is_sent(tmp_path):
    config = make_config(
        routes={
            "fake-batch": {
                "provider": "fake", "model": "fake-model", "kind": "batch", "budget_usd": 1.0,
                "purpose_caps": {"production": 1.0}, "max_output_tokens": 500,
            }
        }
    )
    world = World(tmp_path, config=config, prices=PRICES)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002"), purpose="probe"))
    summary = controller.run()
    assert summary.reason == "idle" and summary.parked_by_budget == 2
    assert world.provider.submit_count == 0
    refused = world.events_of("budget_refused")
    assert len(refused) == 2 and {e.payload["purpose_cap"] for e in refused} == {0.0}
    assert world.events_of("call_prepared") == []


def test_the_route_total_stops_every_purpose(tmp_path):
    world = bench(tmp_path, total=0.006, production=0.05, pricing=0.05)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003", "004", "005"), batch="main"))
    controller.add_task(task_spec(("p1", "p2"), batch="pricing-run", purpose="pricing"))
    controller.step()
    assert len(world.events_of("call_prepared")) == 3  # three estimates of $0.002 fit under $0.006
    assert world.events_of("budget_refused") == []  # the rest wait for the three to come back
    world.drive(controller)
    assert len(world.events_of("call_prepared")) == 3  # which cost $0.0042 and leave no room for a fourth
    refused = world.events_of("budget_refused")
    assert len(refused) == 4
    assert {e.payload["reason"] for e in refused} == {"route_cap"}
    assert {e.payload["purpose"] for e in refused} == {"production", "pricing"}  # pricing had room of its own
    for event in refused:
        assert event.payload["route_cap"] == pytest.approx(0.006)
        assert event.payload["route_spent"] == pytest.approx(3 * COST) and event.payload["route_held"] == pytest.approx(0)
    assert "route" in refused[0].payload["message"] and "total" in refused[0].payload["message"]
    # the money already spent leaves no room for another estimate, so a later run is refused again
    later = world.controller()
    later.run()
    assert len(world.provider.submitted_calls) == 3
    assert len(world.events_of("budget_refused")) == 8


def test_calls_in_flight_hold_their_estimated_cost_and_a_call_that_does_not_fit_yet_waits(tmp_path):
    world = bench(tmp_path, production=0.0035)  # room for one estimate ($0.002) but not two
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    controller.step()
    (prepared,) = world.events_of("call_prepared")
    assert prepared.payload["line"] == "001"
    assert world.events_of("budget_refused") == []  # the second call waits: it is not refused, and not parked
    spend = controller.state.spend("fake-batch", "production")
    assert spend.reserved == pytest.approx(EST) and spend.actual == 0  # nothing paid yet, one call in flight
    assert controller.is_idle() is False and controller.state.job_progress(2)["lines_new"] == 1
    assert "1 calls waiting for room under the production cap of fake-batch ($0.0035)" in controller._progress_line(None, None)
    # the first call turned out cheaper than its estimate ($0.0014), so the second fits in this run
    world.drive(controller)
    spend = controller.state.spend("fake-batch", "production")
    assert spend.calls == 2 and spend.actual == pytest.approx(2 * COST) and spend.reserved == pytest.approx(0)
    assert world.events_of("budget_refused") == []
    assert controller.state.job_progress(2)["lines_accepted"] == 2


def test_a_refused_call_is_asked_again_by_the_next_run_and_a_raised_cap_lets_it_through(tmp_path):
    world = bench(tmp_path, pricing=0.004, production=0.02, rebound=0.01)
    first = world.controller()
    first.add_task(task_spec(("p1", "p2", "p3", "p4"), batch="pricing-run", purpose="pricing"))
    world.drive(first)
    assert len(world.events_of("budget_refused")) == 2 and len(world.provider.submitted_calls) == 2

    again = world.controller()  # the same caps: refused again, nothing sent
    summary = again.run()
    assert summary.reason == "idle" and summary.parked_by_budget == 2
    assert len(world.events_of("budget_refused")) == 4 and len(world.provider.submitted_calls) == 2

    world.config = budget_config(pricing=0.02, production=0.02, rebound=0.01)  # the owner raises the cap
    third = world.controller()
    world.drive(third)
    assert len(world.events_of("budget_refused")) == 4  # no new refusals
    assert len(world.provider.submitted_calls) == 4
    assert third.state.spend("fake-batch", "pricing").calls == 4
    assert third.state.job_progress(2)["lines_accepted"] == 4


# ------------------------------------------------------------------ rebounds


def test_rebounds_draw_from_their_own_cap(tmp_path):
    # Four first attempts use up the probe cap exactly; each is billed $0.002 and returns nothing usable.
    world = World(
        tmp_path,
        config=budget_config(probe=0.008, rebound=0.004),
        prices=PRICES,
        behavior=unbilled_json,
    )
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003", "004"), purpose="probe"))
    world.drive(controller)
    summary = controller.run()

    state = controller.state
    assert state.spend("fake-batch", "probe").actual == pytest.approx(0.008)  # at its cap; the retries cost it nothing
    rebound = [e for e in world.events_of("call_prepared") if e.payload["purpose"] == "rebound"]
    assert len(rebound) == 2 and all(e.payload["attempt"] == 2 and e.payload["retry_of"] for e in rebound)
    assert state.spend("fake-batch", "rebound").calls == 2
    assert state.spend("fake-batch", "rebound").actual == pytest.approx(2 * COST)
    assert state.spend("fake-batch", "probe").calls == 4  # the first attempts, still charged to probe

    refused = world.events_of("budget_refused")
    assert len(refused) == 2
    for event in refused:
        assert event.payload["purpose"] == "rebound" and event.payload["attempt"] == 2
        assert event.payload["reason"] == "purpose_cap" and event.payload["purpose_cap"] == pytest.approx(0.004)
        assert event.payload["purpose_spent"] == pytest.approx(2 * COST)  # the two retries that went out cost $0.0014 each
    assert summary.reason == "idle" and summary.parked_by_budget == 2
    assert summary.progress["lines_accepted"] == 2 and summary.progress["lines_retry"] == 2


def test_raising_the_rebound_cap_lets_the_refused_retries_run_on_the_next_run(tmp_path):
    world = World(
        tmp_path,
        config=budget_config(probe=0.008, rebound=0.004),
        prices=PRICES,
        behavior=unbilled_json,
    )
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003", "004"), purpose="probe"))
    world.drive(first)
    assert first.state.job_progress(2)["lines_accepted"] == 2

    world.config = budget_config(probe=0.008, rebound=0.02)
    second = world.controller()
    world.drive(second)
    assert second.state.job_progress(2)["lines_accepted"] == 4
    assert second.state.spend("fake-batch", "rebound").calls == 4
    assert second.state.spend("fake-batch", "probe").calls == 4  # still only the first attempts
    # the retry carries the failed attempt's error as feedback, whatever purpose paid for it
    retries = [req[2] for batch in world.provider.batches.values() for req in batch["requests"] if req[2]["attempt"] == 2]
    assert len(retries) == 4 and all("unusable output" in req["feedback"][0] for req in retries)


def test_a_retry_of_a_production_call_is_a_rebound_whatever_the_original_purpose(tmp_path):
    world = World(
        tmp_path,
        config=budget_config(production=0.02, rebound=0.02),
        prices=PRICES,
        behavior=unbilled_json,
    )
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller)
    assert [e.payload["purpose"] for e in world.events_of("call_prepared")] == ["production"] * 2 + ["rebound"] * 2
    assert controller.state.spend("fake-batch", "production").calls == 2
    assert controller.state.spend("fake-batch", "rebound").calls == 2
    assert world.events_of("budget_refused") == []


# ------------------------------------------------------------------ what is reported


def test_the_status_shows_spend_per_route_and_purpose_against_its_cap(tmp_path):
    world = bench(tmp_path, pricing=0.004, production=0.02, rebound=0.01)
    controller = world.controller()
    controller.add_task(task_spec(("p1", "p2", "p3"), batch="pricing-run", purpose="pricing"))
    controller.add_task(task_spec(("001", "002"), batch="main"))
    world.drive(controller)
    cells = {(row["route"], row["purpose"]): row for row in controller.status()["spend"]}
    pricing = cells[("fake-batch", "pricing")]
    assert pricing["calls"] == 2 and pricing["cap"] == pytest.approx(0.004) and pricing["spent"] == pytest.approx(2 * COST)
    production = cells[("fake-batch", "production")]
    assert production["calls"] == 2 and production["cap"] == pytest.approx(0.02)
    assert cells[("fake-batch", "(route total)")]["spent"] == pytest.approx(4 * COST)
    assert cells[("fake-batch", "(route total)")]["cap"] == pytest.approx(0.05)
    text = controller.format_status()
    assert "1 call(s) refused by a budget this run" in text
    for expected in ("pricing", "production", "rebound", "(route total)", "$0.0040", "$0.0200"):
        assert expected in text


def test_the_ledger_files_record_spend_by_purpose(tmp_path):
    import json

    world = bench(tmp_path, pricing=0.004, production=0.02, rebound=0.01)
    controller = world.controller()
    controller.add_task(task_spec(("p1", "p2", "p3"), batch="pricing-run", purpose="pricing"))
    controller.add_task(task_spec(("001", "002"), batch="main"))
    world.drive(controller)
    summary = json.loads(world.ship_store.read_text("ledger/summary.json"))
    purposes = summary["routes"]["fake-batch"]["purposes"]
    assert purposes["pricing"]["calls"] == 2 and purposes["pricing"]["cap_usd"] == pytest.approx(0.004)
    assert purposes["production"]["calls"] == 2 and purposes["rebound"]["calls"] == 0
    assert summary["routes"]["fake-batch"]["spent_usd"] == pytest.approx(4 * COST)
    lines = [json.loads(line) for line in world.ship_store.read_text("ledger/tasks.jsonl").splitlines()]
    assert sorted(sorted(entry["usd_by_purpose"]) for entry in lines) == [["pricing"], ["production"]]


# ------------------------------------------------------------------ the price tier a call is held at

TIERED = PriceTable.from_dict(
    {
        "date": "2026-11-06",
        "batch_discount": 1.0,
        "models": {"fake-model": {"tiers": {"offpeak": {"input": 0.5, "output": 1.0}, "peak": {"input": 1.0, "output": 2.0}}}},
    }
)


class OffPeakWriter(FakeWriter):
    """A writer that can promise every call goes out in the cheap tier."""

    def hold_tier(self):
        return "offpeak"


def test_a_call_is_held_at_the_tier_its_writer_promises_and_otherwise_at_the_dearest(tmp_path):
    estimates = {}
    for label, writer_class in (("no promise", FakeWriter), ("promised", OffPeakWriter)):
        world = World(
            tmp_path / label.replace(" ", "-"),
            config=budget_config(production=0.003),
            prices=TIERED,
            writer_class=writer_class,
        )
        controller = world.controller()
        controller.add_task(task_spec(("001", "002", "003")))
        controller.step()
        estimates[label] = [e.payload["est_usd"] for e in world.events_of("call_prepared")]
    # Dearest tier: (1000 x $1 + 500 x $2) / 1M = $0.002 a call, so one call fills the cap of $0.003.
    assert estimates["no promise"] == [pytest.approx(0.002)]
    # Off-peak tier: (1000 x $0.5 + 500 x $1) / 1M = $0.001 a call, so all three fit.
    assert estimates["promised"] == [pytest.approx(0.001)] * 3
