"""The controller finding a batch again after a crash, with the real Anthropic writer on a pretend Message Batches service.

A crash between the provider taking a batch and the controller recording its id leaves a batch nobody
holds a handle for. Anthropic cannot be asked what a batch holds, so the writer matches by size and
creation time and the controller checks at collection that the batch really held its calls.
"""

from __future__ import annotations

import json

import pytest
from anthropic.types import Message
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse

from rps.controller import _parse_ts
from rps.providers import AnthropicBatchWriter, FakeWriter, SimulatedCrash

from adapter_helpers import ApiError, Namespace, rules
from harness import World, task_spec
from helpers import ROUTE_CAPS, make_config

MODEL = "claude-haiku-5-5"
ROW = json.dumps({"id": "row", "thoughts": ["a thought"]})


@pytest.fixture(autouse=True)
def an_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key-for-the-tests-only")


class Service:
    """A pretend Message Batches service, shared by successive controllers the way the real one is."""

    def __init__(self, clock):
        self.clock = clock
        self.batches = {}  # batch id -> {"custom_ids": [...], "created": datetime}
        self.created = 0  # how many batches the controllers asked for
        self.before_create = None  # raised before the batch exists
        self.after_create = None  # raised after the batch exists, as if the answer never came back
        self.client = Namespace(
            messages=Namespace(
                batches=Namespace(create=self.create, list=self.list, retrieve=self.retrieve, results=self.results)
            )
        )

    def add_stranger(self, size):
        """Somebody else's batch of `size` requests, made now."""
        batch_id = f"msgbatch_{len(self.batches) + 1:03d}"
        self.batches[batch_id] = {"custom_ids": [f"stranger-{n}" for n in range(size)], "created": self.clock()}
        return batch_id

    def create(self, *, requests):
        if self.before_create is not None:
            raise self.before_create
        self.created += 1
        batch_id = f"msgbatch_{len(self.batches) + 1:03d}"
        self.batches[batch_id] = {"custom_ids": [entry["custom_id"] for entry in requests], "created": self.clock()}
        if self.after_create is not None:
            raise self.after_create
        return self.batch(batch_id)

    def list(self, **_kwargs):
        newest_first = sorted(self.batches, key=lambda batch_id: self.batches[batch_id]["created"], reverse=True)
        return iter([self.batch(batch_id) for batch_id in newest_first])

    def retrieve(self, batch_id):
        return self.batch(batch_id, ended=True)

    def results(self, batch_id):
        message = Message.model_validate(
            {
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": ROW}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )
        for custom_id in self.batches[batch_id]["custom_ids"]:
            yield MessageBatchIndividualResponse.model_validate(
                {"custom_id": custom_id, "result": {"type": "succeeded", "message": message.model_dump()}}
            )

    def batch(self, batch_id, ended=False):
        size = len(self.batches[batch_id]["custom_ids"])
        stamp = self.batches[batch_id]["created"].strftime("%Y-%m-%dT%H:%M:%SZ")
        return MessageBatch.model_validate(
            {
                "id": batch_id,
                "type": "message_batch",
                "processing_status": "ended" if ended else "in_progress",
                "request_counts": {
                    "processing": 0 if ended else size,
                    "succeeded": size if ended else 0,
                    "errored": 0,
                    "canceled": 0,
                    "expired": 0,
                },
                "created_at": stamp,
                "expires_at": stamp,
                "ended_at": None,
                "archived_at": None,
                "cancel_initiated_at": None,
                "results_url": None,
            }
        )


def bench(tmp_path):
    config = make_config(
        routes={
            "fake-batch": {
                "provider": "anthropic",
                "model": MODEL,
                "kind": "batch",
                "effort": "medium",
                "budget_usd": 10.0,
                "purpose_caps": dict(ROUTE_CAPS),
            }
        }
    )

    def writers(name, route, *, provider, behavior, polls_until_ended):
        return AnthropicBatchWriter(name, route, client=service.client)

    world = World(tmp_path, config=config, writer_class=writers)
    service = Service(world.now)
    return world, service


def crash_run(world, lines=("001", "002", "003")):
    """A run that dies at once, leaving the calls prepared and the first batch where the service has put it."""
    controller = world.controller(rules=rules())
    controller.add_task(task_spec(lines))
    with pytest.raises(SimulatedCrash):
        controller.run()


def steps(controller, count=5):
    for _ in range(count):
        controller.step()


def test_a_batch_a_crash_left_unrecorded_is_adopted_and_nothing_is_sent_again(tmp_path):
    world, service = bench(tmp_path)
    service.after_create = SimulatedCrash("the process died after the service took the batch")
    crash_run(world)
    assert service.created == 1 and not world.events_of("batch_submitted")
    service.after_create = None
    second = world.controller(rules=rules())
    steps(second)
    [submitted] = world.events_of("batch_submitted")
    assert submitted.payload["recovered"] is True and submitted.payload["handle_id"] == "msgbatch_001"
    assert submitted.payload["handle"]["data"] == {"batch_id": "msgbatch_001", "adopted": True}
    assert service.created == 1  # nothing was sent again
    assert len(world.events_of("call_returned")) == 3 and not world.events_of("call_failed")


def test_with_no_candidate_the_calls_are_sent_again(tmp_path):
    world, service = bench(tmp_path)
    service.before_create = SimulatedCrash("the process died before the service took the batch")
    crash_run(world)
    service.before_create = None
    second = world.controller(rules=rules())
    steps(second)
    [submitted] = world.events_of("batch_submitted")
    assert submitted.payload["recovered"] is False and service.created == 1
    assert len(world.events_of("call_returned")) == 3


def test_a_strangers_batch_of_the_same_size_is_found_out_at_collection_and_the_calls_are_sent_again(tmp_path):
    world, service = bench(tmp_path)
    service.before_create = SimulatedCrash("the process died before the service took the batch")
    crash_run(world)
    service.before_create = None
    stranger = service.add_stranger(3)  # made after the calls were prepared, the same size as their group
    second = world.controller(rules=rules())
    steps(second, 6)
    submitted = world.events_of("batch_submitted")
    assert [(e.payload["handle_id"], e.payload["recovered"]) for e in submitted] == [(stranger, True), ("msgbatch_002", False)]
    failed = world.events_of("call_failed")
    assert [e.payload["kind"] for e in failed] == ["batch_mismatch"] * 3
    assert all(e.payload["retryable"] and not e.payload["billed"] for e in failed)
    assert all("did not hold these calls" in e.payload["error"] for e in failed)
    assert service.created == 1  # the calls went out once, in a batch of their own
    returned = world.events_of("call_returned")
    assert len(returned) == 3 and {second.state.calls[e.payload["call_id"]].attempt for e in returned} == {2}
    assert any("did not hold its calls" in problem for problem in second.problems)
    # the stranger's batch is recorded, so it is never taken for anybody's calls again
    assert stranger in second.state.batches and second.state.batches[stranger].call_ids != second.state.batches["msgbatch_002"].call_ids


def test_a_create_that_failed_on_the_way_back_is_found_again_in_the_same_run(tmp_path):
    world, service = bench(tmp_path)
    service.after_create = ApiError(500, "bad gateway")
    controller = world.controller(rules=rules())
    controller.add_task(task_spec(("001", "002", "003")))
    controller.step()
    assert service.created == 1 and not world.events_of("batch_submitted")  # the service has it, the controller does not
    service.after_create = None
    steps(controller, 4)
    [submitted] = world.events_of("batch_submitted")
    assert submitted.payload["recovered"] is True and submitted.payload["handle_id"] == "msgbatch_001"
    assert service.created == 1  # no second batch for the same calls
    assert len(world.events_of("call_returned")) == 3


def test_a_create_that_failed_and_left_nothing_behind_is_simply_tried_again(tmp_path):
    world, service = bench(tmp_path)
    service.before_create = ApiError(503, "unavailable")
    controller = world.controller(rules=rules())
    controller.add_task(task_spec(("001", "002")))
    controller.step()
    service.before_create = None
    steps(controller, 4)
    [submitted] = world.events_of("batch_submitted")
    assert submitted.payload["recovered"] is False and service.created == 1
    assert len(world.events_of("call_returned")) == 2


class Watching(FakeWriter):
    """A fake writer that notes what the controller tells it when it looks for earlier work."""

    seen: list = []

    def recover(self, requests, *, since=None, recorded=()):
        Watching.seen.append((since, frozenset(recorded), [request.call_id for request in requests]))
        return super().recover(requests, since=since, recorded=recorded)


def test_the_writer_is_told_when_the_calls_were_prepared_and_which_batches_the_controller_holds(tmp_path):
    Watching.seen = []
    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003"), batch="1"))
    first.step()  # the first batch is submitted and recorded
    [held] = world.events_of("batch_submitted")
    first.add_task(task_spec(("004", "005"), batch="2"))
    world.provider.crash_after_submit = True
    with pytest.raises(SimulatedCrash):
        first.run()
    world.provider.crash_after_submit = False
    world.writer_class = Watching
    second = world.controller()
    second.start()
    waiting = [call for call in second.state.calls.values() if call.status == "prepared"]
    assert len(waiting) == 2
    steps(second, 2)
    since, recorded, asked = Watching.seen[0]
    assert since == min(_parse_ts(call.prepared_ts) for call in waiting)
    assert recorded == frozenset({held.payload["handle_id"]}) and sorted(asked) == sorted(call.call_id for call in waiting)
    assert world.provider.submit_count == 2  # the second batch was found again, not sent a third time
