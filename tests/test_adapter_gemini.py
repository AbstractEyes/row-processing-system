"""The Gemini writers: request building, result parsing, usage, the explicit cache, and the batch and one-call flows.

The fixtures are the SDK's own types, built from recorded-response-shaped data.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from google.genai import types

from rps import ids
from rps.config import ConfigError
from rps.ledger import Usage
from rps.providers import GeminiBatchWriter, GeminiSyncWriter, Handle, RouteRejected
from rps.providers.common import estimate_text_tokens
from rps.providers.gemini_api import digest_of, parse_inlined, parse_response, usage_of
from rps.routing import ImageRoutingError

from adapter_helpers import ANSWER, ApiError, Namespace, Recorder, SCHEMA, call, route, rules

MODEL = "gemini-3.8-flash"
PICTURE = b"\xff\xd8\xff\xe0 pretend jpeg bytes"
NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)


def make_route(kind="batch", **overrides):
    return route("gemini", MODEL, kind, **overrides)


def make_call(line="001", **overrides):
    return call(line, provider="gemini", model=MODEL, **overrides)


def generated(text=ANSWER, finish="STOP", usage=None, parts=None, feedback=None, candidates=True):
    data = {
        "candidates": [{"content": {"role": "model", "parts": parts if parts is not None else [{"text": text}]}, "finish_reason": finish}]
        if candidates
        else [],
        "usage_metadata": usage
        or {"prompt_token_count": 100, "candidates_token_count": 50, "total_token_count": 150},
    }
    if feedback:
        data["prompt_feedback"] = feedback
    return types.GenerateContentResponse.model_validate(data)


def inlined(call_id, response=None, error=None):
    return types.InlinedResponse(response=response, error=error, metadata={"custom_id": ids.custom_id(call_id)})


def job(state="JOB_STATE_RUNNING", responses=None, name="batches/job-1", display_name=None, error=None):
    return types.BatchJob.model_validate(
        {
            "name": name,
            "display_name": display_name,
            "state": state,
            "error": error,
            "dest": {"inlined_responses": [r.model_dump() for r in responses]} if responses is not None else None,
        }
    )


# ---------------------------------------------------------------------------- request building


def test_a_request_holds_the_fields_the_specification_names():
    writer = GeminiBatchWriter("flash-batch", make_route())
    [request] = writer.prepare([make_call()])
    inlined_request = request.payload["request"]
    assert isinstance(inlined_request, types.InlinedRequest)
    assert inlined_request.metadata == {"custom_id": ids.custom_id(request.call_id)}
    [content] = inlined_request.contents
    assert content.role == "user" and [part.text for part in content.parts] == ["Write the row for 001."]
    config = inlined_request.config
    assert config.response_mime_type == "application/json" and config.response_json_schema == SCHEMA
    assert config.system_instruction == rules().system and config.max_output_tokens == 2000
    assert config.thinking_config.thinking_level == types.ThinkingLevel.MEDIUM
    assert config.media_resolution is None and config.cached_content is None
    for forbidden in ("temperature", "top_p", "top_k", "tools", "safety_settings", "response_schema", "candidate_count", "seed"):
        assert getattr(config, forbidden) is None


def test_without_an_effort_no_thinking_setting_is_sent():
    writer = GeminiBatchWriter("flash-batch", make_route(effort=None))
    config = writer.prepare([make_call(effort=None)])[0].payload["request"].config
    assert config.thinking_config is None


def test_a_picture_goes_in_as_bytes_with_the_configured_resolution_when_its_class_is_allowed():
    for image_class in ("public_common", "coco", "ffhq"):
        writer = GeminiBatchWriter("flash-batch", make_route(options={"media_resolution": "high"}))
        [request] = writer.prepare([make_call(image=PICTURE, image_ext="jpg", image_class=image_class)])
        parts = request.payload["request"].contents[0].parts
        assert parts[1].inline_data.data == PICTURE and parts[1].inline_data.mime_type == "image/jpeg"
        assert request.payload["request"].config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
    plain = GeminiBatchWriter("flash-batch", make_route())
    [with_picture] = plain.prepare([make_call(image=PICTURE, image_ext="jpg", image_class="coco")])
    [without] = plain.prepare([make_call()])
    assert with_picture.est_input_tokens > without.est_input_tokens and with_picture.size_bytes > without.size_bytes


@pytest.mark.parametrize("image_class", ["deepfashion", "hico", "synthetic", "mystery", None])
def test_a_picture_of_a_class_the_table_does_not_allow_never_reaches_the_request(image_class):
    writer = GeminiBatchWriter("flash-batch", make_route())
    with pytest.raises(ImageRoutingError):
        writer.prepare([make_call(image=PICTURE, image_ext="jpg", image_class=image_class)])


def test_the_route_and_the_rules_are_checked_before_a_run():
    with pytest.raises(ConfigError, match="effort"):
        GeminiBatchWriter("r", make_route(effort="turbo"))
    with pytest.raises(ConfigError, match="unknown option"):
        GeminiBatchWriter("r", make_route(options={"chache": True}))
    with pytest.raises(ConfigError, match="media_resolution"):
        GeminiBatchWriter("r", make_route(options={"media_resolution": "huge"}))
    with pytest.raises(ConfigError, match="cache_ttl_seconds"):
        GeminiBatchWriter("r", make_route(options={"cache_ttl_seconds": 5}))
    writer = GeminiBatchWriter("r", make_route())
    writer.check_rules(rules())
    with pytest.raises(ConfigError, match="root"):
        writer.check_rules(rules(schema={"type": "array", "items": {}}))


def test_a_route_takes_calls_only_with_its_key_and_while_switched_on(monkeypatch):
    writer = GeminiBatchWriter("r", make_route())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert not writer.accepting().ok and "GEMINI_API_KEY" in writer.accepting().reason
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert writer.accepting().ok
    assert not GeminiBatchWriter("r", make_route(enabled=False)).accepting().ok


# ---------------------------------------------------------------------------- results and usage


def test_usage_counts_cached_tokens_apart_from_the_prompt_and_thinking_as_output():
    usage = {"prompt_token_count": 1000, "cached_content_token_count": 700, "candidates_token_count": 120, "thoughts_token_count": 80, "total_token_count": 1200}
    assert usage_of(generated(usage=usage).usage_metadata) == Usage(input_tokens=300, cache_read_tokens=700, output_tokens=120, reasoning_tokens=80)
    assert usage_of(None) == Usage()


def test_a_normal_finish_gives_the_text_without_the_thought_parts():
    parts = [{"text": "let me think", "thought": True}, {"text": '{"skip": false,'}, {"text": ' "reason": "", "rows": []}'}]
    result = parse_response("c1", generated(parts=parts))
    assert json.loads(result.output) == {"skip": False, "reason": "", "rows": []} and result.error is None


def test_a_response_cut_off_at_the_limit_is_a_retryable_truncation_that_keeps_its_usage():
    result = parse_response("c1", generated(text='{"sk', finish="MAX_TOKENS"), max_tokens=2000)
    assert result.kind == "truncated" and result.retryable is True and "2000" in result.error and result.usage.output_tokens == 50


@pytest.mark.parametrize("finish", ["SAFETY", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "BLOCKLIST", "RECITATION"])
def test_a_safety_stop_is_recorded_and_not_retried(finish):
    result = parse_response("c1", generated(text="", finish=finish))
    assert result.kind == "blocked" and result.retryable is False and finish in result.error


def test_a_blocked_prompt_has_no_candidates_and_a_reason():
    result = parse_response("c1", generated(candidates=False, feedback={"block_reason": "PROHIBITED_CONTENT"}))
    assert result.kind == "blocked" and "PROHIBITED_CONTENT" in result.error and result.retryable is False


def test_any_other_finish_reason_fails_the_call_without_a_retry():
    result = parse_response("c1", generated(finish="OTHER"))
    assert result.kind == "unexpected" and result.retryable is False
    nothing = parse_response("c1", generated(candidates=False))
    assert nothing.kind == "unexpected"


def test_blank_text_is_a_failed_parse_that_is_retried():
    result = parse_response("c1", generated(text="  "))
    assert result.kind == "invalid_output" and result.retryable is True


def test_inlined_responses_with_an_error_become_failed_results_of_the_right_kind():
    c1, c2, c3 = (make_call(line).call_id for line in ("001", "002", "003"))
    assert parse_inlined(inlined(c1, generated()), c1).output == ANSWER
    bad = parse_inlined(inlined(c2, error=types.JobError(code=400, message="bad schema")), c2)
    assert bad.kind == "bad_request" and bad.retryable is False and "bad schema" in bad.error
    busy = parse_inlined(inlined(c3, error=types.JobError(code=503, message="try later")), c3)
    assert busy.kind == "provider_error" and busy.retryable is True


# ---------------------------------------------------------------------------- the batch flow


def fake_client(**overrides):
    batches = Namespace(create=Recorder(job()), get=Recorder(job()), list=Recorder([]))
    caches = Namespace(create=Recorder(Namespace(name="cachedContents/c1", expire_time=NOW + timedelta(hours=6))), update=Recorder(None))
    client = Namespace(batches=batches, caches=caches, models=Namespace(generate_content=Recorder(generated())))
    for path, value in overrides.items():
        section, name = path.split("__")
        getattr(client, section).__dict__[name] = value
    return client


def test_submit_sends_the_inline_requests_with_a_digest_in_the_display_name():
    client = fake_client()
    writer = GeminiBatchWriter("flash-batch", make_route(), client=client)
    requests = writer.prepare([make_call("001"), make_call("002")])
    handle = writer.submit(requests)
    sent = client.batches.create.calls[0]
    assert sent["model"] == MODEL and len(sent["src"]) == 2
    assert [item.metadata["custom_id"] for item in sent["src"]] == [r.custom_id for r in requests]
    assert sent["config"].display_name == f"rps-{digest_of([r.custom_id for r in requests])}"
    assert handle.handle_id == "batches/job-1" and handle.mode == "batch" and handle.data == {"name": "batches/job-1"}
    assert Handle.from_dict(json.loads(json.dumps(handle.to_dict()))) == handle  # plain data


@pytest.mark.parametrize("status, kind", [(400, "bad_request"), (401, "auth"), (403, "auth")])
def test_a_rejected_submit_stops_the_route(status, kind):
    client = fake_client(batches__create=Recorder(ApiError(status)))
    writer = GeminiBatchWriter("flash-batch", make_route(), client=client)
    with pytest.raises(RouteRejected) as raised:
        writer.submit(writer.prepare([make_call()]))
    assert raised.value.kind == kind


@pytest.mark.parametrize(
    "state, expected",
    [
        ("JOB_STATE_QUEUED", "in_progress"),
        ("JOB_STATE_PENDING", "in_progress"),
        ("JOB_STATE_RUNNING", "in_progress"),
        ("JOB_STATE_PAUSED", "in_progress"),
        ("JOB_STATE_CANCELLING", "in_progress"),
        ("JOB_STATE_UPDATING", "in_progress"),
        ("JOB_STATE_SUCCEEDED", "ended"),
        ("JOB_STATE_PARTIALLY_SUCCEEDED", "ended"),
        ("JOB_STATE_FAILED", "failed"),
        ("JOB_STATE_CANCELLED", "canceled"),
        ("JOB_STATE_EXPIRED", "expired"),
    ],
)
def test_poll_maps_the_job_state(state, expected):
    writer = GeminiBatchWriter("flash-batch", make_route(), client=fake_client(batches__get=Recorder(job(state))))
    assert writer.poll(Handle("batches/job-1", ("c1",), data={"name": "batches/job-1"})).state == expected


def test_poll_treats_an_unknown_job_as_failed_and_passes_other_errors_up():
    handle = Handle("batches/job-1", ("c1",), data={"name": "batches/job-1"})
    writer = GeminiBatchWriter("flash-batch", make_route(), client=fake_client(batches__get=Recorder(ApiError(404))))
    assert writer.poll(handle).state == "failed"
    writer = GeminiBatchWriter("flash-batch", make_route(), client=fake_client(batches__get=Recorder(ApiError(500))))
    with pytest.raises(ApiError):
        writer.poll(handle)


def test_results_are_matched_by_the_custom_id_in_each_responses_metadata():
    a, b, c = (make_call(line).call_id for line in ("001", "002", "003"))
    stray = ids.call_id(ids.task_id("other", "9", {"x": "y"}), "001", 1)
    done = job("JOB_STATE_SUCCEEDED", [inlined(c, generated()), inlined(stray, generated()), inlined(a, error=types.JobError(code=500, message="oops")), inlined(b, generated())])
    writer = GeminiBatchWriter("flash-batch", make_route(), client=fake_client(batches__get=Recorder(done)))
    results = {r.call_id: r for r in writer.results(Handle("batches/job-1", (a, b, c), data={"name": "batches/job-1"}))}
    assert set(results) == {a, b, c}
    assert results[b].output == ANSWER and results[c].output == ANSWER and results[a].kind == "provider_error"


def test_batches_are_split_by_count_and_by_size():
    writer = GeminiBatchWriter("flash-batch", make_route(max_batch_calls=2))
    requests = writer.prepare([make_call(line) for line in ("001", "002", "003")])
    assert [len(group) for group in writer.split(requests)] == [2, 1]
    from dataclasses import replace

    big = [replace(r, size_bytes=12 * 1024 * 1024) for r in requests]
    assert [len(group) for group in GeminiBatchWriter("r", make_route(max_batch_calls=10)).split(big)] == [1, 1, 1]


def test_a_batch_created_just_before_a_crash_is_found_again_by_its_display_name():
    requests = GeminiBatchWriter("r", make_route()).prepare([make_call("001"), make_call("002")])
    name = f"rps-{digest_of([r.custom_id for r in requests])}"
    mine = job("JOB_STATE_RUNNING", name="batches/mine", display_name=name)
    other = job("JOB_STATE_RUNNING", name="batches/other", display_name="rps-somebody-else")
    dead = job("JOB_STATE_FAILED", name="batches/dead", display_name=name)
    writer = GeminiBatchWriter("flash-batch", make_route(), client=fake_client(batches__list=Recorder([other, dead, mine])))
    found = writer.recover(requests)
    assert found is not None and found.handle_id == "batches/mine" and found.call_ids == tuple(r.call_id for r in requests)
    assert writer.recover(requests[:1]) is None


# ---------------------------------------------------------------------------- the explicit cache


def cached_writer(client, **options):
    return GeminiBatchWriter("flash-batch", make_route(options={"cache": True, **options}), client=client, now=lambda: NOW)


def test_with_the_cache_on_the_rules_are_cached_once_and_the_requests_refer_to_the_cache():
    client = fake_client()
    writer = cached_writer(client)
    requests = writer.prepare([make_call("001"), make_call("002")])
    handle = writer.submit(requests)
    created = client.caches.create.calls[0]
    assert created["model"] == MODEL
    assert created["config"].system_instruction == rules().system and created["config"].ttl == "21600s"
    assert created["config"].display_name == "rps-rules-v1"
    for item in client.batches.create.calls[0]["src"]:
        assert item.config.cached_content == "cachedContents/c1" and item.config.system_instruction is None
        assert item.config.response_json_schema == SCHEMA  # the schema is not part of the cache
    assert handle.data["cache_name"] == "cachedContents/c1"
    [(kind, payload)] = writer.drain_notes()
    assert kind == "cache_created" and payload["route"] == "flash-batch" and payload["name"] == "cachedContents/c1"
    assert payload["key"] == f"{MODEL}|rules-v1" and payload["expires"] == "2026-11-07T18:00:00Z"
    assert writer.drain_notes() == []
    writer.submit(writer.prepare([make_call("003")]))
    assert len(client.caches.create.calls) == 1  # the second batch reuses the cache


def test_a_restart_reuses_a_recorded_cache_that_is_still_live_and_replaces_one_about_to_end():
    note = {"route": "flash-batch", "key": f"{MODEL}|rules-v1", "name": "cachedContents/old", "expires": "2026-11-07T18:00:00Z"}
    client = fake_client()
    writer = cached_writer(client)
    writer.restore([note, {**note, "route": "another-route", "name": "cachedContents/not-mine"}])
    handle = writer.submit(writer.prepare([make_call()]))
    assert client.caches.create.calls == [] and handle.data["cache_name"] == "cachedContents/old"
    soon = {**note, "expires": "2026-11-07T13:00:00Z"}  # under two hours left
    client = fake_client()
    writer = cached_writer(client)
    writer.restore([soon])
    writer.submit(writer.prepare([make_call()]))
    assert len(client.caches.create.calls) == 1


def test_the_cache_is_kept_alive_while_a_batch_that_uses_it_is_running():
    client = fake_client(caches__update=Recorder(Namespace(expire_time=NOW + timedelta(hours=6))), batches__get=Recorder(job("JOB_STATE_RUNNING")))
    clock = [NOW]
    writer = GeminiBatchWriter("flash-batch", make_route(options={"cache": True}), client=client, now=lambda: clock[0])
    handle = writer.submit(writer.prepare([make_call()]))
    writer.drain_notes()
    writer.poll(handle)
    assert client.caches.update.calls == []  # plenty of time left
    clock[0] = NOW + timedelta(hours=4)
    assert writer.poll(handle).state == "in_progress"
    update = client.caches.update.calls[0]
    assert update["name"] == "cachedContents/c1" and update["config"].ttl == "21600s"
    [(kind, payload)] = writer.drain_notes()
    assert kind == "cache_created" and payload["expires"] == "2026-11-07T18:00:00Z"


def test_a_failure_to_create_the_cache_is_loud():
    client = fake_client(caches__create=Recorder(ApiError(400, "content too small to cache")))
    writer = cached_writer(client)
    with pytest.raises(RouteRejected, match="too small"):
        writer.submit(writer.prepare([make_call()]))
    assert client.batches.create.calls == []


def sized_cache(tokens=12_345):
    """A cache as the provider reports it after creating it: its size is in the usage metadata."""
    return Namespace(
        name="cachedContents/c1",
        create_time=NOW,
        expire_time=NOW + timedelta(hours=6),
        usage_metadata=Namespace(total_token_count=tokens),
    )


def test_the_note_of_a_new_cache_carries_its_size_and_the_stretch_of_its_life_the_storage_fee_covers():
    writer = cached_writer(fake_client(caches__create=Recorder(sized_cache())))
    writer.submit(writer.prepare([make_call()]))
    [(kind, note)] = writer.drain_notes()
    assert kind == "cache_created" and note["tokens"] == 12_345
    assert note["created"] == "2026-11-07T12:00:00Z" and note["expires"] == "2026-11-07T18:00:00Z"
    assert "tokens_estimated" not in note and "extended_from" not in note


def test_a_cache_whose_size_the_provider_did_not_report_is_charged_by_a_generous_estimate():
    writer = cached_writer(fake_client())  # the default fake cache has no usage metadata
    writer.submit(writer.prepare([make_call()]))
    [(_kind, note)] = writer.drain_notes()
    assert note["tokens_estimated"] is True and note["tokens"] == estimate_text_tokens(rules().system) > 0


def test_keeping_a_cache_alive_notes_the_earlier_expiry_so_only_the_added_hours_are_charged():
    client = fake_client(
        caches__create=Recorder(sized_cache()),
        caches__update=Recorder(Namespace(expire_time=NOW + timedelta(hours=10))),
        batches__get=Recorder(job("JOB_STATE_RUNNING")),
    )
    clock = [NOW]
    writer = GeminiBatchWriter("flash-batch", make_route(options={"cache": True}), client=client, now=lambda: clock[0])
    handle = writer.submit(writer.prepare([make_call()]))
    writer.drain_notes()
    clock[0] = NOW + timedelta(hours=4)
    writer.poll(handle)
    [(kind, note)] = writer.drain_notes()
    assert kind == "cache_created" and note["tokens"] == 12_345 and "created" not in note
    assert note["extended_from"] == "2026-11-07T18:00:00Z" and note["expires"] == "2026-11-07T22:00:00Z"


def test_a_restored_cache_keeps_its_size_for_the_charges_that_come_later():
    recorded = {
        "route": "flash-batch", "key": f"{MODEL}|rules-v1", "name": "cachedContents/old",
        "expires": "2026-11-07T18:00:00Z", "tokens": 777,
    }
    client = fake_client(
        caches__update=Recorder(Namespace(expire_time=NOW + timedelta(hours=8))),  # the update does not say how big it is
        batches__get=Recorder(job("JOB_STATE_RUNNING")),
    )
    clock = [NOW]
    writer = GeminiBatchWriter("flash-batch", make_route(options={"cache": True}), client=client, now=lambda: clock[0])
    writer.restore([recorded])
    handle = writer.submit(writer.prepare([make_call()]))
    assert handle.data["cache_name"] == "cachedContents/old" and writer.drain_notes() == []  # reused: nothing new to charge
    clock[0] = NOW + timedelta(hours=4)
    writer.poll(handle)
    [(_kind, note)] = writer.drain_notes()
    assert note["tokens"] == 777 and note["extended_from"] == "2026-11-07T18:00:00Z"


def test_only_a_batch_writer_with_the_cache_on_bills_for_storage():
    assert GeminiBatchWriter("r", make_route(options={"cache": True})).bills_cache_storage is True
    assert GeminiBatchWriter("r", make_route()).bills_cache_storage is False
    assert GeminiSyncWriter("r", make_route("api", options={"cache": True})).bills_cache_storage is False


# ---------------------------------------------------------------------------- the one-call flow


def test_the_sync_writer_makes_one_generate_content_call_per_request():
    client = fake_client()
    writer = GeminiSyncWriter("flash-sync", make_route("api"), client=client)
    [request] = writer.prepare([make_call(kind="api")])
    result = writer.send(request)
    sent = client.models.generate_content.calls[0]
    assert sent["model"] == MODEL and sent["config"].system_instruction == rules().system
    assert result.output == ANSWER and result.usage == Usage(input_tokens=100, output_tokens=50)


def test_the_sync_writer_turns_an_error_into_a_failed_result_of_the_right_kind():
    for status, kind, retryable in [(429, "provider_error", True), (500, "provider_error", True), (403, "auth", False), (400, "bad_request", False)]:
        client = fake_client(models__generate_content=Recorder(ApiError(status)))
        writer = GeminiSyncWriter("flash-sync", make_route("api"), client=client)
        result = writer.send(writer.prepare([make_call(kind="api")])[0])
        assert (result.kind, result.retryable, result.output) == (kind, retryable, None)


# ---------------------------------------------------------------------------- the client


def test_the_client_is_built_from_the_environment_key(monkeypatch):
    from google import genai

    from rps.providers import RouteUnavailable

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RouteUnavailable, match="GEMINI_API_KEY"):
        GeminiBatchWriter("r", make_route())._client()
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    assert isinstance(GeminiBatchWriter("r", make_route(max_retries=4, timeout_seconds=90))._client(), genai.Client)
