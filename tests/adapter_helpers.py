"""Builders shared by the adapter tests: writing rules, calls, routes and recording clients."""

from __future__ import annotations

from typing import Any

from rps import ids
from rps.config import RouteConfig
from rps.prompting import WriterRules, make_rules
from rps.providers import CallSpec

SYSTEM = 'You write dataset rows. Answer with one json object, for example {"skip": false, "rows": []}.'
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["skip", "reason", "rows"],
    "properties": {
        "skip": {"type": "boolean"},
        "reason": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id"],
                "properties": {"id": {"type": "string"}},
            },
        },
    },
}
TASK = ids.task_id("photos", "1", {"001": "a", "002": "b", "003": "c"})
ANSWER = '{"skip": false, "reason": "", "rows": [{"id": "r1"}]}'


def rules(**overrides: Any) -> WriterRules:
    fields: dict[str, Any] = {"system": SYSTEM, "schema": SCHEMA, "version": "rules-v1"}
    fields.update(overrides)
    return make_rules(fields.pop("system"), fields.pop("schema"), fields.pop("version"), **fields)


def route(provider: str, model: str, kind: str = "batch", **overrides: Any) -> RouteConfig:
    fields: dict[str, Any] = {"provider": provider, "model": model, "kind": kind, "budget_usd": 5.0, "effort": "medium"}
    fields.update(overrides)
    return RouteConfig(**fields)


def call(line: str = "001", attempt: int = 1, *, provider: str, model: str, kind: str = "batch", **overrides: Any) -> CallSpec:
    fields: dict[str, Any] = dict(
        call_id=ids.call_id(TASK, line, attempt),
        task_id=TASK,
        line=line,
        attempt=attempt,
        route="r1",
        provider=provider,
        model=model,
        kind=kind,
        effort="medium",
        max_output_tokens=2000,
        spec=f"spec for {line}",
        pool_version="1",
        purpose="test",
        task_text=f"Write the row for {line}.",
        rules=rules(),
    )
    fields.update(overrides)
    return CallSpec(**fields)


class ApiError(Exception):
    """A stand-in for an SDK status error: it carries the HTTP status the way the SDKs' errors do."""

    def __init__(self, status_code: int, message: str = "", code: str | None = None) -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code
        self.code = code


class Recorder:
    """Records calls to a fake client method and returns what it is given (or raises it)."""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, **kwargs})
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return self.result(*args, **kwargs)
        return self.result


class Namespace:
    """A fake client section: attributes are set by the test."""

    def __init__(self, **members: Any) -> None:
        self.__dict__.update(members)
