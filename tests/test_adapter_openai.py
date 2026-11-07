"""The OpenAI writers: request building, result parsing, usage, and the batch and one-call flows.

The fixtures are the SDK's own types, built from recorded-response-shaped data.
"""

from __future__ import annotations

import base64
import inspect
import json

import pytest
from openai.resources.responses import Responses
from openai.types import Batch
from openai.types.responses import Response

from rps import ids
from rps.config import ConfigError
from rps.ledger import Usage
from rps.providers import Handle, OpenAIBatchWriter, OpenAISyncWriter, RouteRejected
from rps.providers.openai_api import digest_of, parse_batch_line, parse_response, usage_of
from rps.routing import ImageRoutingError

from adapter_helpers import ANSWER, ApiError, Namespace, Recorder, SCHEMA, call, route, rules

MODEL = "gpt-6-luna"
PICTURE = b"\xff\xd8\xff\xe0 pretend jpeg bytes"


def make_route(kind="batch", **overrides):
    return route("openai", MODEL, kind, **overrides)


def make_call(line="001", **overrides):
    return call(line, provider="openai", model=MODEL, **overrides)


def response_data(text=ANSWER, status="completed", usage=None, output=None, incomplete=None, error=None):
    return {
        "id": "resp_01",
        "object": "response",
        "created_at": 1_800_000_000.0,
        "model": MODEL,
        "status": status,
        "output": output
        if output is not None
        else [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "error": error,
        "incomplete_details": incomplete,
        "instructions": None,
        "metadata": None,
        "temperature": None,
        "top_p": None,
        "usage": usage
        or {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 150,
        },
    }


def response(**kwargs):
    return Response.model_validate(response_data(**kwargs))


def batch(status="in_progress", output="file_out", error="file_err", metadata=None, batch_id="batch_01"):
    return Batch.model_validate(
        {
            "id": batch_id,
            "object": "batch",
            "endpoint": "/v1/responses",
            "completion_window": "24h",
            "created_at": 1_800_000_000,
            "input_file_id": "file_in",
            "status": status,
            "output_file_id": output,
            "error_file_id": error,
            "request_counts": {"total": 2, "completed": 1, "failed": 1},
            "metadata": metadata,
            "errors": None,
        }
    )


def line_ok(call_id, **kwargs):
    return {
        "id": "batch_req_1",
        "custom_id": ids.custom_id(call_id),
        "response": {"status_code": 200, "request_id": "req_1", "body": response_data(**kwargs)},
        "error": None,
    }


def line_status(call_id, status_code, code="invalid_request_error", message="bad"):
    return {
        "id": "batch_req_2",
        "custom_id": ids.custom_id(call_id),
        "response": {"status_code": status_code, "request_id": "req_2", "body": {"error": {"code": code, "message": message}}},
        "error": None,
    }


def line_error(call_id, code, message="expired"):
    return {"id": "batch_req_3", "custom_id": ids.custom_id(call_id), "response": None, "error": {"code": code, "message": message}}


class FileContent:
    def __init__(self, lines):
        self.text = "".join(json.dumps(line) + "\n" for line in lines)


# ---------------------------------------------------------------------------- request building


def test_a_request_line_holds_the_fields_the_specification_names():
    writer = OpenAIBatchWriter("luna-batch", make_route())
    [request] = writer.prepare([make_call()])
    line = request.payload
    assert set(line) == {"custom_id", "method", "url", "body"}
    assert (line["method"], line["url"]) == ("POST", "/v1/responses") and line["custom_id"] == ids.custom_id(request.call_id)
    body = line["body"]
    assert set(body) == {"model", "instructions", "input", "text", "max_output_tokens", "store", "prompt_cache_key", "reasoning"}
    assert set(body) <= set(inspect.signature(Responses.create).parameters)  # real SDK parameter names
    assert body["model"] == MODEL and body["instructions"] == rules().system and body["max_output_tokens"] == 2000
    assert body["store"] is False and body["prompt_cache_key"] == "rules-v1" and body["reasoning"] == {"effort": "medium"}
    assert body["text"] == {"format": {"type": "json_schema", "name": "row", "schema": SCHEMA, "strict": True}}
    assert body["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Write the row for 001."}]}]
    for forbidden in ("temperature", "top_p", "tools", "tool_choice", "previous_response_id"):
        assert forbidden not in body


