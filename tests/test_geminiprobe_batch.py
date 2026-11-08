"""The tool that sends the Gemini writer's request to the batch service, whole and with one part changed."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from google.genai import types

from rps import geminiprobe
from rps.geminiprobe import BATCH_VARIANTS, main, probe_gemini_batch
from rps.ledger import PriceTable
from rps.routes import standard_route

from adapter_helpers import ApiError, Namespace

MODEL = "gemini-3.8-flash"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
DETAILS = ['{"@type": "type.googleapis.com/google.rpc.BadRequest", "fieldViolations": [{"field": "generation_config"}]}']
ANSWER = '{"skip": false, "reason": "", "class": "", "rows": []}'


def generated():
    return types.GenerateContentResponse.model_validate(
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": ANSWER}]}, "finish_reason": "STOP"}],
            "usage_metadata": {"prompt_token_count": 6000, "candidates_token_count": 40, "total_token_count": 6040},
        }
    )


class Clock:
    """Time that only moves when the probe sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BatchService:
    """A fake Gemini batch service: a job answers its one item at its first look, or never.

    `refuses(request, model)` returns the sentence of a refusal for a request the service will not take, else None.
    `stalls(request, model)` marks a request whose job is never done.
    """

    def __init__(self, refuses=None, stalls=None, create_error=None):
        self.refuses = refuses or (lambda request, model: None)
        self.stalls = stalls or (lambda request, model: False)
        self.create_error = create_error or (lambda request, model: None)
        self.jobs = {}
        self.cancelled = []
        self.caches_made = []
        self.caches_deleted = []
        self.looks = 0

    # -- batches

    def create(self, *, model, src, config=None):
        [request] = src  # the probe sends batches of one item
        error = self.create_error(request, model)
        if error is not None:
            raise error
        name = f"batches/probe-{len(self.jobs) + 1}"
        self.jobs[name] = {"request": request, "model": model, "display_name": config.display_name}
        return types.BatchJob.model_validate({"name": name, "state": "JOB_STATE_PENDING"})

    def get(self, *, name):
        self.looks += 1
        job = self.jobs[name]
        request, model = job["request"], job["model"]
        if self.stalls(request, model):
            return types.BatchJob.model_validate({"name": name, "state": "JOB_STATE_RUNNING"})
        refusal = self.refuses(request, model)
        if refusal is not None:
            item = types.InlinedResponse(
                error=types.JobError(code=3, message=refusal, details=DETAILS), metadata=request.metadata
            )
        else:
            item = types.InlinedResponse(response=generated(), metadata=request.metadata)
        return types.BatchJob.model_validate(
            {"name": name, "state": "JOB_STATE_SUCCEEDED", "dest": {"inlined_responses": [item.model_dump()]}}
        )

    def cancel(self, *, name):
        self.cancelled.append(name)

    # -- caches

    def cache_create(self, *, model, config):
        name = f"cachedContents/probe-{len(self.caches_made) + 1}"
        self.caches_made.append((name, config.system_instruction, config.ttl))
        return Namespace(name=name, expire_time=NOW + timedelta(minutes=10), create_time=NOW)

    def cache_delete(self, *, name):
        self.caches_deleted.append(name)

    def client(self):
        return Namespace(
            batches=Namespace(create=self.create, get=self.get, cancel=self.cancel),
            caches=Namespace(create=self.cache_create, delete=self.cache_delete),
        )

    def request_of(self, outcome):
        return self.jobs[outcome.job]["request"]


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")


def probe(service, **kwargs):
    lines = []
    clock = Clock()
    kwargs.setdefault("sleep", clock.sleep)
    report = probe_gemini_batch(
        "flash-batch", client=service.client(), printer=lines.append, now=lambda: NOW, clock=clock, wait_seconds=60, poll_seconds=5, **kwargs
    )
    return report, lines


def keys(report, verdict):
    return report.keys_with(verdict)


def test_a_service_that_takes_everything_is_not_refusing_the_request(with_key):
    service = BatchService()
    report, lines = probe(service)
    expected = [key for key in BATCH_VARIANTS if key not in ("cache_off", "cache_on")]
    assert [outcome.key for outcome in report.outcomes] == expected  # one batch of one item for each variant
    assert len(service.jobs) == len(expected) and service.cancelled == []
    assert report.ok and keys(report, "refused") == [] and report.estimate_usd > 0
    assert all(outcome.cost_usd > 0 and outcome.finish for outcome in report.outcomes)
    text = "\n".join(lines)
    assert "took the request as the writer sends it" in lines[-1] and "not-a-real-key" not in text


