"""The status and the progress line say which cap refused a call, and how full it was.

A live run ended with "7 calls refused by a budget" and no word on which budget: the rebound cap of one
route, which calls that follow an answer cut off at the token limit fill quickly because each is held at the
estimate for its larger limit.
"""

from __future__ import annotations

from harness import World, task_spec
from test_controller_budgets import PRICES, bench, budget_config, unbilled_json


def test_a_purpose_cap_is_named_with_its_route_and_how_full_it_was(tmp_path):
    world = bench(tmp_path, pricing=0.004, production=0.02, rebound=0.01)
    controller = world.controller()
    controller.add_task(task_spec(("p1", "p2", "p3", "p4", "p5"), batch="pricing-run", purpose="pricing"))
    controller.add_task(task_spec(("001", "002", "003", "004"), batch="main"))
    world.drive(controller)
    assert [(cap, len(found)) for cap, found in controller._refused_by()] == [("the pricing cap of fake-batch ($0.0040)", 3)]
    text = controller.format_status()
    assert "3 call(s) refused by a budget this run" in text
    assert (
        "  3 by the pricing cap of fake-batch ($0.0040): $0.0040 was spent or held by calls in flight, "
        "each call needed room for $0.0020"
    ) in text
    progress = controller._progress_line(None, None)
    assert "waiting: 3 calls refused by the pricing cap of fake-batch ($0.0040)" in progress


def test_the_route_total_is_named_as_the_total_of_the_route(tmp_path):
    world = bench(tmp_path, total=0.006, production=0.05, pricing=0.05)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003", "004", "005"), batch="main"))
    controller.add_task(task_spec(("p1", "p2"), batch="pricing-run", purpose="pricing"))
    world.drive(controller)
    assert [(cap, len(found)) for cap, found in controller._refused_by()] == [("the total of fake-batch ($0.0060)", 4)]
    assert "  4 by the total of fake-batch ($0.0060): $0.0060 was spent or held by calls in flight" in controller.format_status()


def test_calls_refused_by_different_caps_are_counted_apart_with_the_largest_group_first(tmp_path):
    world = bench(tmp_path, total=0.006, pricing=0.004, production=0.05)
    controller = world.controller()
    controller.add_task(task_spec(("p1", "p2", "p3"), batch="pricing-run", purpose="pricing"))
    controller.add_task(task_spec(("001", "002", "003", "004", "005"), batch="main"))
    world.drive(controller)
    groups = [(cap, len(found)) for cap, found in controller._refused_by()]
    assert groups == [("the total of fake-batch ($0.0060)", 4), ("the pricing cap of fake-batch ($0.0040)", 1)]
    status = controller.format_status().splitlines()
    first = status.index("5 call(s) refused by a budget this run")
    assert status[first + 1].startswith("  4 by the total of fake-batch") and status[first + 2].startswith("  1 by the pricing cap")
    progress = controller._progress_line(None, None)
    assert "4 calls refused by the total of fake-batch ($0.0060), 1 calls refused by the pricing cap of fake-batch ($0.0040)" in progress


def test_the_cap_that_refused_the_retries_is_named_in_the_line_the_run_ends_with(tmp_path):
    world = World(tmp_path, config=budget_config(probe=0.008, rebound=0.004), prices=PRICES, behavior=unbilled_json)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003", "004"), purpose="probe"))
    world.drive(controller)
    controller.run()
    ended = [line for line in world.printed if " ended (" in line]
    assert ended and "waiting: 2 calls refused by the rebound cap of fake-batch ($0.0040)" in ended[-1]
    assert "  2 by the rebound cap of fake-batch ($0.0040)" in controller.format_status()


def test_nothing_is_said_when_no_call_was_refused(tmp_path):
    world = bench(tmp_path, production=0.05)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller)
    assert controller._refused_by() == []
    assert "refused" not in controller.format_status() and "refused" not in controller._progress_line(None, None)