def test_without_an_effort_no_reasoning_setting_is_sent():
    writer = OpenAIBatchWriter("luna-batch", make_route(effort=None))
    assert "reasoning" not in writer.prepare([make_call(effort=None)])[0].payload["body"]


def test_a_picture_goes_in_as_a_data_url_with_the_configured_detail_when_its_class_is_allowed():
    for image_class in ("public_common", "coco", "ffhq"):
        writer = OpenAIBatchWriter("luna-batch", make_route(options={"detail": "high"}))
        [request] = writer.prepare([make_call(image=PICTURE, image_ext="jpg", image_class=image_class)])
        content = request.payload["body"]["input"][0]["content"]
        assert [part["type"] for part in content] == ["input_text", "input_image"]
        expected = "data:image/jpeg;base64," + base64.b64encode(PICTURE).decode()
        assert content[1] == {"type": "input_image", "image_url": expected, "detail": "high"}
    plain = OpenAIBatchWriter("luna-batch", make_route())
    [with_picture] = plain.prepare([make_call(image=PICTURE, image_ext="jpg", image_class="coco")])
    [without] = plain.prepare([make_call()])
    assert with_picture.est_input_tokens > without.est_input_tokens


@pytest.mark.parametrize("image_class", ["deepfashion", "hico", "synthetic", "mystery", None])
def test_a_picture_of_a_class_the_table_does_not_allow_never_reaches_the_request(image_class):
    writer = OpenAIBatchWriter("luna-batch", make_route())
    with pytest.raises(ImageRoutingError):
        writer.prepare([make_call(image=PICTURE, image_ext="jpg", image_class=image_class)])


def test_the_size_of_each_line_is_measured_for_the_file_limit():
    writer = OpenAIBatchWriter("luna-batch", make_route())
    [request] = writer.prepare([make_call()])
    assert request.size_bytes == len(json.dumps(request.payload, ensure_ascii=False).encode()) + 1


def test_the_route_and_the_rules_are_checked_before_a_run():
    with pytest.raises(ConfigError, match="unknown option"):
        OpenAIBatchWriter("r", make_route(options={"detial": "low"}))
    with pytest.raises(ConfigError, match="detail"):
        OpenAIBatchWriter("r", make_route(options={"detail": "ultra"}))
    writer = OpenAIBatchWriter("r", make_route())
    writer.check_rules(rules())
    lax = dict(SCHEMA, required=["skip"])
    with pytest.raises(ConfigError, match="required"):
        writer.check_rules(rules(schema=lax))


def test_the_pools_row_schema_is_accepted_in_strict_mode():
    from rps import rules as pool

    OpenAIBatchWriter("r", make_route()).check_rules(rules(schema=pool.row_schema()))


def test_a_route_takes_calls_only_with_its_key_and_while_switched_on(monkeypatch):
    writer = OpenAIBatchWriter("r", make_route())
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert not writer.accepting().ok and "OPENAI_API_KEY" in writer.accepting().reason
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert writer.accepting().ok
    assert not OpenAIBatchWriter("r", make_route(enabled=False)).accepting().ok


# ---------------------------------------------------------------------------- results and usage


def test_usage_is_split_into_disjoint_buckets():
    usage = {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
        "output_tokens": 500,
        "output_tokens_details": {"reasoning_tokens": 200},
        "total_tokens": 1500,
    }
    result = parse_response("c1", response(usage=usage))
    assert result.usage == Usage(input_tokens=300, cache_write_tokens=100, cache_read_tokens=600, output_tokens=300, reasoning_tokens=200)
    assert usage_of(response().usage) == Usage(input_tokens=100, output_tokens=50)


