"""What a Gemini batch request puts on the wire, seen through the SDK's own code and a recorder (no network).

A live probe of the batch service found that the row schema in `response_schema` is refused at creation:
"Unknown name "additional_properties"" at the schema and at every nested object. The SDK's `Schema` type has the
field and sends it, even as `false`, and the API's `Schema` object does not have it.
"""

from __future__ import annotations

import pytest

from rps import geminiprobe
from rps.config import ConfigError, RulesConfig
from rps.geminiprobe import probe_gemini_batch
from rps.prompting import load_writer_rules
from rps.providers import GeminiBatchWriter, GeminiSyncWriter
from rps.providers.gemini_api import api_version_of
from rps.routes import standard_route

from adapter_helpers import call, route
from gemini_wire import REAL_CLIENT, Wire, names_in, send_through

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


# ---------------------------------------------------------------------------- the version of the API, and the explicit cache

CACHE_URL = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
LIVE_ERROR = "404 Not Found. {'message': '', 'status': 'Not Found'}"


def the_sdks_own_version():
    return api_version_of(REAL_CLIENT(api_key="not-a-real-key"))


def test_a_cache_made_through_a_client_on_the_default_version_is_a_request_to_a_path_the_service_does_not_have(monkeypatch):
    # The live probe: caches.create answered "404 Not Found" with an empty body. The SDK posts it to the version
    # it defaults to (v1 in google-genai 3.0.0), and Google lists /v1beta/cachedContents and no v1 path for it.
    wire = Wire(missing=("/v1/cachedContents",))
    send_through(monkeypatch, wire)
    writer = GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch"))  # cache off: the writer asks for no version
    assert writer.resolved_api_version() is None
    with pytest.raises(Exception) as caught:
        writer._ensure_cache(MODEL, "rules-1", "The rules, long enough to cache.")
    assert str(caught.value) == LIVE_ERROR and caught.value.code == 404  # the text the live run printed
    [(method, url, body)] = wire.requests
    assert (method, url) == ("POST", f"https://generativelanguage.googleapis.com/{the_sdks_own_version()}/cachedContents")
    assert body["model"] == "models/gemini-3.8-flash"  # the name form is the one Google's examples use


def test_a_route_with_the_cache_on_sends_the_cache_the_batch_and_the_poll_to_v1beta(monkeypatch):
    wire = Wire(missing=("/v1/cachedContents",))
    send_through(monkeypatch, wire)
    writer = GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch", options={"cache": True}))
    assert writer.resolved_api_version() == "v1beta"
    requests = writer.prepare([call("001", provider="gemini", model=MODEL)])
    handle = writer.submit(requests)
    assert handle.data["cache_name"] == "cachedContents/cache-1" and handle.data["name"] == "batches/job-1"
    writer.poll(handle)
    assert [(method, url) for method, url, _body in wire.requests] == [
        ("POST", CACHE_URL),
        ("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:batchGenerateContent"),
        ("GET", "https://generativelanguage.googleapis.com/v1beta/batches/job-1"),
    ]
    [item] = wire.requests[1][2]["batch"]["inputConfig"]["requests"]["requests"]
    assert item["request"]["cachedContent"] == "cachedContents/cache-1" and "systemInstruction" not in item["request"]  # the rules are in the cache


def test_the_api_version_option_sets_the_version_of_every_request_of_the_route(monkeypatch):
    for option_value in ("v1beta", "v1"):
        wire = Wire()
        send_through(monkeypatch, wire)
        writer = GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch", options={"api_version": option_value}))
        writer.submit(writer.prepare([call("001", provider="gemini", model=MODEL)]))
        assert wire.requests[0][1].startswith(f"https://generativelanguage.googleapis.com/{option_value}/models/"), option_value
    wire = Wire()
    send_through(monkeypatch, wire)
    writer = GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch"))  # "auto", no cache: the SDK's own version
    writer.submit(writer.prepare([call("001", provider="gemini", model=MODEL)]))
    assert wire.requests[0][1].startswith(f"https://generativelanguage.googleapis.com/{the_sdks_own_version()}/models/")


