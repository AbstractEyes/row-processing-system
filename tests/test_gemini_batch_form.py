"""The two forms of a Gemini batch request: the first one, and the one the batch pages document.

A batch of which every request comes back refused as an invalid argument is sent again, once for the run, in the
documented form: the schema in `response_schema`, the metadata key `key`, no explicit cache. See the module notes
of `rps.providers.gemini_api` and the run-level tests in test_gemini_refused_run.
"""

from __future__ import annotations

import json

import pytest
from google.genai import types

from rps import ids
from rps.config import ConfigError
from rps.failures import PROVIDER_ERROR_CHARS
from rps.providers import GeminiBatchWriter, GeminiSyncWriter, Handle
from rps.providers.schemas import gemini_schema

from adapter_helpers import Recorder
from test_adapter_gemini import fake_client, generated, inlined, job, make_call, make_route


def refusal(call_id, code=3, message="Request contains an invalid argument.", key="custom_id"):
    return types.InlinedResponse(error=types.JobError(code=code, message=message), metadata={key: ids.custom_id(call_id)})


def handle_of(*call_ids, form=0):
    return Handle("batches/job-1", tuple(call_ids), data={"name": "batches/job-1", "form": form})


def writer_for(responses, **options):
    client = fake_client(batches__get=Recorder(job("JOB_STATE_SUCCEEDED", responses)))
    return GeminiBatchWriter("flash-batch", make_route(options=options), client=client)


# ---------------------------------------------------------------------------- the options


def test_the_route_options_name_the_schema_field_and_the_batch_form():
    assert GeminiBatchWriter("r", make_route())._batch_form == "auto"
    assert GeminiBatchWriter("r", make_route(options={"batch_form": "documented"}))._batch_form == "documented"
    with pytest.raises(ConfigError, match="batch_form"):
        GeminiBatchWriter("r", make_route(options={"batch_form": "sometimes"}))
    with pytest.raises(ConfigError, match="schema_field"):
        GeminiBatchWriter("r", make_route(options={"schema_field": "response_format"}))
    with pytest.raises(ConfigError, match="schema_field"):
        GeminiSyncWriter("r", make_route("api", options={"schema_field": 3}))
    with pytest.raises(ConfigError, match="unknown option"):
        GeminiSyncWriter("r", make_route("api", options={"batch_form": "full"}))  # a one-call route has no batches


@pytest.mark.parametrize("writer_class, kind", [(GeminiBatchWriter, "batch"), (GeminiSyncWriter, "api")])
def test_the_schema_field_option_moves_the_schema_to_response_schema_for_either_writer(writer_class, kind):
    default = writer_class("r", make_route(kind))
    moved = writer_class("r", make_route(kind, options={"schema_field": "response_schema"}))
    [first] = default.prepare([make_call(kind=kind)])
    [second] = moved.prepare([make_call(kind=kind)])
    one, two = first.payload["request"].config, second.payload["request"].config
    assert one.response_json_schema is not None and one.response_schema is None
    assert two.response_schema == one.response_json_schema and two.response_json_schema is None
    assert two.response_mime_type == "application/json"


def test_a_route_with_no_schema_sends_none_in_either_field():
    writer = GeminiBatchWriter("r", make_route(options={"schema": "none", "schema_field": "response_schema"}))
    [request] = writer.prepare([make_call()])
    config = request.payload["request"].config
    assert config.response_schema is None and config.response_json_schema is None


# ---------------------------------------------------------------------------- the two forms of a request


def test_the_first_form_is_the_default_and_the_documented_form_changes_four_things():
    writer = GeminiBatchWriter("flash-batch", make_route())
    [first] = writer.prepare([make_call()])
    assert first.payload["form"] == 0
    full = first.payload["request"]
    assert full.metadata == {"custom_id": ids.custom_id(first.call_id)} and full.config.response_json_schema is not None
    writer.use_form(1)
    [second] = writer.prepare([make_call()])
    documented = second.payload["request"]
    assert second.payload["form"] == 1 and list(documented.metadata) == ["key"]
    assert documented.metadata["key"] == full.metadata["custom_id"]
    # the schema moves to response_schema and loses additionalProperties, which that field does not take
    assert documented.config.response_json_schema is None
    assert documented.config.response_schema == gemini_schema(make_call().rules.schema, "openapi")
    assert "additionalProperties" in json.dumps(full.config.response_json_schema)
    assert "additionalProperties" not in json.dumps(documented.config.response_schema)
    # nothing else differs: the rules, the thinking setting, the limit and the answer type are the same
    assert documented.contents == full.contents
    for name in ("system_instruction", "thinking_config", "max_output_tokens", "response_mime_type"):
        assert getattr(documented.config, name) == getattr(full.config, name), name
    writer.use_form(0)
    assert writer.prepare([make_call()])[0].payload["form"] == 0


def test_a_route_set_to_the_documented_form_starts_in_it():
    writer = GeminiBatchWriter("flash-batch", make_route(options={"batch_form": "documented"}))
    [request] = writer.prepare([make_call()])
    assert request.payload["form"] == 1 and list(request.payload["request"].metadata) == ["key"]
    [other] = GeminiBatchWriter("r", make_route(options={"batch_form": "full"})).prepare([make_call()])
    assert other.payload["form"] == 0


