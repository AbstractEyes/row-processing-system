"""A call whose estimated prompt passes the provider's long-prompt threshold is refused before it is sent.

Past the threshold the provider bills higher rates that the price table does not model, so the
controller records the call as failed with its own kind and sends nothing.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rps.config import RouteConfig
from rps.ledger import PriceTable
from rps.providers import FakeWriter, Request

from harness import World, task_spec


def priced(limit=500):
    entry = {"sync": {"input": 1.0, "output": 2.0}, "batch": {"input": 1.0, "output": 2.0}}
    if limit is not None:
        entry["long_prompt_tokens"] = limit
    return PriceTable.from_dict({"date": "2026-11-07", "models": {"fake-model": entry}})


class Sized(FakeWriter):
    """A fake writer whose requests are estimated at the number of tokens the spec line names."""

    def prepare(self, calls):
        requests = super().prepare(calls)
        return [replace(request, est_input_tokens=int(call.spec.split()[-1])) for call, request in zip(calls, requests)]


def spec_task(sizes, **fields):
    task = task_spec(tuple(f"{n:03d}" for n in range(1, len(sizes) + 1)), **fields)
    lines = tuple(replace(line, spec=f"tokens {size}") for line, size in zip(task.lines, sizes))
    return replace(task, lines=lines)


def bench(tmp_path, limit=500):
    return World(tmp_path, writer_class=Sized, prices=priced(limit))


def test_a_call_over_the_threshold_is_refused_with_its_own_failure_kind_and_never_sent(tmp_path):
    world = bench(tmp_path)
    controller = world.controller()
    controller.add_task(spec_task([100, 501, 500]))
    controller.step()
    sent = world.provider.submitted_calls
    assert len(sent) == 2  # the 501-token call was not sent; the one at the threshold was
    [failed] = world.events_of("call_failed")
    p = failed.payload
    assert p["kind"] == "long_prompt" and p["retryable"] is False and p["billed"] is False and p["usd"] == 0.0
    assert "501 tokens" in p["error"] and "500-token long-prompt threshold of fake-model" in p["error"]
    call = controller.state.calls[p["call_id"]]
    assert call.status == "failed" and call.kind == "long_prompt" and call.est_usd == 0.0 and call.call_id not in sent
    assert controller.state.line_status(call.task_id, call.line, controller.config.max_attempts).outcome == "exhausted"


def test_the_refused_call_costs_nothing_and_is_not_tried_again(tmp_path):
    world = bench(tmp_path)
    controller = world.controller()
    controller.add_task(spec_task([900, 100]))
    world.drive(controller)
    assert len(world.events_of("call_prepared")) == 2  # one attempt each: the long call is not retried as a rebound
    assert controller.state.spend("fake-batch", "rebound").calls == 0
    assert len(world.shipped_rows()) == 1
    controller.step()
    assert len(world.events_of("call_failed")) == 1


def test_without_a_threshold_a_long_prompt_goes_through(tmp_path):
    world = bench(tmp_path, limit=None)
    controller = world.controller()
    controller.add_task(spec_task([900_000]))
    controller.step()
    assert len(world.provider.submitted_calls) == 1 and not world.events_of("call_failed")


@pytest.mark.parametrize(
    "provider, model, limit",
    [("anthropic", "claude-haiku-5-5", 100_000), ("openai", "gpt-6-luna", 272_000), ("openai", "gpt-6.1-sol", 272_000)],
)
def test_the_packaged_thresholds_apply_to_the_models_that_have_one(tmp_path, provider, model, limit):
    controller = World(tmp_path, prices=PriceTable.default()).controller()
    route = RouteConfig(provider=provider, model=model, kind="batch")

    def request(tokens):
        return Request(call_id="c", custom_id="c", payload={}, est_input_tokens=tokens, max_output_tokens=10)

    assert controller._long_prompt_problem(route, request(limit)) is None
    problem = controller._long_prompt_problem(route, request(limit + 1))
    assert problem is not None and f"{limit + 1:,} tokens" in problem and model in problem


@pytest.mark.parametrize(
    "provider, model", [("anthropic", "claude-sonnet-5-5"), ("gemini", "gemini-3.8-flash"), ("deepseek", "deepseek-flash")]
)
def test_a_model_whose_page_lists_no_long_prompt_tier_is_never_refused_for_length(tmp_path, provider, model):
    controller = World(tmp_path, prices=PriceTable.default()).controller()
    route = RouteConfig(provider=provider, model=model, kind="api")
    request = Request(call_id="c", custom_id="c", payload={}, est_input_tokens=5_000_000, max_output_tokens=10)
    assert controller._long_prompt_problem(route, request) is None
