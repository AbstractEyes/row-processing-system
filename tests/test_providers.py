import json

import pytest

from rps import ids
from rps.config import ConfigError, RouteConfig
from rps.ledger import Usage
from rps.providers import (
    AnthropicWriter,
    CallSpec,
    FakeOutcome,
    FakeProvider,
    FakeWriter,
    GeminiWriter,
    Handle,
    OpenAIWriter,
    SimulatedCrash,
    make_writer,
)

TASK = ids.task_id("photos", "1", {"001": "a", "002": "b"})


def spec(line="001", attempt=1, **overrides):
    fields = dict(
        call_id=ids.call_id(TASK, line, attempt), task_id=TASK, line=line, attempt=attempt, route="r1",
        provider="fake", model="fake-model", kind="batch", effort="low", max_output_tokens=300,
        spec=f"spec for {line}", pool_version="1", purpose="production",
    )
    fields.update(overrides)
    return CallSpec(**fields)


def route(kind="batch", provider="fake"):
    return RouteConfig(provider=provider, model="fake-model", kind=kind, budget_usd=1)


def test_prepare_builds_one_request_per_call_with_provider_safe_ids():
    writer = FakeWriter("r1", route())
    requests = writer.prepare([spec("001"), spec("002", feedback=("bad thought",))])
    assert [r.call_id for r in requests] == [ids.call_id(TASK, "001", 1), ids.call_id(TASK, "002", 1)]
    assert requests[0].custom_id == f"{TASK}_001_1"
    assert requests[1].payload["feedback"] == ["bad thought"]
    assert all(r.max_output_tokens == 300 and r.est_input_tokens > 0 for r in requests)
    json.dumps([r.payload for r in requests])  # payloads are JSON data


def test_submit_poll_results_for_a_batch_route():
    writer = FakeWriter("r1", route(), polls_until_ended=2)
    requests = writer.prepare([spec("001"), spec("002")])
    handle = writer.submit(requests)
    assert handle.mode == "batch" and len(handle.call_ids) == 2
    assert [writer.poll(handle).state for _ in range(3)] == ["in_progress", "in_progress", "ended"]
    first = writer.results(handle)
    second = writer.results(handle)
    assert [r.call_id for r in first] == list(handle.call_ids)
    assert [r.output for r in first] == [r.output for r in second]
    row = json.loads(first[0].output)
    assert row["spec"] == "spec for 001"
    assert writer.provider.submitted_calls == list(handle.call_ids)


def test_a_per_call_route_is_finished_at_once():
    writer = FakeWriter("r1", route(kind="api"), polls_until_ended=5)
    handle = writer.submit(writer.prepare([spec("001", kind="api")]))
    assert handle.mode == "per_call"
    assert writer.poll(handle).state == "ended"


def test_behavior_can_inject_errors_usage_and_odd_outputs():
    def behavior(payload):
        if payload["line"] == "001":
            return FakeOutcome(error="overloaded", retryable=True)
        return FakeOutcome(output="{}", usage=Usage(input_tokens=5, output_tokens=7), minutes=1.5)

    writer = FakeWriter("r1", route(), behavior=behavior)
    handle = writer.submit(writer.prepare([spec("001"), spec("002")]))
    first, second = writer.results(handle)
    assert first.error == "overloaded" and first.output is None
    assert second.usage == Usage(input_tokens=5, output_tokens=7) and second.minutes == 1.5


def test_a_second_writer_on_the_same_provider_sees_earlier_batches():
    provider = FakeProvider()
    one = FakeWriter("r1", route(), provider=provider)
    handle = one.submit(one.prepare([spec("001")]))
    two = FakeWriter("r1", route(), provider=provider)
    assert two.poll(handle).state == "ended"
    assert [r.call_id for r in two.results(handle)] == list(handle.call_ids)
    assert provider.submit_count == 1


def test_recover_finds_a_batch_submitted_before_a_crash():
    provider = FakeProvider()
    provider.crash_after_submit = True
    one = FakeWriter("r1", route(), provider=provider)
    requests = one.prepare([spec("001"), spec("002")])
    with pytest.raises(SimulatedCrash):
        one.submit(requests)
    provider.crash_after_submit = False
    two = FakeWriter("r1", route(), provider=provider)
    found = two.recover(requests)
    assert found is not None and set(found.call_ids) == {r.call_id for r in requests}
    assert two.recover(two.prepare([spec("002", attempt=2)])) is None


def test_handles_round_trip_through_plain_data():
    handle = Handle("b1", ("c1", "c2"), mode="per_call", data={"cursor": 3})
    assert Handle.from_dict(json.loads(json.dumps(handle.to_dict()))) == handle


@pytest.mark.parametrize("writer_class", [AnthropicWriter, OpenAIWriter, GeminiWriter])
def test_the_real_adapters_are_stubs_that_say_so(writer_class):
    writer = writer_class("r1", route(provider="x"))
    with pytest.raises(NotImplementedError, match="stub"):
        writer.prepare([spec()])
    with pytest.raises(NotImplementedError, match="stub"):
        writer.submit([])
    with pytest.raises(NotImplementedError, match="stub"):
        writer.poll(Handle("h", ()))
    with pytest.raises(NotImplementedError, match="stub"):
        writer.results(Handle("h", ()))
    assert writer.recover([]) is None


def test_the_registry_builds_writers_by_provider():
    assert isinstance(make_writer("r", route(provider="fake")), FakeWriter)
    assert isinstance(make_writer("r", route(provider="anthropic")), AnthropicWriter)
    assert isinstance(make_writer("r", route(provider="openai")), OpenAIWriter)
    assert isinstance(make_writer("r", route(provider="gemini")), GeminiWriter)
    with pytest.raises(ConfigError):
        make_writer("r", route(provider="mystery"))
