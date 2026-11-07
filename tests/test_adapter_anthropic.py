"""The Anthropic writers: request building, result parsing, usage, and the batch and one-call flows.

The fixtures are the SDK's own types, built from recorded-response-shaped data, so a change in the
SDK's field names fails here and not in a run.
"""

from __future__ import annotations

import json
import typing

import pytest
from anthropic.types import Message
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse

from rps import ids
from rps.config import ConfigError
from rps.ledger import Usage
from rps.providers import AnthropicBatchWriter, AnthropicSyncWriter, Handle, RouteRejected
from rps.providers.anthropic_api import parse_batch_item, parse_message, usage_of
from rps.routing import ImageRoutingError

from adapter_helpers import ANSWER, ApiError, Namespace, Recorder, SCHEMA, call, route, rules

MODEL = "claude-haiku-5-5"


def make_route(kind="batch", **overrides):
    return route("anthropic", MODEL, kind, **overrides)


def make_call(line="001", **overrides):
    return call(line, provider="anthropic", model=MODEL, **overrides)


def message(text=ANSWER, stop="end_turn", usage=None, content=None):
    return Message.model_validate(
        {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": content if content is not None else [{"type": "text", "text": text}],
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage or {"input_tokens": 100, "output_tokens": 50},
        }
    )


def batch(status="in_progress", batch_id="msgbatch_01"):
    return MessageBatch.model_validate(
        {
            "id": batch_id,
            "type": "message_batch",
            "processing_status": status,
            "request_counts": {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0},
            "created_at": "2026-11-07T10:00:00Z",
            "expires_at": "2026-11-08T10:00:00Z",
            "ended_at": None,
            "archived_at": None,
            "cancel_initiated_at": None,
            "results_url": None,
        }
    )


def item(call_id, result):
    return MessageBatchIndividualResponse.model_validate({"custom_id": ids.custom_id(call_id), "result": result})


def succeeded(call_id, text=ANSWER, stop="end_turn", usage=None):
    return item(call_id, {"type": "succeeded", "message": message(text, stop, usage).model_dump()})


def errored(call_id, error_type, text="trouble"):
    return item(
        call_id,
        {"type": "errored", "error": {"type": "error", "request_id": "req_1", "error": {"type": error_type, "message": text}}},
    )


# ---------------------------------------------------------------------------- request building


def test_a_request_holds_the_fields_the_specification_names_and_nothing_it_forbids():
    writer = AnthropicBatchWriter("haiku-batch", make_route())
    [request] = writer.prepare([make_call()])
    params = request.payload["params"]
    assert set(params) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert set(params) <= set(typing.get_type_hints(MessageCreateParamsNonStreaming))  # real SDK parameter names
    assert request.payload["custom_id"] == request.custom_id == ids.custom_id(request.call_id)
    assert params["model"] == MODEL and params["max_tokens"] == 2000
    for forbidden in ("temperature", "top_p", "top_k", "tool_choice", "tools", "stop_sequences", "thinking"):
        assert forbidden not in params
    assert [m["role"] for m in params["messages"]] == ["user"]  # no prefill
    assert params["messages"][0]["content"] == [{"type": "text", "text": "Write the row for 001."}]


def test_the_rules_go_in_the_system_block_with_a_cache_mark_and_the_schema_in_the_output_config():
    writer = AnthropicBatchWriter("haiku-batch", make_route())
    params = writer.prepare([make_call()])[0].payload["params"]
    assert params["system"] == [
        {"type": "text", "text": rules().system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}
    ]
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"}
    five = AnthropicBatchWriter("haiku-batch", make_route(options={"cache_ttl": "5m"}))
    assert five.prepare([make_call()])[0].payload["params"]["system"][0]["cache_control"]["ttl"] == "5m"


def test_without_an_effort_none_is_sent():
    writer = AnthropicBatchWriter("haiku-batch", make_route(effort=None))
    params = writer.prepare([make_call(effort=None)])[0].payload["params"]
    assert "effort" not in params["output_config"]