def test_the_cache_with_the_api_version_v1_is_refused_when_the_route_is_made_and_the_reason_is_in_the_message():
    with pytest.raises(ConfigError, match=r"'cache' needs the v1beta API, the only version that has explicit caching"):
        GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch", options={"cache": True, "api_version": "v1"}))
    with pytest.raises(ConfigError, match="api_version"):
        GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch", options={"api_version": "v2"}))
    # v1beta with the cache, and the cache left off with v1, are both fine, on the one-call route too
    both = GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch", options={"cache": True, "api_version": "v1beta"}))
    assert both.resolved_api_version() == "v1beta"
    assert GeminiBatchWriter("flash-batch", route("gemini", MODEL, "batch", options={"api_version": "v1"})).resolved_api_version() == "v1"
    assert GeminiSyncWriter("flash-sync", route("gemini", MODEL, "api", options={"api_version": "v1beta"})).resolved_api_version() == "v1beta"


class Clock:
    """Time for the probe that moves only when the probe sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run_probe(monkeypatch, wire, **kwargs):
    send_through(monkeypatch, wire)
    clock = Clock()
    lines = []
    report = probe_gemini_batch(
        "flash-batch", printer=lines.append, clock=clock, sleep=clock.sleep, wait_seconds=10, poll_seconds=5, **kwargs
    )
    return report, lines


def test_a_probe_that_makes_a_cache_sends_all_of_its_requests_to_v1beta_and_says_so(monkeypatch):
    wire = Wire(missing=("/v1/cachedContents",))
    report, lines = run_probe(monkeypatch, wire, keys=("as_sent", "cache_on"), try_cache=True)
    assert report.refused is None and "API version v1beta" in lines[0]
    assert wire.requests and all("/v1beta/" in url for _method, url, _body in wire.requests)
    made = wire.to("cachedContents")
    assert made[0][:2] == ("POST", CACHE_URL) and made[-1][0] == "DELETE"  # made, then deleted


def test_a_probe_without_a_cache_goes_where_the_route_goes_and_a_flag_or_an_option_picks_the_version(monkeypatch):
    own = the_sdks_own_version()
    wire = Wire()
    _report, lines = run_probe(monkeypatch, wire, keys=("as_sent",))
    assert f"API version {own} (the SDK default)" in lines[0]
    assert all(url.startswith(f"https://generativelanguage.googleapis.com/{own}/") for _m, url, _b in wire.requests)
    wire = Wire()
    _report, lines = run_probe(monkeypatch, wire, keys=("as_sent",), api_version="v1beta")
    assert "API version v1beta;" in lines[0] and all("/v1beta/" in url for _m, url, _b in wire.requests)
    wire = Wire()
    config = standard_route("flash-batch", max_output_tokens=8192, options={"api_version": "v1"})
    _report, lines = run_probe(monkeypatch, wire, keys=("as_sent",), route_config=config)
    assert "API version v1;" in lines[0] and all(url.startswith("https://generativelanguage.googleapis.com/v1/") for _m, url, _b in wire.requests)


def test_a_probe_with_a_cache_does_not_go_to_v1_and_an_unknown_version_is_refused(monkeypatch):
    wire = Wire()
    report, _lines = run_probe(monkeypatch, wire, keys=("cache_on",), try_cache=True, api_version="v1")
    assert report.refused and "explicit cache exists only in the v1beta API" in report.refused and wire.requests == []
    report, _lines = run_probe(monkeypatch, wire, keys=("as_sent",), api_version="v2")
    assert report.refused and "v2" in report.refused and wire.requests == []
    config = standard_route("flash-batch", max_output_tokens=8192, options={"cache": True})
    wire = Wire(missing=("/v1/cachedContents",))
    report, _lines = run_probe(monkeypatch, wire, keys=("cache_off",), route_config=config)
    assert report.refused is None and all("/v1beta/" in url for _m, url, _b in wire.requests)  # a route with the cache on is taken to v1beta


def test_the_command_line_takes_the_api_version_only_with_the_batch_probe(monkeypatch):
    seen = {}

    def fake(route_name, **kwargs):
        seen.update(kwargs)
        outcome = geminiprobe.BatchOutcome(key="as_sent", title="t", detail="d", verdict="taken")
        return geminiprobe.BatchProbeReport(route=route_name, outcomes=[outcome])

    monkeypatch.setattr(geminiprobe, "probe_gemini_batch", fake)
    assert geminiprobe.main(["--batch", "--api-version", "v1beta"]) == 0 and seen["api_version"] == "v1beta"
    assert geminiprobe.main(["--batch"]) == 0 and seen["api_version"] is None
    with pytest.raises(SystemExit):
        geminiprobe.main(["--api-version", "v1beta"])
