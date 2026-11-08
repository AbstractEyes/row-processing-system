"""What a Gemini batch request puts on the wire, seen through the SDK's own code and a recorder (no network).

A live probe of the batch service found that the row schema in `response_schema` is refused at creation:
"Unknown name "additional_properties"" at the schema and at every nested object. The SDK's `Schema` type has the
field and sends it, even as `false`, and the API's `Schema` object does not have it.
"""

from __future__ import annotations

import pytest

from rps.config import RulesConfig
from rps.prompting import load_writer_rules
from rps.providers import GeminiBatchWriter

from adapter_helpers import call, route
from gemini_wire import Wire, names_in, send_through

MODEL = "gemini-3.8-flash"
REJECTED = {"additional_properties", "additionalProperties"}


@pytest.fixture(scope="module")
def pool_rules():
    return load_writer_rules(RulesConfig(package="rps.rules"))


def objects_in(schema):
    """How many objects (nodes with properties) a schema holds, at any depth."""
    count = 0
    if isinstance(schema, dict):
        count += 1 if "properties" in schema else 0
        for child in (schema.get("properties") or {}).values():
            count += objects_in(child)
        count += objects_in(schema.get("items"))
    return count


def sent_through_the_sdk(monkeypatch, pool_rules, **options):
    """The one inlined request of a batch of one call, as it is in the body the SDK sends: {"request", "metadata"}."""
    wire = Wire()
    send_through(monkeypatch, wire)
    writer = GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch", options=options))
    requests = writer.prepare([call("001", provider="gemini", model=MODEL, rules=pool_rules, max_output_tokens=8192)])
    writer.submit(requests)
    [(_method, _url, body)] = wire.to("batchGenerateContent")
    [item] = body["batch"]["inputConfig"]["requests"]["requests"]
    return item


def test_the_row_schema_in_response_schema_without_the_openapi_form_puts_the_rejected_name_on_the_wire(monkeypatch, pool_rules):
    # This is what the batch service refused: the plain form of the schema in the field the batch pages show.
    item = sent_through_the_sdk(monkeypatch, pool_rules, schema_field="response_schema")
    found = names_in(item["request"]["generationConfig"]["responseSchema"], REJECTED)
    assert len(found) == objects_in(pool_rules.schema) == 7  # one on every object of the row schema, as the service reported
    assert found[0] == "/additional_properties"


def test_the_documented_form_puts_none_of_the_names_the_batch_service_rejects_on_the_wire(monkeypatch, pool_rules):
    item = sent_through_the_sdk(monkeypatch, pool_rules, batch_form="documented")
    config = item["request"]["generationConfig"]
    assert "responseJsonSchema" not in config and names_in(item, REJECTED) == []
    schema = config["responseSchema"]
    assert schema["type"] == "OBJECT" and schema["required"] == pool_rules.schema["required"]
    assert schema["properties"]["rows"]["items"]["required"] == pool_rules.schema["properties"]["rows"]["items"]["required"]
    assert list(item["metadata"]) == ["key"]


def test_the_openapi_option_takes_the_same_schema_to_the_first_form_too(monkeypatch, pool_rules):
    # The shape and the field are two settings: the OpenAPI form can be sent in response_json_schema as well.
    item = sent_through_the_sdk(monkeypatch, pool_rules, schema="openapi")
    config = item["request"]["generationConfig"]
    assert "responseSchema" not in config and names_in(config["responseJsonSchema"], REJECTED) == []
    assert list(item["metadata"]) == ["custom_id"]
    item = sent_through_the_sdk(monkeypatch, pool_rules, schema="openapi", schema_field="response_schema")
    assert names_in(item, REJECTED) == [] and "responseSchema" in item["request"]["generationConfig"]