def test_a_call_that_carries_a_picture_is_refused_whatever_its_class():
    writer = AnthropicBatchWriter("haiku-batch", make_route())
    for image_class in ("public_common", "coco", "ffhq", "deepfashion", None):
        with pytest.raises(ImageRoutingError):
            writer.prepare([make_call(image=b"\xff\xd8bytes", image_ext="jpg", image_class=image_class)])


def test_the_estimate_and_size_are_filled_in_for_every_request():
    writer = AnthropicBatchWriter("haiku-batch", make_route())
    requests = writer.prepare([make_call("001"), make_call("002", attempt=2)])
    assert all(r.est_input_tokens > 0 and r.size_bytes > 0 for r in requests)
    assert [r.attempt for r in requests] == [1, 2]
    assert requests[0].size_bytes == len(json.dumps(requests[0].payload, sort_keys=True, separators=(",", ":")).encode()) + 1


def test_the_route_and_the_rules_are_checked_before_a_run():
    with pytest.raises(ConfigError, match="effort"):
        AnthropicBatchWriter("r", make_route(effort="turbo"))
    with pytest.raises(ConfigError, match="unknown option"):
        AnthropicBatchWriter("r", make_route(options={"cashe_ttl": "1h"}))
    with pytest.raises(ConfigError, match="cache_ttl"):
        AnthropicBatchWriter("r", make_route(options={"cache_ttl": "2h"}))
    writer = AnthropicBatchWriter("r", make_route())
    writer.check_rules(rules())
    open_schema = {"type": "object", "properties": {"id": {"type": "string"}}}
    with pytest.raises(ConfigError, match="additionalProperties"):
        writer.check_rules(rules(schema=open_schema))


def test_the_pools_row_schema_is_accepted():
    from rps import rules as pool

    AnthropicBatchWriter("r", make_route()).check_rules(rules(schema=pool.row_schema()))


def test_a_route_takes_calls_only_with_its_key_and_while_switched_on(monkeypatch):
    writer = AnthropicBatchWriter("r", make_route())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert not writer.accepting().ok and "ANTHROPIC_API_KEY" in writer.accepting().reason
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert writer.accepting().ok
    off = AnthropicBatchWriter("r", make_route(enabled=False))
    assert not off.accepting().ok


# ---------------------------------------------------------------------------- results and usage


def test_a_finished_message_gives_its_text_and_disjoint_usage_buckets():
    usage = {
        "input_tokens": 120,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 400,
        "output_tokens": 500,
        "output_tokens_details": {"thinking_tokens": 200},
        "cache_creation": {"ephemeral_1h_input_tokens": 30, "ephemeral_5m_input_tokens": 0},
    }
    result = parse_message("c1", message(usage=usage))
    assert result.output == ANSWER and result.error is None and result.stop_reason == "complete"
    assert result.usage == Usage(input_tokens=120, cache_write_tokens=30, cache_read_tokens=400, output_tokens=300, reasoning_tokens=200)


def test_usage_without_the_optional_fields_still_counts():
    assert usage_of(message().usage) == Usage(input_tokens=100, output_tokens=50)


def test_the_text_blocks_are_joined_and_thinking_blocks_are_left_out():
    content = [
        {"type": "thinking", "thinking": "hmm", "signature": "sig"},
        {"type": "text", "text": '{"skip": false,'},
        {"type": "text", "text": ' "reason": "", "rows": []}'},
    ]
    result = parse_message("c1", message(content=content))
    assert json.loads(result.output) == {"skip": False, "reason": "", "rows": []}


def test_a_message_cut_off_at_the_token_limit_is_a_retryable_truncation_that_keeps_its_usage():
    result = parse_message("c1", message(text='{"skip": fal', stop="max_tokens"), max_tokens=2000)
    assert result.output is None and result.kind == "truncated" and result.retryable is True
    assert "2000" in result.error and result.usage.output_tokens == 50


def test_a_refusal_is_recorded_and_not_retried():
    result = parse_message("c1", message(text="I can't help with that.", stop="refusal"))
    assert result.kind == "refused" and result.retryable is False and "can't help" in result.error


