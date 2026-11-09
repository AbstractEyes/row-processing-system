"""Builders shared by the tests of the repair step: a configuration with the step on, a fake writer that
writes a row again when it is asked to repair one, and reviewers that send chosen lines back."""

from __future__ import annotations

import json
from typing import Any

from rps.providers import FakeOutcome, FakeWriter
from rps.providers.base import PollStatus

from helpers import ROUTE_CAPS
from review_helpers import review_config

FIX_REASON = "a fix verdict: the row waits for the owner"
THREE = [
    {"id": "rev-a", "family": "alpha"},
    {"id": "rev-b", "family": "beta"},
    {"id": "rev-c", "family": "gamma"},
]
CAPS = {**ROUTE_CAPS, "repair": 1.0}


def repair_route(caps: dict[str, float] | None = None, **extra: Any) -> dict[str, Any]:
    """The one fake batch route of these tests, with a cap for the purpose `repair` unless `caps` says otherwise."""
    return {
        "provider": "fake",
        "model": "fake-model",
        "kind": "batch",
        "effort": "low",
        "budget_usd": 10.0,
        "purpose_caps": dict(CAPS if caps is None else caps),
        **extra,
    }


def repair_config(
    reviewers=THREE,
    *,
    enabled: bool = True,
    min_rows: int = 1,
    caps: dict[str, float] | None = None,
    writer_family: str = "writer-family",
    policy: dict[str, Any] | None = None,
    route: dict[str, Any] | None = None,
    routes: dict[str, dict[str, Any]] | None = None,
    **extra: Any,
):
    """A configuration with the repair step on, `fix_to_hand` on, and a route that has a cap for `repair`.

    `route` replaces the one route (`fake-batch`); `routes` replaces all of them.
    """
    return review_config(
        reviewers,
        writer_family=writer_family,
        policy={"fix_to_hand": True, **(policy or {})},
        routes=routes if routes is not None else {"fake-batch": route if route is not None else repair_route(caps)},
        repair={"enabled": enabled, "min_rows": min_rows},
        **extra,
    )


def is_repair_payload(payload) -> bool:
    return bool(payload.get("repair_of"))


def writes_and_rewrites(payload):
    """A writer that writes a thought about the spec, and a thought that says it was repaired when it is asked to
    write a row again (the call then carries `repair_of`)."""
    if is_repair_payload(payload):
        row = {"id": f"{payload['line']}-repaired", "thoughts": [f"repaired: a better thought about {payload['spec']}"]}
    else:
        row = {"id": f"{payload['line']}-{payload['attempt']}", "thoughts": [f"a thought about {payload['spec']}"]}
    return FakeOutcome(output=json.dumps(row))


def sends_back(*lines, repaired="keep"):
    """A review function: a row written for one of `lines` is sent back with a fix, any other row is kept, and a
    row that was written again (its thought starts with "repaired") gets the verdict `repaired`."""

    def verdict(reviewer, item):
        if item.row["thoughts"][0].startswith("repaired"):
            word = repaired
        else:
            word = "fix" if item.task["line_id"] in lines else "keep"
        if word == "keep":
            return {"verdict": "keep"}
        return {
            "verdict": word,
            "reasons": ["needs_work"],
            "notes": f"line {item.task['line_id']}: thought 1 states a pose that no record gives",
        }

    return verdict


def gated_writer(held: set[str]):
    """A fake batch writer class that keeps every batch holding a call for one of the spec lines in `held` in
    progress for as long as the line is in the set, so that a route can be kept busy while a pass looks at
    other rows. Take a line out of the set to let its batch finish."""

    class Gated(FakeWriter):
        def poll(self, handle):
            if any(call_id.split(":")[1] in held for call_id in handle.call_ids):
                return PollStatus("in_progress")
            return super().poll(handle)

    return Gated