def test_a_completed_response_gives_its_text():
    result = parse_response("c1", response())
    assert result.output == ANSWER and result.error is None and result.stop_reason == "complete"


def test_the_output_text_parts_of_the_message_are_joined_and_other_items_are_skipped():
    output = [
        {"id": "rs_1", "type": "reasoning", "summary": []},
        {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": '{"skip": false,', "annotations": []},
            {"type": "output_text", "text": ' "reason": "", "rows": []}', "annotations": []},
        ]},
    ]
    result = parse_response("c1", response(output=output))
    assert json.loads(result.output) == {"skip": False, "reason": "", "rows": []}


def test_an_incomplete_response_cut_off_at_the_limit_is_a_retryable_truncation():
    result = parse_response("c1", response(status="incomplete", output=[], incomplete={"reason": "max_output_tokens"}), max_tokens=2000)
    assert result.kind == "truncated" and result.retryable is True and "2000" in result.error and result.usage.output_tokens == 50


def test_a_content_filter_stop_is_a_block_that_is_not_retried():
    result = parse_response("c1", response(status="incomplete", output=[], incomplete={"reason": "content_filter"}))
    assert result.kind == "blocked" and result.retryable is False


def test_a_refusal_is_recorded_and_not_retried():
    output = [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "I can't help."}]}]
    result = parse_response("c1", response(output=output))
    assert result.kind == "refused" and result.retryable is False and "can't help" in result.error


def test_a_failed_response_is_retried_unless_the_prompt_itself_was_refused():
    server = parse_response("c1", response(status="failed", output=[], error={"code": "server_error", "message": "oops"}))
    assert server.kind == "provider_error" and server.retryable is True
    flagged = parse_response("c1", response(status="failed", output=[], error={"code": "invalid_prompt", "message": "flagged"}))
    assert flagged.kind == "blocked" and flagged.retryable is False


def test_blank_text_is_a_failed_parse_that_is_retried():
    result = parse_response("c1", response(text=" "))
    assert result.kind == "invalid_output" and result.retryable is True


def test_batch_lines_of_every_kind_become_call_results():
    c = {n: make_call(f"00{n}").call_id for n in range(1, 8)}
    assert parse_batch_line(line_ok(c[1]), c[1]).output == ANSWER
    bad = parse_batch_line(line_status(c[2], 400, message="schema is wrong"), c[2])
    assert bad.kind == "bad_request" and bad.retryable is False and "schema is wrong" in bad.error
    key = parse_batch_line(line_status(c[3], 401, "invalid_api_key"), c[3])
    assert key.kind == "auth" and key.retryable is False
    quota = parse_batch_line(line_status(c[4], 429, "insufficient_quota"), c[4])
    assert quota.kind == "auth" and quota.retryable is False
    busy = parse_batch_line(line_status(c[5], 503, "overloaded"), c[5])
    assert busy.kind == "provider_error" and busy.retryable is True
    gone = parse_batch_line(line_error(c[6], "batch_expired"), c[6])
    assert gone.kind == "expired" and gone.retryable is True
    stopped = parse_batch_line(line_error(c[7], "batch_cancelled"), c[7])
    assert stopped.kind == "canceled" and stopped.retryable is True


# ---------------------------------------------------------------------------- the batch flow


def fake_client(**overrides):
    files = Namespace(create=Recorder(Namespace(id="file_in")), content=Recorder(FileContent([])), delete=Recorder(None))
    batches = Namespace(create=Recorder(batch()), retrieve=Recorder(batch()), list=Recorder([]))
    client = Namespace(files=files, batches=batches, responses=Namespace(create=Recorder(response())))
    for path, value in overrides.items():
        section, name = path.split("__")
        getattr(client, section).__dict__[name] = value
    return client