@pytest.mark.parametrize("stop", ["tool_use", "pause_turn", "model_context_window_exceeded"])
def test_any_other_stop_reason_fails_the_call_without_a_retry(stop):
    result = parse_message("c1", message(stop=stop))
    assert result.kind == "unexpected" and result.retryable is False and stop in result.error


def test_blank_text_is_a_failed_parse_that_is_retried():
    result = parse_message("c1", message(text="  \n"))
    assert result.kind == "invalid_output" and result.retryable is True


def test_batch_items_of_every_kind_become_call_results():
    c1, c2, c3, c4, c5, c6 = (make_call(f"00{n}").call_id for n in range(1, 7))
    ok = parse_batch_item(succeeded(c1), c1)
    assert ok.output == ANSWER
    bad = parse_batch_item(errored(c2, "invalid_request_error", "schema is wrong"), c2)
    assert bad.kind == "bad_request" and bad.retryable is False and "schema is wrong" in bad.error
    key = parse_batch_item(errored(c3, "authentication_error"), c3)
    assert key.kind == "auth" and key.retryable is False
    busy = parse_batch_item(errored(c4, "overloaded_error"), c4)
    assert busy.kind == "provider_error" and busy.retryable is True
    gone = parse_batch_item(item(c5, {"type": "expired"}), c5)
    assert gone.kind == "expired" and gone.retryable is True
    stopped = parse_batch_item(item(c6, {"type": "canceled"}), c6)
    assert stopped.kind == "canceled" and stopped.retryable is True


# ---------------------------------------------------------------------------- the batch flow


def fake_client(**sections):
    batches = Namespace(create=Recorder(batch()), retrieve=Recorder(batch()), results=Recorder([]))
    batches.__dict__.update(sections)
    return Namespace(messages=Namespace(batches=batches, create=Recorder(message())))


def test_submit_sends_the_prepared_requests_and_returns_the_batch_id():
    client = fake_client()
    writer = AnthropicBatchWriter("haiku-batch", make_route(), client=client)
    requests = writer.prepare([make_call("001"), make_call("002")])
    handle = writer.submit(requests)
    assert handle.handle_id == "msgbatch_01" and handle.mode == "batch" and handle.data == {"batch_id": "msgbatch_01"}
    assert handle.call_ids == tuple(r.call_id for r in requests)
    sent = client.messages.batches.create.calls[0]["requests"]
    assert [entry["custom_id"] for entry in sent] == [r.custom_id for r in requests]
    assert Handle.from_dict(json.loads(json.dumps(handle.to_dict()))) == handle  # plain data


@pytest.mark.parametrize("status, kind", [(400, "bad_request"), (401, "auth"), (403, "auth")])
def test_a_rejected_submit_stops_the_route(status, kind):
    client = fake_client(create=Recorder(ApiError(status, "no")))
    writer = AnthropicBatchWriter("haiku-batch", make_route(), client=client)
    with pytest.raises(RouteRejected) as raised:
        writer.submit(writer.prepare([make_call()]))
    assert raised.value.kind == kind


def test_a_busy_provider_at_submit_is_an_ordinary_error_the_controller_retries():
    client = fake_client(create=Recorder(ApiError(529, "overloaded")))
    writer = AnthropicBatchWriter("haiku-batch", make_route(), client=client)
    with pytest.raises(ApiError):
        writer.submit(writer.prepare([make_call()]))


def test_poll_maps_the_processing_status():
    client = fake_client(retrieve=Recorder(batch("in_progress")))
    writer = AnthropicBatchWriter("haiku-batch", make_route(), client=client)
    handle = Handle("msgbatch_01", ("c1",), data={"batch_id": "msgbatch_01"})
    assert writer.poll(handle).state == "in_progress"
    client.messages.batches.retrieve.result = batch("canceling")
    assert writer.poll(handle).state == "in_progress"
    client.messages.batches.retrieve.result = batch("ended")
    assert writer.poll(handle).state == "ended"
    client.messages.batches.retrieve.result = ApiError(404)
    assert writer.poll(handle).state == "failed"
    client.messages.batches.retrieve.result = ApiError(500)
    with pytest.raises(ApiError):
        writer.poll(handle)


