"""Small builders shared by the tests."""

from __future__ import annotations

from typing import Any

from rps import ids
from rps.config import Config
from rps.events import Event
from rps.provenance import SourceLabel, WriterLabel, stamp_row

ROUTE_CAPS = {"test": 1.0, "pricing": 0.50, "probe": 0.25, "rebound": 2.0, "production": 6.0}


def make_config(**overrides: Any) -> Config:
    """A config with one fake batch route and generous defaults."""
    data: dict[str, Any] = {
        "routes": {
            "fake-batch": {
                "provider": "fake",
                "model": "fake-model",
                "kind": "batch",
                "effort": "low",
                "budget_usd": 10.0,
                "purpose_caps": dict(ROUTE_CAPS),
            }
        },
        "sources": {
            "photos": {"image_size": 64, "license": "CC BY-NC", "dataset": "photos", "image_class": "public_common"}
        },
        "poll_seconds": 0,
        "review": {
            "packet_size": 3,
            "reviewers": [
                {"id": "rev-a", "family": "alpha"},
                {"id": "rev-b", "family": "beta"},
                {"id": "rev-c", "family": "gamma"},
            ],
            "provider_families": {"fake": "writer-family"},
            "policy": {"audit_fraction": 0.0},
        },
    }
    data.update(overrides)
    return Config.from_dict(data)


class Builder:
    """Builds a list of events with increasing timestamps."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def add(self, kind: str, payload: dict[str, Any]) -> Event:
        stamp = f"2026-11-07T09:{len(self.events) // 60:02d}:{len(self.events) % 60:02d}.000000Z"
        event = Event(ts=stamp, actor="controller-test", kind=kind, payload=payload)
        self.events.append(event)
        return event

    def task(self, *, batch: str = "1", lines: dict[str, Any] | None = None, route: str = "fake-batch", purpose: str = "production") -> str:
        lines = lines or {"001": "a cat", "002": "a dog"}
        task_id = ids.task_id("photos", batch, lines)
        self.add(
            "task_created",
            {
                "task_id": task_id,
                "source": "photos",
                "batch": batch,
                "variant": "",
                "route": route,
                "purpose": purpose,
                "effort": "low",
                "pool_version": "1",
                "lines": [
                    {"line_id": line_id, "spec": spec, "item_id": f"img-{line_id}", "image": None, "license": "CC BY-NC"}
                    for line_id, spec in sorted(lines.items())
                ],
            },
        )
        return task_id

    def prepare(self, task_id: str, line: str, attempt: int = 1, *, route: str = "fake-batch", purpose: str = "production", est: float = 0.01, feedback=None, retry_of=None, repair_of=None) -> str:
        call_id = ids.call_id(task_id, line, attempt)
        payload = {
            "call_id": call_id,
            "task_id": task_id,
            "line": line,
            "attempt": attempt,
            "route": route,
            "purpose": purpose,
            "est_usd": est,
            "est_input_tokens": 100,
            "max_output_tokens": 200,
            "image_size": None,
            "feedback": feedback or [],
            "retry_of": retry_of,
        }
        if repair_of is not None:  # a call of the repair step says which row it writes again
            payload["repair_of"] = repair_of
        self.add("call_prepared", payload)
        return call_id

    def submit(self, call_ids: list[str], handle_id: str = "batch-1", route: str = "fake-batch") -> None:
        self.add(
            "batch_submitted",
            {"handle_id": handle_id, "handle": {"handle_id": handle_id}, "route": route, "call_ids": call_ids},
        )

    def row(
        self,
        call_id: str,
        index: int = 0,
        *,
        text: str = "thought",
        provider: str = "fake",
        model: str = "fake-model",
        source_block: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id, line, _ = ids.parse_call_id(call_id)
        writer = WriterLabel(
            provider=provider, model=model, route="batch", effort="low", date="2026-11-07",
            task_id=task_id, pool_version="1", image_size=None,
        )
        source = SourceLabel(dataset="photos", item_id=f"img-{line}", license="CC BY-NC")
        body: dict[str, Any] = {"id": f"{line}-{index}", "thoughts": [text]}
        if source_block is not None:
            body["source"] = source_block
        return stamp_row(body, row_id=ids.row_id(call_id, index), writer=writer, source=source)

    def returned(self, call_id: str, *, rows: int = 1, usd: float = 0.005, skips=None, handle_id: str = "batch-1", flags=None) -> list[str]:
        records = [self.row(call_id, index) for index in range(rows)]
        self.add(
            "call_returned",
            {
                "call_id": call_id,
                "handle_id": handle_id,
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "usd": usd,
                "price_table_date": "2026-11-06",
                "minutes": 0.5,
                "rows": records,
                "row_flags": flags or {},
                "skips": skips or [],
            },
        )
        return [record["row_id"] for record in records]

    def failed(self, call_id: str, *, error: str = "overloaded", retryable: bool = True, billed: bool = False, usd: float = 0.0, handle_id: str = "batch-1") -> None:
        self.add(
            "call_failed",
            {
                "call_id": call_id,
                "handle_id": handle_id,
                "error": error,
                "retryable": retryable,
                "billed": billed,
                "usage": {},
                "usd": usd,
                "price_table_date": "2026-11-06",
                "minutes": 0.1,
            },
        )

    def assessed(self, row_id: str, call_id: str, *, passed: bool = True, flags=None, errors=None) -> None:
        self.add(
            "row_assessed",
            {"row_id": row_id, "call_id": call_id, "passed": passed, "flags": flags or [], "errors": errors or []},
        )