def test_the_documented_form_sends_no_explicit_cache_and_the_handle_says_what_form_the_batch_was_in():
    client = fake_client()
    writer = GeminiBatchWriter("flash-batch", make_route(options={"cache": True}), client=client)
    first = writer.submit(writer.prepare([make_call("001")]))
    assert first.data["form"] == 0 and first.data["cache_name"] == "cachedContents/c1"
    assert client.batches.create.calls[0]["src"][0].config.cached_content == "cachedContents/c1"
    writer.use_form(1)
    second = writer.submit(writer.prepare([make_call("002")]))
    assert second.data["form"] == 1 and "cache_name" not in second.data
    sent = client.batches.create.calls[1]["src"][0]
    assert sent.config.cached_content is None and sent.config.system_instruction  # the rules go in the request itself
    assert len(client.caches.create.calls) == 1  # no second cache was made for it


def test_a_group_prepared_in_the_first_form_is_sent_as_the_first_form_even_after_the_step_down():
    client = fake_client()
    writer = GeminiBatchWriter("flash-batch", make_route(), client=client)
    requests = writer.prepare([make_call("001"), make_call("002")])
    writer.use_form(1)  # another batch came back refused while these waited
    handle = writer.submit(requests)
    assert handle.data["form"] == 0  # they were built in the first form, and are read as such if they are refused


def test_the_answers_are_matched_by_the_metadata_key_as_well_as_by_the_custom_id():
    a, b = make_call("001").call_id, make_call("002").call_id
    done = job(
        "JOB_STATE_SUCCEEDED",
        [
            types.InlinedResponse(response=generated(), metadata={"key": ids.custom_id(a)}),
            types.InlinedResponse(response=generated(), metadata={"custom_id": ids.custom_id(b)}),
        ],
    )
    writer = GeminiBatchWriter("flash-batch", make_route(), client=fake_client(batches__get=Recorder(done)))
    results = {r.call_id: r for r in writer.results(handle_of(a, b))}
    assert set(results) == {a, b} and all(r.error is None for r in results.values())


# ---------------------------------------------------------------------------- a batch refused as a whole


def test_a_batch_every_item_of_which_is_an_invalid_argument_is_read_as_a_trouble_of_the_form_and_sent_again():
    a, b = make_call("001").call_id, make_call("002").call_id
    writer = writer_for([refusal(a), refusal(b)])
    assert writer._form() == 0
    results = writer.results(handle_of(a, b))
    assert [r.call_id for r in results] == [a, b]
    for result in results:
        assert result.kind == "provider_error" and result.retryable is True and result.output is None
        assert result.error.startswith("3: Request contains an invalid argument. | every request of the batch was refused as sent;")
        assert "response_schema" in result.error and "no explicit cache" in result.error
    assert writer._form() == 1  # the calls prepared from now on are in the documented form


def test_a_second_batch_of_the_first_form_that_comes_back_refused_after_the_step_down_is_sent_again_too():
    a = make_call("001").call_id
    writer = writer_for([refusal(a)])
    writer.use_form(1)  # an earlier batch has already stepped the route down
    [result] = writer.results(handle_of(a, form=0))
    assert result.retryable is True and result.kind == "provider_error" and writer._form() == 1


def test_the_same_refusal_after_a_restart_steps_down_again_and_a_refused_documented_form_is_final():
    a = make_call("001").call_id
    restarted = writer_for([refusal(a, key="key")])
    assert restarted._form() == 0  # a new process knows nothing of the step down
    [again] = restarted.results(handle_of(a, form=0))
    assert again.retryable is True and restarted._form() == 1
    [final] = restarted.results(handle_of(a, form=1))
    assert final.kind == "bad_request" and final.retryable is False
    assert "python -m rps.geminiprobe --batch --route flash-batch" in final.error
    assert final.error.startswith("3: Request contains an invalid argument. | ")


def test_a_route_set_to_the_first_form_never_steps_down():
    a = make_call("001").call_id
    writer = writer_for([refusal(a)], batch_form="full")
    [result] = writer.results(handle_of(a))
    assert result.kind == "bad_request" and result.retryable is False and writer._form() == 0
    assert 'the route option "batch_form" is "full"' in result.error


def test_an_http_400_item_is_read_the_same_way_and_other_codes_are_not():
    a = make_call("001").call_id
    [result] = writer_for([refusal(a, code=400)]).results(handle_of(a))
    assert result.retryable is True and "sent again" in result.error
    for code, kind in ((5, "bad_request"), (7, "auth"), (14, "provider_error")):
        untouched = writer_for([refusal(a, code=code)])
        [result] = untouched.results(handle_of(a))
        assert result.kind == kind and "sent again" not in result.error and untouched._form() == 0, code


def test_a_batch_in_which_some_items_were_answered_is_not_read_as_refused_as_a_whole():
    a, b, c = (make_call(line).call_id for line in ("001", "002", "003"))
    writer = writer_for([refusal(a), inlined(b, generated()), refusal(c)])
    results = {r.call_id: r for r in writer.results(handle_of(a, b, c))}
    assert results[b].error is None and writer._form() == 0
    assert results[a].kind == "bad_request" and results[a].retryable is False and "sent again" not in results[a].error


def test_a_note_added_to_a_long_error_keeps_the_error_within_its_length():
    a = make_call("001").call_id
    writer = writer_for([refusal(a, message="x" * 5000)])
    [result] = writer.results(handle_of(a))
    assert len(result.error) <= PROVIDER_ERROR_CHARS and result.error.endswith("(schema in response_schema, metadata key, no explicit cache)")
    assert result.error.startswith("3: xxxx")


def test_the_empty_batch_and_the_batch_of_strangers_change_nothing():
    a = make_call("001").call_id
    writer = writer_for([])
    assert writer.results(handle_of(a)) == [] and writer._form() == 0
    stranger = ids.call_id(ids.task_id("other", "9", {"x": "y"}), "001", 1)
    writer = writer_for([refusal(stranger)])
    assert writer.results(handle_of(a)) == [] and writer._form() == 0
