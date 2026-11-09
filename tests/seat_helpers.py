"""Builders for the tests of the API review seat: packets in a store, a fake Anthropic batch client and its answers.

The results are the SDK's own types built from recorded-response-shaped data, so a change in the SDK's field names
fails here and not in a run.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from anthropic.types import Message
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse

from rps import ids, layout, rules
from rps.layout import controller_actor
from rps.ledger import Usage
from rps.review.api_seat import ASK_LINE, ApiSeat, SeatConfig, cost_of
from rps.review.packets import build_packet, parse_verdict_lines, publish_packet
from rps.store import ActorStore

from adapter_helpers import ApiError, Namespace
from helpers import Builder

MODEL = "claude-haiku-5-5"
REVIEWER = "claude-haiku"
TASK = ids.task_id("photos", "1", {"001": "x"})
LINE = {"line_id": "001", "item_id": "img-001", "spec": "a cat"}


class Clock:
    """UTC time that moves only when a test moves it."""

    def __init__(self) -> None:
        self.current = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


# ---------------------------------------------------------------------------- packets


def publish(
    remote,
    *,
    reviewer: str = REVIEWER,
    rows: int = 3,
    line: str = "001",
    brief_version: str | None = None,
    pictures: bool = False,
    texts: list[str] | None = None,
):
    """Publish a packet of `rows` rows to a store the way the controller does, and return it.

    Each row carries its writer label (the packet's blind view drops it) and a `source.model` the view blanks. Packets
    built with different `line` values have different rows and so different ids.
    """
    builder = Builder()
    call = ids.call_id(TASK, line, 1)
    stored = [
        builder.row(call, index, text=(texts[index] if texts else f"thought {index}"), source_block={"model": "fake-model", "kind": "photo"})
        for index in range(rows)
    ]
    packet = build_packet(
        packet_id=ids.packet_id([f"{reviewer}:{row['row_id']}" for row in stored]),
        reviewer_id=reviewer,
        brief_version=brief_version if brief_version is not None else rules.rules_version(),
        created="2026-11-08T11:00:00.000000Z",
        rows=[(row, {**LINE, "line_id": line}, b"picture-%d" % number if pictures else None, "jpg") for number, row in enumerate(stored)],
    )
    publish_packet(ActorStore(remote, controller_actor("20261108T110000Z-aaaa")), packet)
    return packet


# ---------------------------------------------------------------------------- answers


def verdict_text(verdict: str = "keep", reasons: tuple[str, ...] | list[str] = (), notes: str = "reads right") -> str:
    return json.dumps({"verdict": verdict, "reasons": list(reasons), "notes": notes})


def message(text: str = "", *, stop: str = "end_turn", usage: dict[str, Any] | None = None, content=None, stop_details=None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 100, "output_tokens": 50},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return Message.model_validate(body).model_dump()


def succeeded(custom_id: str, text: str, **fields: Any) -> MessageBatchIndividualResponse:
    return MessageBatchIndividualResponse.model_validate(
        {"custom_id": custom_id, "result": {"type": "succeeded", "message": message(text, **fields)}}
    )


def errored(custom_id: str, error_type: str = "overloaded_error", text: str = "trouble") -> MessageBatchIndividualResponse:
    return MessageBatchIndividualResponse.model_validate(
        {
            "custom_id": custom_id,
            "result": {"type": "errored", "error": {"type": "error", "request_id": "req_1", "error": {"type": error_type, "message": text}}},
        }
    )


def canceled(custom_id: str) -> MessageBatchIndividualResponse:
    return MessageBatchIndividualResponse.model_validate({"custom_id": custom_id, "result": {"type": "canceled"}})


def expired(custom_id: str) -> MessageBatchIndividualResponse:
    return MessageBatchIndividualResponse.model_validate({"custom_id": custom_id, "result": {"type": "expired"}})


def asked_row(request: dict[str, Any]) -> dict[str, Any]:
    """The row view a request carries: the JSON before the line that says what to return."""
    text = request["params"]["messages"][0]["content"][0]["text"]
    assert text.endswith("\n\n" + ASK_LINE)
    return json.loads(text[: -len("\n\n" + ASK_LINE)])


def keep_all(request: dict[str, Any]) -> str:
    return verdict_text("keep")


class FakeBatches:
    """`messages.batches` of a fake client.

    `answer(request)` says what the model answered for a request: the answer text (a succeeded result with default usage),
    or a ready result object built with `succeeded`, `errored`, `canceled` or `expired` (called with the request). Batches
    stay in progress until a test calls `end`, and their results come back in reverse order.
    """

    def __init__(self, clock: Clock, answer: Callable[[dict[str, Any]], Any] = keep_all) -> None:
        self.clock = clock
        self.answer = answer
        self.batches: dict[str, dict[str, Any]] = {}
        self.fail_create: Exception | None = None  # raised by create, before any batch is made
        self.lose_reply: Exception | None = None  # raised by create after the batch was made (the reply was lost)
        self.unknown: set[str] = set()  # batch ids the provider says it does not know
        self.results_error: Exception | None = None
        self.retrieve_error: Exception | None = None
        self.extra_items: list[Any] = []  # results that belong to no request of ours
        self.auto_end = False  # a batch is ended the moment it is made
        self.listed = 0

    @property
    def sent(self) -> list[dict[str, Any]]:
        """Every request ever sent, in order."""
        return [request for batch in self.batches.values() for request in batch["requests"]]

    def _object(self, batch_id: str) -> MessageBatch:
        batch = self.batches[batch_id]
        total = len(batch["requests"])
        ended = batch["status"] == "ended"
        return MessageBatch.model_validate(
            {
                "id": batch_id,
                "type": "message_batch",
                "processing_status": batch["status"],
                "request_counts": {"processing": 0 if ended else total, "succeeded": total if ended else 0, "errored": 0, "canceled": 0, "expired": 0},
                "created_at": batch["created"].isoformat().replace("+00:00", "Z"),
                "expires_at": (batch["created"] + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                "ended_at": None,
                "archived_at": None,
                "cancel_initiated_at": None,
                "results_url": None,
            }
        )

    def create(self, *, requests):
        if self.fail_create is not None:
            raise self.fail_create
        batch_id = f"msgbatch_{len(self.batches) + 1:04d}"
        self.batches[batch_id] = {"requests": list(requests), "created": self.clock(), "status": "ended" if self.auto_end else "in_progress"}
        if self.lose_reply is not None:
            raise self.lose_reply
        return self._object(batch_id)

    def retrieve(self, batch_id):
        if self.retrieve_error is not None:
            raise self.retrieve_error
        if batch_id in self.unknown or batch_id not in self.batches:
            raise ApiError(404, "not found")
        return self._object(batch_id)

    def list(self, limit=None):
        self.listed += 1
        newest_first = sorted(self.batches, key=lambda key: self.batches[key]["created"], reverse=True)
        return iter([self._object(key) for key in newest_first])

    def results(self, batch_id):
        if self.results_error is not None:
            raise self.results_error
        items = []
        for request in self.batches[batch_id]["requests"]:
            answer = self.answer(request)
            if isinstance(answer, str):
                answer = succeeded(request["custom_id"], answer)
            items.append(answer)
        return iter(self.extra_items + list(reversed(items)))

    def end(self, batch_id: str | None = None) -> None:
        """Mark a batch (by default every one in progress) as ended."""
        for key, batch in self.batches.items():
            if (batch_id is None or key == batch_id) and batch["status"] == "in_progress":
                batch["status"] = "ended"


class FakeAnthropic:
    """A stand-in for the Anthropic client: only `messages.batches`."""

    def __init__(self, clock: Clock | None = None, answer: Callable[[dict[str, Any]], Any] = keep_all) -> None:
        self.clock = clock or Clock()
        self.batches = FakeBatches(self.clock, answer)
        self.messages = Namespace(batches=self.batches)


# ---------------------------------------------------------------------------- seats


def seat_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"reviewer_id": REVIEWER, "model": MODEL, "budget_usd": 5.0, "poll_seconds": 0}
    config.update(overrides)
    return config


def make_seat(remote, tmp_path, api: FakeAnthropic, *, clock: Clock | None = None, **overrides: Any):
    """A seat on a store, with its printed lines collected. Returns (seat, printed lines)."""
    printed: list[str] = []
    seat = ApiSeat(
        seat_config(**overrides),
        remote,
        client=api,
        work_dir=tmp_path / "seat-work",
        printer=printed.append,
        now=clock or api.clock,
        fsync=False,
    )
    return seat, printed


# ---------------------------------------------------------------------------- reading what a seat left behind


def verdict_file(remote, packet, reviewer: str = REVIEWER):
    """The verdicts a reviewer pushed for a packet, as parsed lines (None when there is no file)."""
    path = layout.verdicts_path(reviewer, packet.packet_id)
    if not remote.exists(path):
        return None
    lines, problems = parse_verdict_lines(remote.read_text(path))
    assert problems == []
    return lines


def ledger_records(remote, kind: str | None = None, reviewer: str = REVIEWER):
    """The seat's ledger records of a kind (all of them without one)."""
    path = f"review/{reviewer}/seat/ledger.jsonl"
    if not remote.exists(path):
        return []
    records, problems = parse_verdict_lines(remote.read_text(path))
    assert problems == []
    return [record for record in records if kind is None or record["kind"] == kind]


def run_until_quiet(seat, api, limit: int = 12) -> int:
    """Step the seat, ending every batch it sends, until a pass does nothing. Returns the number of passes."""
    for number in range(1, limit + 1):
        api.batches.end()
        result = seat.step()
        if not (result.settled or result.answered or result.sent):
            return number
    raise AssertionError("the seat kept working")


def attempt_of(request: dict[str, Any]) -> int:
    """Which try at its row a request is (the attempt in its custom id)."""
    return ids.parse_call_id(ids.call_id_from_custom_id(request["custom_id"]))[2]


def row_of(request: dict[str, Any]) -> str:
    return asked_row(request)["row_id"]


def packet_estimate(seat, remote, packet) -> float:
    """What the seat estimates a packet to cost: its first requests, added up."""
    document = json.loads(remote.read_text(layout.packet_json_path(REVIEWER, packet.packet_id)))
    return sum(seat._request(packet.packet_id, place, entry, 1).usd for place, entry in enumerate(document["rows"], start=1))


# What a request costs at the fake's default usage (100 input and 50 output tokens), at the Haiku batch rates.
PER_REQUEST = cost_of(Usage(input_tokens=100, output_tokens=50), SeatConfig.from_dict(seat_config()).prices[MODEL].batch_rates())
