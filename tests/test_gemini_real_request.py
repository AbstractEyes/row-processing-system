"""The Gemini request for the real rule pool: the form of the row schema it sends, and what a refusal says.

A live run of the pool through `flash-batch` had every call refused with "3: Request contains an invalid
argument.", which the adapter took for a passing trouble of the provider's (so each call was tried again), and
whose text was cut to one sentence. These tests hold the schema forms, the reading of the status codes the batch
service reports, and the full error text.
"""

from __future__ import annotations

import copy
import json

import pytest
from google.genai import types

from rps import rules as pool
from rps.config import ConfigError, RulesConfig
from rps.failures import PROVIDER_ERROR_CHARS
from rps.prompting import load_writer_rules
from rps.providers import GeminiBatchWriter, GeminiSyncWriter, Handle
from rps.providers.gemini_api import inlined_error_text, parse_inlined
from rps.providers.schemas import GEMINI_SCHEMA_MODES, gemini_schema

from adapter_helpers import ApiError, Namespace, Recorder, call, route

MODEL = "gemini-3.8-flash"
LONG = "The schema is refused because " + "x" * 900  # longer than the 200 characters that used to be kept


@pytest.fixture(scope="module")
def pool_rules():
    return load_writer_rules(RulesConfig(package="rps.rules"))


def pool_call(pool_rules, line="001"):
    return call(line, provider="gemini", model=MODEL, rules=pool_rules, max_output_tokens=8192)


def route_with(schema=None, kind="batch"):
    options = {} if schema is None else {"schema": schema}
    return route("gemini", MODEL, kind, options=options)


def walk(original, plain, path="(root)"):
    """The plain form has the structure of the original, and the original's value lists in descriptions."""
    expected = set(original) - {"enum"}
    if "enum" in original:
        expected.add("description")
        for value in original["enum"]:
            assert json.dumps(value) in plain["description"], f"{path}: {value!r} is not in the description"
    assert set(plain) == expected, path
    for name, child in original.get("properties", {}).items():
        walk(child, plain["properties"][name], f"{path}.{name}")
    if isinstance(original.get("items"), dict):
        walk(original["items"], plain["items"], f"{path}[]")


# ---------------------------------------------------------------------------- the forms of the schema


def test_the_plain_form_of_the_pool_schema_keeps_the_structure_and_moves_every_value_list_into_a_description():
    schema = pool.row_schema()
    before = copy.deepcopy(schema)
    plain = gemini_schema(schema, "plain")
    assert schema == before  # the schema passed in is never changed
    assert "enum" not in json.dumps(plain)
    walk(schema, plain)
    assert plain["required"] == schema["required"] and plain["additionalProperties"] is False
    # the empty-string value, an integer list and a list of sixteen are among the ones that moved
    assert '""' in plain["properties"]["class"]["description"]
    assert "1, 2, 3" in plain["properties"]["rows"]["items"]["properties"]["rank"]["description"]
    assert "colour" in plain["properties"]["rows"]["items"]["properties"]["thoughts"]["items"]["properties"]["step"]["description"]


def test_the_exact_form_is_a_copy_of_the_schema_and_the_none_form_is_no_schema():
    schema = pool.row_schema()
    exact = gemini_schema(schema, "exact")
    assert exact == schema and exact is not schema
    exact["properties"]["skip"]["type"] = "string"
    assert schema["properties"]["skip"]["type"] == "boolean"
    assert gemini_schema(schema, "none") is None
    with pytest.raises(ValueError, match="unknown schema mode"):
        gemini_schema(schema, "turbo")
    assert set(GEMINI_SCHEMA_MODES) == {"plain", "exact", "none"}


def test_the_plain_form_extends_a_description_and_treats_a_field_named_enum_as_a_field():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "description": "The kind of thing.", "enum": ["a", "b"]},
            "enum": {"type": "string"},
            "fixed": {"type": "string", "const": "x"},
            "either": {"anyOf": [{"type": "string", "enum": ["p"]}, {"type": "null"}]},
        },
        "required": ["kind"],
    }
    plain = gemini_schema(schema, "plain")
    assert plain["properties"]["kind"]["description"] == 'The kind of thing. Allowed values: "a", "b".'
    assert plain["properties"]["enum"] == {"type": "string"}
    assert plain["properties"]["fixed"] == {"type": "string", "description": 'Allowed values: "x".'}
    assert plain["properties"]["either"]["anyOf"][0] == {"type": "string", "description": 'Allowed values: "p".'}
    assert plain["required"] == ["kind"]


