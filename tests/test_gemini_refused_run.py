"""A run of the rule pool against a Gemini batch whose every item is refused with "3: Request contains an invalid argument."

The live run tried each of 35 calls again (28 more, all refused) and said nothing of why. The writer now sends the
calls of a batch refused as a whole once more, in the form the batch pages document (the schema in response_schema,
the metadata key `key`, no explicit cache); when that is refused too, or the route is set to stay in the first form,
the route stops once, with the provider's whole message in the run's notes.
"""

from __future__ import annotations

import json

from google.genai import types

from rps import ids
from rps import rules as pool
from rps.controller import Controller
from rps.events import replay
from rps.providers import GeminiBatchWriter
from rps.store import LocalStore
from rps.tasks import SpecLine, TaskSpec

from adapter_helpers import Namespace, Recorder
from harness import FakeNow
from helpers import ROUTE_CAPS, make_config
from test_controller_rules import conform

DETAILS = ['{"@type": "type.googleapis.com/google.rpc.BadRequest", "fieldViolations": [{"field": "response_json_schema"}]}']


def metadata_id(request):
    return request.metadata.get("custom_id") or request.metadata.get("key")


def answer_for(request):
    """The pool's worked answer for the item a request is about, as the writer is told to give it."""
    line = ids.parse_call_id(ids.call_id_from_custom_id(metadata_id(request)))[1]
    example = next(e for e in pool.examples() if e["item"]["ids"][0] == line)
    output = conform({"task_text": request.contents[0].parts[0].text}, example["output"]).output
    return types.GenerateContentResponse.model_validate(
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": output}]}, "finish_reason": "STOP"}],
            "usage_metadata": {"prompt_token_count": 6000, "candidates_token_count": 900, "total_token_count": 6900},
        }
    )


class BatchService:
    """A Gemini batch service that takes every batch and answers every item at once: refused, or with the worked answer.

    `refuses(request)` says which requests it refuses (all of them by default); `code` is the status it reports.
    """

    def __init__(self, refuses=None, code=3):
        self.refuses = refuses or (lambda request: True)
        self.code = code
        self.created = []

    def create(self, *, model, src, config=None):
        self.created.append(list(src))
        return types.BatchJob.model_validate({"name": f"batches/job-{len(self.created)}", "state": "JOB_STATE_PENDING"})

    def get(self, *, name):
        sent = self.created[int(name.rsplit("-", 1)[1]) - 1]
        items = []
        for request in sent:
            if self.refuses(request):
                error = types.JobError(code=self.code, message="Request contains an invalid argument.", details=DETAILS)
                items.append(types.InlinedResponse(error=error, metadata=request.metadata))
            else:
                items.append(types.InlinedResponse(response=answer_for(request), metadata=request.metadata))
        return types.BatchJob.model_validate(
            {"name": name, "state": "JOB_STATE_SUCCEEDED", "dest": {"inlined_responses": [item.model_dump() for item in items]}}
        )


def run_pool(tmp_path, monkeypatch, service, **options):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    config = make_config(
        routes={
            "flash-batch": {
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "kind": "batch",
                "effort": "medium",
                "budget_usd": 5.0,
                "purpose_caps": dict(ROUTE_CAPS),
                "max_output_tokens": 8192,
                "options": options,
            }
        },
        rules={"package": "rps.rules"},
    )
    client = Namespace(batches=Namespace(create=service.create, get=service.get, list=Recorder([])))
    writer = GeminiBatchWriter("flash-batch", config.routes["flash-batch"], client=client)
    store = LocalStore(tmp_path / "state", fsync=False)
    printed = []
    controller = Controller(
        config, store, {"flash-batch": writer}, instance="20261107T120000Z-test", now=FakeNow(), printer=printed.append, sleep=lambda s: None
    )
    items = [e["item"] for e in pool.examples()]
    lines = tuple(SpecLine(item["ids"][0], item, item_id=item["ids"][0], license=item["license"]) for item in items)
    controller.add_task(TaskSpec(source="photos", batch="pool", route="flash-batch", purpose="production", variant="", lines=lines))
    return controller, controller.run(), store, printed


def events_of(store, kind):
    return [event for event in replay(store).events if event.kind == kind]


def test_a_batch_every_item_of_which_is_refused_is_sent_again_in_the_documented_form_and_the_route_stops_when_that_is_refused_too(
    tmp_path, monkeypatch
):
    service = BatchService()
    _controller, summary, store, printed = run_pool(tmp_path, monkeypatch, service)
    assert summary.reason == "idle" and len(service.created) == 2 and [len(batch) for batch in service.created] == [3, 3]
    first, second = service.created
    assert all(request.config.response_json_schema is not None and "custom_id" in request.metadata for request in first)
    assert all(
        request.config.response_schema is not None and request.config.response_json_schema is None
        and "additionalProperties" not in json.dumps(request.config.response_schema)  # the field takes none (the OpenAPI form)
        and list(request.metadata) == ["key"] and request.config.cached_content is None
        for request in second
    )
    failed = events_of(store, "call_failed")
    assert [event.payload["kind"] for event in failed] == ["provider_error"] * 3 + ["bad_request"] * 3
    assert [event.payload["retryable"] for event in failed] == [True] * 3 + [False] * 3
    assert all(event.payload["error"].startswith("3: Request contains an invalid argument. | details: ") for event in failed)
    assert all("fieldViolations" in event.payload["error"] for event in failed)  # the provider's details are kept
    assert all("sent again in the form the batch pages document" in event.payload["error"] for event in failed[:3])
    assert all(
        "python -m rps.geminiprobe --batch --route flash-batch" in event.payload["error"] for event in failed[3:]
    )  # the second refusal says where to look
    assert [event.payload["attempt"] for event in events_of(store, "call_prepared")] == [1, 1, 1, 2, 2, 2]
    [halted] = events_of(store, "route_halted")
    assert halted.payload["route"] == "flash-batch" and halted.payload["kind"] == "bad_request"
    assert halted.payload["reason"].startswith("3: Request contains an invalid argument.") and "fieldViolations" in halted.payload["reason"]
    assert any("route flash-batch stopped for this run (bad_request)" in problem for problem in summary.problems)
    assert any("fieldViolations" in line for line in printed if "stopped for this run" in line)