def test_the_variants_each_change_one_part_of_the_request_as_sent(with_key):
    service = BatchService()
    report, _lines = probe(service)
    sent = service.request_of(report.outcomes[0])
    by_key = {outcome.key: service.request_of(outcome) for outcome in report.outcomes}
    assert sent.config.response_json_schema is not None and sent.config.system_instruction and sent.config.thinking_config
    assert sent.metadata == {"custom_id": sent.metadata["custom_id"]} and sent.config.cached_content is None
    # each variant is the request as sent with one part gone or changed
    assert by_key["no_schema"].config.response_json_schema is None and by_key["no_schema"].config.response_schema is None
    assert by_key["no_schema"].config.system_instruction == sent.config.system_instruction
    assert by_key["no_system"].config.system_instruction is None and by_key["no_system"].config.response_json_schema is not None
    assert by_key["no_thinking"].config.thinking_config is None and by_key["no_thinking"].config.system_instruction
    assert by_key["no_metadata"].metadata is None and by_key["no_metadata"].config == sent.config
    assert by_key["schema_field"].config.response_json_schema is None
    assert by_key["schema_field"].config.response_schema == sent.config.response_json_schema
    assert by_key["metadata_key"].metadata == {"key": sent.metadata["custom_id"]} and by_key["metadata_key"].config == sent.config
    openapi = by_key["openapi"]  # the OpenAPI form of the schema in response_schema, the rest as sent
    assert openapi.config.response_json_schema is None and openapi.config.response_schema is not None
    assert "additionalProperties" in json.dumps(sent.config.response_json_schema)
    assert "additionalProperties" not in json.dumps(openapi.config.response_schema)
    assert openapi.metadata == sent.metadata and openapi.config.system_instruction == sent.config.system_instruction
    assert openapi.config.thinking_config == sent.config.thinking_config and openapi.config.cached_content is None
    documented = by_key["documented"]
    assert documented.config.response_schema == openapi.config.response_schema and documented.config.response_json_schema is None
    assert list(documented.metadata) == ["key"] and documented.config.cached_content is None
    bare = by_key["bare"]
    assert bare.contents == sent.contents and bare.metadata is None
    assert bare.config.max_output_tokens == 8192
    assert bare.config.model_dump(exclude_none=True) == {"max_output_tokens": 8192}
    assert all(request.contents == sent.contents for request in by_key.values())


def test_the_part_the_service_refuses_is_named_with_the_providers_whole_error(with_key):
    # The service refuses any request that carries the schema in response_json_schema.
    service = BatchService(refuses=lambda request, model: "Request contains an invalid argument." if request.config.response_json_schema else None)
    report, lines = probe(service)
    assert not report.ok and report.verdict("as_sent") == "refused"
    assert keys(report, "taken") == ["no_schema", "schema_field", "openapi", "documented", "bare"]
    refused = report.outcomes[0]
    assert refused.state == "JOB_STATE_SUCCEEDED" and refused.error.startswith("3: Request contains an invalid argument. | details: ")
    assert "fieldViolations" in refused.error  # the whole error: code, message and details
    text = "\n".join(lines)
    assert "REFUSED (JOB_STATE_SUCCEEDED" in text and "fieldViolations" in text
    result = [line for line in lines if line.startswith("RESULT:") or (line.startswith("  ") and not line.startswith("    "))]
    assert result[0] == "RESULT: the batch service refused the request as the writer sends it."
    assert any("(without the schema)" in line and 'set the route option "schema" to "none"' in line for line in result)
    assert any("(schema in response_schema)" in line and '"schema_field" to "response_schema"' in line for line in result)
    assert any("(schema in the OpenAPI form)" in line and '"schema" to "openapi"' in line for line in result)
    assert any("documented form" in line and '"batch_form" to "documented"' in line for line in result)
    assert not any("bare request was refused" in line for line in result)