def test_submit_uploads_the_lines_in_order_then_creates_the_batch():
    client = fake_client()
    writer = OpenAIBatchWriter("luna-batch", make_route(options={"file_expiry_days": 2}), client=client)
    requests = writer.prepare([make_call("001"), make_call("002")])
    handle = writer.submit(requests)
    upload = client.files.create.calls[0]
    name, content = upload["file"]
    assert name.endswith(".jsonl") and upload["purpose"] == "batch"
    assert upload["expires_after"] == {"anchor": "created_at", "seconds": 2 * 24 * 3600}
    sent = [json.loads(line) for line in content.decode().splitlines()]
    assert [entry["custom_id"] for entry in sent] == [r.custom_id for r in requests]
    created = client.batches.create.calls[0]
    assert created["input_file_id"] == "file_in" and created["endpoint"] == "/v1/responses" and created["completion_window"] == "24h"
    assert created["metadata"]["rps_digest"] == digest_of([r.custom_id for r in requests])
    assert handle.handle_id == "batch_01" and handle.mode == "batch"
    assert handle.data == {"batch_id": "batch_01", "input_file_id": "file_in"}


def test_the_schema_property_order_survives_into_the_uploaded_file():
    client = fake_client()
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["zeta", "alpha"],
        "properties": {"zeta": {"type": "string"}, "alpha": {"type": "string"}},
    }
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=client)
    writer.submit(writer.prepare([make_call(rules=rules(schema=schema))]))
    sent = json.loads(client.files.create.calls[0]["file"][1].decode().splitlines()[0])
    assert list(sent["body"]["text"]["format"]["schema"]["properties"]) == ["zeta", "alpha"]


def test_the_uploaded_file_is_deleted_when_the_batch_cannot_be_created():
    client = fake_client(batches__create=Recorder(ApiError(500)))
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=client)
    with pytest.raises(ApiError):
        writer.submit(writer.prepare([make_call()]))
    assert client.files.delete.calls == [{"args": ("file_in",)}]


@pytest.mark.parametrize("status, kind", [(400, "bad_request"), (401, "auth"), (403, "auth")])
def test_a_rejected_submit_stops_the_route(status, kind):
    client = fake_client(files__create=Recorder(ApiError(status)))
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=client)
    with pytest.raises(RouteRejected) as raised:
        writer.submit(writer.prepare([make_call()]))
    assert raised.value.kind == kind


@pytest.mark.parametrize(
    "status, files, expected",
    [
        ("validating", True, "in_progress"),
        ("in_progress", True, "in_progress"),
        ("finalizing", True, "in_progress"),
        ("cancelling", True, "in_progress"),
        ("completed", True, "ended"),
        ("expired", True, "ended"),
        ("expired", False, "expired"),
        ("cancelled", True, "canceled"),
        ("failed", False, "failed"),
    ],
)
def test_poll_maps_the_batch_status(status, files, expected):
    found = batch(status, output="file_out" if files else None, error=None)
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=fake_client(batches__retrieve=Recorder(found)))
    assert writer.poll(Handle("batch_01", ("c1",), data={"batch_id": "batch_01"})).state == expected


def test_poll_treats_an_unknown_batch_as_failed_and_passes_other_errors_up():
    handle = Handle("batch_01", ("c1",), data={"batch_id": "batch_01"})
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=fake_client(batches__retrieve=Recorder(ApiError(404))))
    assert writer.poll(handle).state == "failed"
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=fake_client(batches__retrieve=Recorder(ApiError(500))))
    with pytest.raises(ApiError):
        writer.poll(handle)


def test_results_come_from_both_files_matched_by_custom_id_in_any_order():
    a, b, c = (make_call(line).call_id for line in ("001", "002", "003"))
    stray = ids.call_id(ids.task_id("other", "9", {"x": "y"}), "001", 1)
    files = {"file_out": FileContent([line_ok(c), line_ok(stray), line_ok(a)]), "file_err": FileContent([line_error(b, "batch_expired")])}
    client = fake_client(files__content=Recorder(lambda file_id: files[file_id]), batches__retrieve=Recorder(batch("expired")))
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=client)
    handle = Handle("batch_01", (a, b, c), data={"batch_id": "batch_01"})
    results = {result.call_id: result for result in writer.results(handle)}
    assert set(results) == {a, b, c}
    assert results[a].output == ANSWER and results[c].output == ANSWER and results[b].kind == "expired"


