"""A run of the rule pool against a Gemini batch whose every item is refused with "3: Request contains an invalid argument."

The live run tried each of 35 calls again (28 more, all refused) and said nothing of why. The route now stops
once, with the provider's whole message in the run's notes, and no call is sent a second time.
"""

from __future__ import annotations

from google.genai import types

from rps import rules as pool
from rps.controller import Controller
from rps.events import replay
from rps.providers import GeminiBatchWriter
from rps.store import LocalStore
from rps.tasks import SpecLine, TaskSpec

from adapter_helpers import Namespace, Recorder
from harness import FakeNow
from helpers import ROUTE_CAPS, make_config

DETAILS = ['{"@type": "type.googleapis.com/google.rpc.BadRequest", "fieldViolations": [{"field": "response_json_schema"}]}']


class RefusingService:
    """A Gemini batch service that takes every batch and then refuses every item in it."""

    def __init__(self, code=3):
        self.code = code
        self.created = []

    def create(self, *, model, src, config=None):
        self.created.append([item.metadata["custom_id"] for item in src])
        return types.BatchJob.model_validate({"name": f"batches/job-{len(self.created)}", "state": "JOB_STATE_PENDING"})

    def get(self, *, name):
        sent = self.created[int(name.rsplit("-", 1)[1]) - 1]
        refused = [
            types.InlinedResponse(
                error=types.JobError(code=self.code, message="Request contains an invalid argument.", details=DETAILS),
                metadata={"custom_id": custom_id},
            )
            for custom_id in sent
        ]
        return types.BatchJob.model_validate(
            {"name": name, "state": "JOB_STATE_SUCCEEDED", "dest": {"inlined_responses": [item.model_dump() for item in refused]}}
        )


def run_pool(tmp_path, monkeypatch, service):
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


def test_a_batch_every_item_of_which_is_refused_stops_the_route_once_and_nothing_is_tried_again(tmp_path, monkeypatch):
    service = RefusingService(code=3)
    _controller, summary, store, printed = run_pool(tmp_path, monkeypatch, service)
    assert summary.reason == "idle" and len(service.created) == 1 and len(service.created[0]) == 3
    failed = events_of(store, "call_failed")
    assert len(failed) == 3
    assert {event.payload["kind"] for event in failed} == {"bad_request"} and not any(event.payload["retryable"] for event in failed)
    assert all(event.payload["error"].startswith("3: Request contains an invalid argument. | details: ") for event in failed)
    assert all("fieldViolations" in event.payload["error"] for event in failed)  # the provider's details are kept
    assert [event.payload["attempt"] for event in events_of(store, "call_prepared")] == [1, 1, 1]  # no second attempt
    [halted] = events_of(store, "route_halted")
    assert halted.payload["route"] == "flash-batch" and halted.payload["kind"] == "bad_request"
    assert halted.payload["reason"].startswith("3: Request contains an invalid argument.") and "fieldViolations" in halted.payload["reason"]
    assert any("route flash-batch stopped for this run (bad_request)" in problem for problem in summary.problems)
    assert any("fieldViolations" in line for line in printed if "stopped for this run" in line)


def test_the_same_refusal_read_as_a_passing_trouble_of_the_providers_would_have_been_tried_again(tmp_path, monkeypatch):
    """The codes that are the provider's own trouble are still retried (here 14, unavailable)."""
    service = RefusingService(code=14)
    _controller, _summary, store, _printed = run_pool(tmp_path, monkeypatch, service)
    failed = events_of(store, "call_failed")
    assert {event.payload["kind"] for event in failed} == {"provider_error"} and all(event.payload["retryable"] for event in failed)
    assert not events_of(store, "route_halted")
    assert sorted({event.payload["attempt"] for event in events_of(store, "call_prepared")}) == [1, 2]