def test_the_findings_of_the_live_run_are_reproduced_and_the_openapi_form_is_the_variant_that_is_taken(with_key):
    """The first live probe: as sent refused after running, the schema in response_schema refused at creation,
    the schema left out taken. The OpenAPI form of the schema in response_schema is the new variant that goes through."""
    error = ApiError(400, "400 INVALID_ARGUMENT. Invalid JSON payload received. Unknown name \"additional_properties\" at response_schema")

    def named(request):
        return "additionalProperties" in json.dumps(request.config.response_schema)

    service = BatchService(
        refuses=lambda request, model: "Request contains an invalid argument." if request.config.response_json_schema else None,
        create_error=lambda request, model: error if named(request) else None,
    )
    report, lines = probe(service)
    assert report.verdict("as_sent") == "refused" and report.outcomes[0].state == "JOB_STATE_SUCCEEDED"
    schema_field = next(o for o in report.outcomes if o.key == "schema_field")
    assert schema_field.verdict == "refused" and schema_field.state == "not created" and "additional_properties" in schema_field.error
    assert keys(report, "taken") == ["no_schema", "openapi", "documented", "bare"]
    text = "\n".join(lines)
    assert 'set the route options "schema" to "openapi" and "schema_field" to "response_schema"' in text
    assert 'set the route option "batch_form" to "documented"' in text


def test_the_openapi_variant_can_be_asked_for_by_itself(with_key):
    service = BatchService()
    report, _lines = probe(service, keys=("openapi",))
    assert [outcome.key for outcome in report.outcomes] == ["openapi"] and len(service.jobs) == 1
    assert service.request_of(report.outcomes[0]).config.response_schema is not None


def test_an_explicit_cache_the_route_uses_is_named_when_it_is_the_part_and_is_deleted_after(with_key):
    config = standard_route("flash-batch", max_output_tokens=8192, options={"cache": True})
    service = BatchService(refuses=lambda request, model: "cached content is not supported" if request.config.cached_content else None)
    report, lines = probe(service, route_config=config)
    assert "cache_off" in [outcome.key for outcome in report.outcomes] and "cache_on" not in [outcome.key for outcome in report.outcomes]
    assert report.verdict("as_sent") == "refused" and "cache_off" in keys(report, "taken")
    assert service.request_of(report.outcomes[0]).config.cached_content == "cachedContents/probe-1"
    assert service.request_of(report.outcomes[0]).config.system_instruction is None  # the rules are in the cache
    assert service.request_of(next(o for o in report.outcomes if o.key == "cache_off")).config.cached_content is None
    assert any('the route option "cache" to false' in line for line in lines)
    assert len(service.caches_made) == 1 and service.caches_made[0][2] == "600s"  # one cache for all the variants, short lived
    assert service.caches_deleted == ["cachedContents/probe-1"]


def test_a_cache_is_tried_only_when_asked_for_and_not_made_when_it_cannot_be(with_key):
    service = BatchService()
    report, _lines = probe(service, try_cache=True)
    assert "cache_on" in [outcome.key for outcome in report.outcomes] and len(service.caches_made) == 1
    assert service.request_of(next(o for o in report.outcomes if o.key == "cache_on")).config.cached_content == "cachedContents/probe-1"
    assert service.caches_deleted == ["cachedContents/probe-1"]

    broken = BatchService()
    client = broken.client()
    client.caches.create = lambda **kwargs: (_ for _ in ()).throw(ApiError(400, "400 INVALID_ARGUMENT. cached content is too small"))
    lines = []
    clock = Clock()
    report = probe_gemini_batch(
        "flash-batch", keys=("as_sent", "cache_on"), try_cache=True, client=client, printer=lines.append, now=lambda: NOW, clock=clock, sleep=clock.sleep
    )
    cache = next(o for o in report.outcomes if o.key == "cache_on")
    assert cache.verdict == "refused" and cache.error.startswith("the cache could not be made: ") and "too small" in cache.error
    assert report.verdict("as_sent") == "taken" and len(broken.jobs) == 1


def test_a_refusal_of_even_the_bare_request_points_at_the_model_and_a_model_that_takes_it(with_key):
    service = BatchService(refuses=lambda request, model: "Request contains an invalid argument." if model == MODEL else None)
    report, lines = probe(service, compare_models=["gemini-2.5-flash"])
    assert report.verdict("bare") == "refused" and report.verdict("bare:gemini-2.5-flash") == "taken"
    assert keys(report, "taken") == ["bare:gemini-2.5-flash"]
    assert service.jobs["batches/probe-" + str(len(service.jobs))]["model"] == "gemini-2.5-flash"
    sentence = next(line for line in lines if "Even the bare request was refused" in line)
    assert "does not take this model" in sentence and "It took the bare request on gemini-2.5-flash" in sentence


def test_a_batch_with_no_answer_when_the_wait_is_over_is_cancelled_and_not_counted_as_refused(with_key):
    service = BatchService(stalls=lambda request, model: request.config.system_instruction is None and request.config.response_json_schema is not None)
    report, lines = probe(service)
    stalled = report.outcomes[[outcome.key for outcome in report.outcomes].index("no_system")]
    assert stalled.verdict == "undecided" and stalled.state == "JOB_STATE_RUNNING" and stalled.seconds >= 60
    assert service.cancelled == [stalled.job]
    assert report.verdict("as_sent") == "taken" and keys(report, "refused") == []
    assert any("no answer after" in line and "was cancelled" in line for line in lines)