def test_release_deletes_the_input_output_and_error_files_and_survives_a_missing_one():
    client = fake_client(batches__retrieve=Recorder(batch("completed")))
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=client)
    client.files.delete.result = lambda file_id: (_ for _ in ()).throw(ApiError(404)) if file_id == "file_err" else None
    writer.release(Handle("batch_01", ("c1",), data={"batch_id": "batch_01", "input_file_id": "file_in"}))
    assert [call["args"][0] for call in client.files.delete.calls] == ["file_in", "file_out", "file_err"]


def test_release_still_deletes_the_input_when_the_batch_cannot_be_read():
    client = fake_client(batches__retrieve=Recorder(ApiError(500)))
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=client)
    writer.release(Handle("batch_01", ("c1",), data={"batch_id": "batch_01", "input_file_id": "file_in"}))
    assert [call["args"][0] for call in client.files.delete.calls] == ["file_in"]


def test_a_batch_created_just_before_a_crash_is_found_again_by_its_digest():
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=fake_client())
    requests = writer.prepare([make_call("001"), make_call("002")])
    mine = batch("in_progress", metadata={"rps_digest": digest_of([r.custom_id for r in requests])})
    other = batch("in_progress", metadata={"rps_digest": "somebody-else"}, batch_id="batch_other")
    failed = batch("failed", metadata={"rps_digest": digest_of([r.custom_id for r in requests])}, batch_id="batch_failed")
    writer = OpenAIBatchWriter("luna-batch", make_route(), client=fake_client(batches__list=Recorder([other, failed, mine])))
    found = writer.recover(requests)
    assert found is not None and found.handle_id == "batch_01" and found.call_ids == tuple(r.call_id for r in requests)
    assert found.data["input_file_id"] == "file_in"
    assert writer.recover(requests[:1]) is None


def test_batches_are_split_by_count_and_by_size():
    writer = OpenAIBatchWriter("luna-batch", make_route(max_batch_calls=2))
    requests = writer.prepare([make_call(line) for line in ("001", "002", "003")])
    assert [len(group) for group in writer.split(requests)] == [2, 1]
    from dataclasses import replace

    huge = [replace(r, size_bytes=150 * 1024 * 1024) for r in requests]
    assert [len(group) for group in OpenAIBatchWriter("r", make_route(max_batch_calls=10)).split(huge)] == [1, 1, 1]


# ---------------------------------------------------------------------------- the one-call flow


def test_the_sync_writer_makes_one_responses_call_per_request():
    client = fake_client()
    writer = OpenAISyncWriter("luna-sync", make_route("api"), client=client)
    [request] = writer.prepare([make_call(kind="api")])
    result = writer.send(request)
    sent = client.responses.create.calls[0]
    assert sent["model"] == MODEL and sent["store"] is False and "custom_id" not in sent
    assert result.output == ANSWER and result.usage == Usage(input_tokens=100, output_tokens=50)


def test_the_sync_writer_turns_an_error_into_a_failed_result_of_the_right_kind():
    for status, kind, retryable in [(429, "provider_error", True), (503, "provider_error", True), (401, "auth", False), (400, "bad_request", False)]:
        client = fake_client(responses__create=Recorder(ApiError(status)))
        writer = OpenAISyncWriter("luna-sync", make_route("api"), client=client)
        result = writer.send(writer.prepare([make_call(kind="api")])[0])
        assert (result.kind, result.retryable, result.output) == (kind, retryable, None)


# ---------------------------------------------------------------------------- the client


def test_the_client_is_built_from_the_environment_key_with_the_routes_retries_and_timeout(monkeypatch):
    from rps.providers import RouteUnavailable

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RouteUnavailable, match="OPENAI_API_KEY"):
        OpenAIBatchWriter("r", make_route())._client()
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    client = OpenAIBatchWriter("r", make_route(max_retries=4, timeout_seconds=90))._client()
    assert client.max_retries == 4 and client.timeout == 90