def test_a_schema_without_value_lists_is_sent_as_it_is_in_the_plain_form():
    tiny = {"type": "object", "additionalProperties": False, "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    assert gemini_schema(tiny, "plain") == tiny


# ---------------------------------------------------------------------------- the request


def config_of(writer, pool_rules):
    [request] = writer.prepare([pool_call(pool_rules)])
    return request.payload["request"].config, request


def test_the_request_for_the_rule_pool_carries_the_plain_schema_by_default(pool_rules):
    config, request = config_of(GeminiBatchWriter("flash-batch", route_with()), pool_rules)
    assert config.response_json_schema == gemini_schema(pool_rules.schema, "plain")
    assert "enum" not in json.dumps(config.response_json_schema)
    assert config.response_mime_type == "application/json" and config.system_instruction == pool_rules.system
    assert config.max_output_tokens == 8192 and config.thinking_config.thinking_level == types.ThinkingLevel.MEDIUM
    assert request.size_bytes > len(pool_rules.system)


def test_the_exact_option_sends_the_schema_as_it_is_and_the_none_option_sends_no_schema(pool_rules):
    exact, exact_request = config_of(GeminiBatchWriter("flash-batch", route_with("exact")), pool_rules)
    assert exact.response_json_schema == pool_rules.schema
    none, none_request = config_of(GeminiBatchWriter("flash-batch", route_with("none")), pool_rules)
    assert none.response_json_schema is None and none.response_mime_type == "application/json"
    assert none.system_instruction == pool_rules.system
    assert none_request.est_input_tokens < exact_request.est_input_tokens
    assert none_request.size_bytes < exact_request.size_bytes


def test_the_schema_option_applies_to_the_one_call_route_too_and_is_checked(pool_rules):
    config, _ = config_of(GeminiSyncWriter("flash-sync", route_with("none", "api")), pool_rules)
    assert config.response_json_schema is None
    with pytest.raises(ConfigError, match="schema"):
        GeminiBatchWriter("flash-batch", route_with("turbo"))
    with pytest.raises(ConfigError, match="unknown option"):
        GeminiBatchWriter("flash-batch", route("gemini", MODEL, options={"scheme": "plain"}))


def test_a_route_that_sends_no_schema_does_not_need_one_that_fits(pool_rules):
    needs_object = pool_rules.__class__(system=pool_rules.system, schema={"type": "array", "items": {}}, version="v")
    with pytest.raises(ConfigError, match="root"):
        GeminiBatchWriter("flash-batch", route_with()).check_rules(needs_object)
    GeminiBatchWriter("flash-batch", route_with("none")).check_rules(needs_object)


# ---------------------------------------------------------------------------- what a refusal says


def failed_item(code, message="Request contains an invalid argument.", details=None):
    return types.InlinedResponse(error=types.JobError(code=code, message=message, details=details), metadata={"custom_id": "c"})


def test_an_invalid_argument_from_the_batch_service_stops_the_route_instead_of_being_tried_again():
    result = parse_inlined(failed_item(3), "c1")
    assert result.kind == "bad_request" and result.retryable is False
    assert result.error == "3: Request contains an invalid argument."


def test_the_status_codes_the_batch_service_reports_are_read_as_codes_and_not_as_http_statuses():
    expected = {
        3: ("bad_request", False),  # invalid argument
        5: ("bad_request", False),  # not found
        9: ("bad_request", False),  # failed precondition (billing not set up, say)
        7: ("auth", False),  # permission denied
        16: ("auth", False),  # unauthenticated
        1: ("canceled", True),
        2: ("provider_error", True),  # unknown
        4: ("provider_error", True),  # deadline exceeded
        8: ("provider_error", True),  # resource exhausted
        13: ("provider_error", True),  # internal
        14: ("provider_error", True),  # unavailable
        400: ("bad_request", False),
        403: ("auth", False),
        429: ("provider_error", True),
        503: ("provider_error", True),
    }
    for code, (kind, retryable) in expected.items():
        result = parse_inlined(failed_item(code, "text"), "c1")
        assert (result.kind, result.retryable) == (kind, retryable), code
    assert parse_inlined(failed_item(None, ""), "c1").error == "the request failed"


def test_a_failure_that_names_the_explicit_cache_is_tried_again_whatever_its_code():
    result = parse_inlined(failed_item(3, "CachedContent not found (or permission denied)"), "c1")
    assert result.kind == "provider_error" and result.retryable is True


def test_the_providers_whole_message_and_its_details_are_kept():
    details = ['{"@type": "type.googleapis.com/google.rpc.BadRequest", "fieldViolations": [{"field": "response_json_schema"}]}']
    result = parse_inlined(failed_item(3, LONG, details), "c1")
    assert result.error.startswith("3: The schema is refused because ") and len(result.error) > 900
    assert "fieldViolations" in result.error and "response_json_schema" in result.error
    huge = inlined_error_text(types.JobError(code=3, message="y" * 5000))
    assert len(huge) == PROVIDER_ERROR_CHARS


def test_a_key_shaped_text_in_an_error_is_not_kept():
    leaked = "sk-" + "a1" * 20
    text = inlined_error_text(types.JobError(code=3, message=f"bad key {leaked}"))
    assert leaked not in text and "[redacted]" in text


def test_a_refused_one_call_request_keeps_its_long_error_text():
    client = Namespace(models=Namespace(generate_content=Recorder(ApiError(400, LONG))))
    writer = GeminiSyncWriter("flash-sync", route_with(kind="api"), client=client)
    request = writer.prepare([call("001", provider="gemini", model=MODEL, kind="api")])[0]
    result = writer.send(request)
    assert result.kind == "bad_request" and result.retryable is False
    assert len(result.error) > 900 and result.error.endswith("x" * 100)


def test_a_failed_job_reports_its_code_and_its_whole_message():
    job = types.BatchJob.model_validate(
        {"name": "batches/j", "state": "JOB_STATE_FAILED", "error": {"code": 3, "message": LONG}}
    )
    writer = GeminiBatchWriter("flash-batch", route_with(), client=Namespace(batches=Namespace(get=Recorder(job))))
    status = writer.poll(Handle("batches/j", ("c1",), data={"name": "batches/j"}))
    assert status.state == "failed" and status.note.startswith("3: The schema is refused") and len(status.note) > 900
    quiet = types.BatchJob.model_validate({"name": "batches/j", "state": "JOB_STATE_FAILED"})
    writer = GeminiBatchWriter("flash-batch", route_with(), client=Namespace(batches=Namespace(get=Recorder(quiet))))
    assert writer.poll(Handle("batches/j", ("c1",), data={"name": "batches/j"})).note is None