def test_what_is_said_while_waiting_names_the_states_of_the_batches_not_yet_done(with_key):
    service = BatchService(stalls=lambda request, model: True)
    report, lines = probe(service, keys=("as_sent", "bare"))
    assert keys(report, "undecided") == ["as_sent", "bare"] and len(service.cancelled) == 2
    assert any(line.startswith("[geminiprobe] still waiting for 2 batch(es): 2 JOB_STATE_RUNNING") for line in lines)
    assert "neither taken nor refused" in lines[-1]


def test_a_batch_the_service_will_not_even_create_shows_its_whole_error_and_the_rest_go_on(with_key):
    error = ApiError(400, "400 INVALID_ARGUMENT. {'error': {'message': 'response_json_schema is not allowed in a batch'}}")
    service = BatchService(create_error=lambda request, model: error if request.config.response_json_schema else None)
    report, lines = probe(service)
    first = report.outcomes[0]
    assert first.verdict == "refused" and first.state == "not created" and "response_json_schema is not allowed in a batch" in first.error
    assert keys(report, "taken") == ["no_schema", "schema_field", "openapi", "documented", "bare"]
    assert any("not allowed in a batch" in line for line in lines)


def test_the_probe_cancels_what_is_pending_and_deletes_its_cache_when_it_is_interrupted(with_key):
    config = standard_route("flash-batch", max_output_tokens=8192, options={"cache": True})
    service = BatchService(stalls=lambda request, model: True)

    def interrupted(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        probe(service, route_config=config, keys=("as_sent", "bare"), sleep=interrupted)
    assert sorted(service.cancelled) == sorted(service.jobs) and len(service.cancelled) == 2
    assert service.caches_deleted == ["cachedContents/probe-1"]


def test_the_probe_refuses_to_start_over_the_cap_for_a_closed_route_or_an_unknown_variant(with_key, monkeypatch):
    service = BatchService()
    report, lines = probe(service, cap_usd=0.01)
    assert report.refused and "over the $0.01 cap" in report.refused and service.jobs == {} and lines[-1].startswith("[geminiprobe] refused:")
    report, _lines = probe(service, keys=("as_sent", "nonsense"))
    assert report.refused and "nonsense" in report.refused and service.jobs == {}
    report, _lines = probe(service, keys=("as_sent",), prices=PriceTable.from_dict({"date": "2026-11-07", "models": {}}))
    assert report.refused and service.jobs == {}
    monkeypatch.delenv("GEMINI_API_KEY")
    report, _lines = probe(service)
    assert report.refused and "GEMINI_API_KEY" in report.refused and service.jobs == {}
    lines = []
    report = probe_gemini_batch("deepseek-sync", client=service.client(), printer=lines.append)
    assert report.refused and "deepseek" in report.refused


def test_the_command_line_runs_the_batch_probe_with_its_options(monkeypatch):
    seen = {}

    def fake(route, **kwargs):
        seen.update(route=route, **kwargs)
        return geminiprobe.BatchProbeReport(route=route, outcomes=[geminiprobe.BatchOutcome(key="as_sent", title="t", detail="d", verdict="taken")])

    monkeypatch.setattr(geminiprobe, "probe_gemini_batch", fake)
    code = main(["--route", "flash-batch", "--batch", "--only", "as_sent, bare", "--wait", "90", "--with-cache", "--compare-model", "gemini-2.5-flash", "--cap", "0.2"])
    assert code == 0
    assert seen["route"] == "flash-batch" and seen["keys"] == ["as_sent", "bare"] and seen["wait_seconds"] == 90
    assert seen["try_cache"] is True and seen["compare_models"] == ["gemini-2.5-flash"] and seen["cap_usd"] == 0.2
    monkeypatch.setattr(geminiprobe, "probe_gemini_batch", lambda route, **kwargs: geminiprobe.BatchProbeReport(route=route, refused="over the cap"))
    assert main(["--batch"]) == 2
    monkeypatch.setattr(
        geminiprobe, "probe_gemini_batch",
        lambda route, **kwargs: geminiprobe.BatchProbeReport(route=route, outcomes=[geminiprobe.BatchOutcome(key="as_sent", title="t", detail="d", verdict="refused")]),
    )
    assert main(["--batch"]) == 1