def test_where_the_first_form_is_refused_and_the_documented_form_is_taken_the_run_goes_on(tmp_path, monkeypatch):
    service = BatchService(refuses=lambda request: request.config.response_json_schema is not None)
    _controller, summary, store, _printed = run_pool(tmp_path, monkeypatch, service)
    assert len(service.created) == 2 and summary.reason == "idle"
    failed = events_of(store, "call_failed")
    assert [event.payload["kind"] for event in failed] == ["provider_error"] * 3 and all(event.payload["retryable"] for event in failed)
    assert all(not event.payload["billed"] for event in failed)  # a refused request costs nothing, so no advice goes with the retry
    returned = events_of(store, "call_returned")
    assert len(returned) == 3 and {event.payload["call_id"].rsplit(":", 1)[1] for event in returned} == {"2"}
    assert events_of(store, "route_halted") == [] and summary.problems == []
    assert sum(len(event.payload["rows"]) for event in returned) == sum(len(e["output"]["rows"]) for e in pool.examples())


def test_calls_prepared_after_the_step_down_are_all_in_the_documented_form(tmp_path, monkeypatch):
    service = BatchService(refuses=lambda request: request.config.response_json_schema is not None)
    controller, _summary, _store, _printed = run_pool(tmp_path, monkeypatch, service)
    writer = controller.writers["flash-batch"]
    assert writer._form() == 1  # for the rest of the run
    task = next(iter(controller.state.tasks.values()))
    spec, _size = controller._call_spec(task, next(iter(task.lines.values())), 3, ())
    [request] = writer.prepare([spec])
    assert request.payload["form"] == 1 and request.payload["request"].config.response_schema is not None


def test_a_route_set_to_stay_in_the_first_form_is_not_sent_again(tmp_path, monkeypatch):
    service = BatchService()
    _controller, summary, store, _printed = run_pool(tmp_path, monkeypatch, service, batch_form="full")
    assert len(service.created) == 1 and summary.reason == "idle"
    failed = events_of(store, "call_failed")
    assert {event.payload["kind"] for event in failed} == {"bad_request"} and not any(event.payload["retryable"] for event in failed)
    assert all('"batch_form" is "full"' in event.payload["error"] for event in failed)
    assert [event.payload["attempt"] for event in events_of(store, "call_prepared")] == [1, 1, 1]
    assert len(events_of(store, "route_halted")) == 1


def test_a_route_set_to_the_documented_form_sends_it_from_the_start(tmp_path, monkeypatch):
    service = BatchService(refuses=lambda request: request.config.response_json_schema is not None)
    _controller, summary, store, _printed = run_pool(tmp_path, monkeypatch, service, batch_form="documented")
    assert len(service.created) == 1 and summary.reason == "idle" and summary.problems == []
    assert all(list(request.metadata) == ["key"] and request.config.response_schema is not None for request in service.created[0])
    assert events_of(store, "call_failed") == [] and len(events_of(store, "call_returned")) == 3
    [submitted] = events_of(store, "batch_submitted")
    assert submitted.payload["handle"]["data"]["form"] == 1  # the handle says what form the batch was sent in


def test_a_batch_in_which_only_some_items_are_refused_is_not_read_as_refused_as_a_whole(tmp_path, monkeypatch):
    service = BatchService(refuses=lambda request: metadata_id(request) != metadata_id(service.created[0][0]))
    _controller, summary, store, _printed = run_pool(tmp_path, monkeypatch, service)
    assert len(service.created) == 1 and summary.reason == "idle"  # no second form: these are the requests' own faults
    failed = events_of(store, "call_failed")
    assert len(failed) == 2 and {event.payload["kind"] for event in failed} == {"bad_request"}
    assert all("sent again" not in event.payload["error"] for event in failed)
    assert len(events_of(store, "call_returned")) == 1 and len(events_of(store, "route_halted")) == 1


def test_the_same_refusal_read_as_a_passing_trouble_of_the_providers_would_have_been_tried_again(tmp_path, monkeypatch):
    """The codes that are the provider's own trouble are still retried (here 14, unavailable)."""
    service = BatchService(code=14)
    _controller, _summary, store, _printed = run_pool(tmp_path, monkeypatch, service)
    failed = events_of(store, "call_failed")
    assert {event.payload["kind"] for event in failed} == {"provider_error"} and all(event.payload["retryable"] for event in failed)
    assert not events_of(store, "route_halted")
    assert sorted({event.payload["attempt"] for event in events_of(store, "call_prepared")}) == [1, 2]
    assert all("sent again in the form" not in event.payload["error"] for event in failed)