def test_results_are_matched_by_custom_id_whatever_their_order_and_unknown_ids_are_ignored():
    first, second, third = (make_call(line) for line in ("001", "002", "003"))
    stray = succeeded(ids.call_id(ids.task_id("other", "9", {"x": "y"}), "001", 1))
    shuffled = [succeeded(third.call_id), stray, errored(first.call_id, "api_error"), succeeded(second.call_id)]
    client = fake_client(results=Recorder(shuffled))
    writer = AnthropicBatchWriter("haiku-batch", make_route(), client=client)
    handle = Handle("msgbatch_01", (first.call_id, second.call_id, third.call_id), data={"batch_id": "msgbatch_01"})
    results = {result.call_id: result for result in writer.results(handle)}
    assert set(results) == {first.call_id, second.call_id, third.call_id}
    assert results[second.call_id].output == ANSWER and results[third.call_id].output == ANSWER
    assert results[first.call_id].kind == "provider_error"
    assert client.messages.batches.results.calls[0]["args"] == ("msgbatch_01",)


def test_batches_are_split_by_count_and_by_size():
    writer = AnthropicBatchWriter("haiku-batch", make_route(max_batch_calls=2))
    requests = writer.prepare([make_call(line) for line in ("001", "002", "003")])
    assert [len(group) for group in writer.split(requests)] == [2, 1]
    big = AnthropicBatchWriter("haiku-batch", make_route(max_batch_calls=10))
    requests = big.prepare([make_call(line) for line in ("001", "002")])
    from dataclasses import replace

    huge = [replace(r, size_bytes=200 * 1024 * 1024) for r in requests]
    assert [len(group) for group in big.split(huge)] == [1, 1]


def test_a_batch_that_was_sent_just_before_a_crash_cannot_be_found_again():
    writer = AnthropicBatchWriter("haiku-batch", make_route(), client=fake_client())
    assert writer.recover(writer.prepare([make_call()])) is None


# ---------------------------------------------------------------------------- the one-call flow


def test_the_sync_writer_sends_one_create_call_per_request_with_an_explicit_timeout():
    client = fake_client()
    writer = AnthropicSyncWriter("haiku-sync", make_route("api"), client=client)
    requests = writer.prepare([make_call("001", kind="api")])
    result = writer.send(requests[0])
    sent = client.messages.create.calls[0]
    assert sent["timeout"] == 600.0 and sent["model"] == MODEL and "custom_id" not in sent
    assert result.output == ANSWER and result.usage == Usage(input_tokens=100, output_tokens=50)


def test_the_sync_writer_turns_an_error_into_a_failed_result_of_the_right_kind():
    for status, kind, retryable in [(429, "provider_error", True), (500, "provider_error", True), (401, "auth", False), (400, "bad_request", False)]:
        writer = AnthropicSyncWriter("haiku-sync", make_route("api"), client=Namespace(messages=Namespace(create=Recorder(ApiError(status)))))
        result = writer.send(writer.prepare([make_call(kind="api")])[0])
        assert (result.kind, result.retryable, result.output) == (kind, retryable, None)


def test_the_sync_writer_runs_a_handle_to_the_end_through_the_pool():
    import time

    client = fake_client()
    writer = AnthropicSyncWriter("haiku-sync", make_route("api", workers=2), client=client)
    handle = writer.submit(writer.prepare([make_call("001", kind="api"), make_call("002", kind="api")]))
    assert client.messages.create.calls == []  # nothing is sent until the first poll
    deadline = time.monotonic() + 10
    while writer.poll(handle).state != "ended":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert sorted(r.call_id for r in writer.results(handle)) == sorted(handle.call_ids)
    writer.release(handle)
    assert writer.poll(handle).state == "failed"
    writer.close()


# ---------------------------------------------------------------------------- the client


def test_the_client_is_built_from_the_environment_key_with_the_routes_retries_and_timeout(monkeypatch):
    from rps.providers import RouteUnavailable

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RouteUnavailable, match="ANTHROPIC_API_KEY"):
        AnthropicBatchWriter("r", make_route())._client()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    client = AnthropicBatchWriter("r", make_route(max_retries=4, timeout_seconds=90))._client()
    assert client.max_retries == 4 and client.timeout == 90
