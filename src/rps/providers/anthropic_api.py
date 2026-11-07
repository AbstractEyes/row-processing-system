"""Anthropic writers: Message Batches (route kind "batch") and one call at a time (kind "api").

Claude routes take text only: a call that carries a picture is refused. The system prompt
(the writing rules) is sent as one block with a cache mark, the answer is constrained by
the row schema (`output_config.format`), and `temperature`, `top_p`, `top_k`, a prefill
and a forced `tool_choice` are never sent.

A batch is created with `messages.batches.create`; its id is returned in the handle and
the controller records it before anything waits. Results come back in any order and are
matched to calls by `custom_id`. A batch cannot be searched for what it holds, so a crash
in the instant between the provider accepting a batch and the controller recording its id
cannot be recovered from the provider's side (`recover` finds nothing); the budgets bound
what such a gap can cost.

The key is read from ANTHROPIC_API_KEY and never logged. Route options: `cache_ttl`
("5m" or "1h", default "1h").
"""

from __future__ import annotations

import threading
from typing import Any, Sequence

from ..config import ConfigError, RouteConfig
from ..failures import KIND_AUTH, KIND_BAD_REQUEST, KIND_CANCELED, KIND_EXPIRED, KIND_PROVIDER
from ..ids import canonical_json, custom_id
from ..ledger import Usage
from ..prompting import WriterRules
from ..store.remote import describe_error
from .base import Acceptance, CallResult, CallSpec, Handle, PollStatus, Request, RouteRejected, RouteUnavailable, Writer, split_requests
from .common import (
    STOP_COMPLETE,
    STOP_OTHER,
    STOP_REFUSED,
    STOP_TRUNCATED,
    api_key,
    call_text,
    check_options,
    classify_error_type,
    classify_exception,
    count,
    estimate_text_tokens,
    failure_from_exception,
    finished_call,
    http_status,
    key_acceptance,
    lookup,
    option,
    require_text_only,
)
from .pool import PoolWriter
from .schemas import ANTHROPIC, SchemaError, check_schema

API_KEY_ENV = "ANTHROPIC_API_KEY"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BATCH_MAX_REQUESTS = 100_000
BATCH_MAX_BYTES = 240 * 1024 * 1024  # the limit is 256 MB; stay clear of it
DEFAULT_SYNC_TIMEOUT = 600.0

_STOP_BY_REASON = {
    "end_turn": STOP_COMPLETE,
    "stop_sequence": STOP_COMPLETE,
    "max_tokens": STOP_TRUNCATED,
    "refusal": STOP_REFUSED,
}


# ---------------------------------------------------------------------------- parsing


def usage_of(usage: Any) -> Usage:
    """Anthropic's usage as disjoint billable buckets.

    `input_tokens` already leaves out the cache writes and reads; `output_tokens` includes the
    thinking tokens, which are split off so each bucket is counted once.
    """
    thinking = count(lookup(usage, "output_tokens_details", "thinking_tokens"))
    output = count(lookup(usage, "output_tokens"))
    return Usage(
        input_tokens=count(lookup(usage, "input_tokens")),
        cache_write_tokens=count(lookup(usage, "cache_creation_input_tokens")),
        cache_read_tokens=count(lookup(usage, "cache_read_input_tokens")),
        output_tokens=max(0, output - thinking),
        reasoning_tokens=min(thinking, output),
    )


def parse_message(call_id: str, message: Any, *, max_tokens: int | None = None, minutes: float | None = None) -> CallResult:
    """A finished Message as a call result."""
    usage = usage_of(lookup(message, "usage"))
    text = "".join(
        str(lookup(block, "text") or "") for block in (lookup(message, "content") or []) if lookup(block, "type") == "text"
    )
    reason = lookup(message, "stop_reason")
    stop = _STOP_BY_REASON.get(reason, STOP_OTHER)
    if stop == STOP_REFUSED:
        detail = text
    elif stop == STOP_OTHER:
        detail = str(reason) if reason is not None else "no stop reason"
    else:
        detail = ""
    return finished_call(call_id, text=text, usage=usage, stop=stop, detail=detail, max_tokens=max_tokens, minutes=minutes)


def parse_batch_item(item: Any, call_id: str) -> CallResult:
    """One line of a batch's results as a call result."""
    result = lookup(item, "result")
    kind = lookup(result, "type")
    if kind == "succeeded":
        return parse_message(call_id, lookup(result, "message"))
    if kind == "errored":
        error = lookup(result, "error", "error")
        error_type = lookup(error, "type")
        failure_kind, retryable = classify_error_type(error_type)
        message = str(lookup(error, "message") or "")[:200]
        return CallResult(
            call_id=call_id,
            output=None,
            error=f"{error_type}: {message}" if message else str(error_type),
            retryable=retryable,
            kind=failure_kind,
        )
    if kind == "canceled":
        return CallResult(call_id=call_id, output=None, error="the request was canceled before it ran", retryable=True, kind=KIND_CANCELED)
    if kind == "expired":
        return CallResult(
            call_id=call_id, output=None, error="the request expired before it ran (24 hours)", retryable=True, kind=KIND_EXPIRED
        )
    return CallResult(call_id=call_id, output=None, error=f"unknown batch result type {kind!r}", retryable=True, kind=KIND_PROVIDER)


# ---------------------------------------------------------------------------- shared by both writers


class _AnthropicCommon:
    """Request building, checks and the client, shared by the batch and the synchronous writer."""

    needs_rules = True
    route: RouteConfig
    route_name: str

    def _setup(self, client: Any | None) -> None:
        check_options(self.route, self.route_name, {"cache_ttl"})
        self._cache_ttl = option(self.route, self.route_name, "cache_ttl", "1h", choices=("5m", "1h"), kind=str)
        if self.route.effort is not None and self.route.effort not in EFFORTS:
            raise ConfigError(f"route {self.route_name!r}: effort must be one of {list(EFFORTS)}, got {self.route.effort!r}")
        self._client_obj = client
        self._client_lock = threading.Lock()

    def accepting(self) -> Acceptance:
        return key_acceptance(self.route, API_KEY_ENV)

    def check_rules(self, rules: WriterRules) -> None:
        try:
            check_schema(rules.schema, ANTHROPIC)
        except SchemaError as error:
            raise ConfigError(f"route {self.route_name!r}: {error}") from None

    def _client(self) -> Any:
        with self._client_lock:
            if self._client_obj is None:
                key = api_key(API_KEY_ENV)
                if key is None:
                    raise RouteUnavailable(f"{API_KEY_ENV} is not set")
                import anthropic  # imported here: the SDK is slow to load

                options: dict[str, Any] = {"api_key": key, "max_retries": self.route.max_retries}
                if self.route.timeout_seconds:
                    options["timeout"] = self.route.timeout_seconds
                self._client_obj = anthropic.Anthropic(**options)
            return self._client_obj

    def params_for(self, call: CallSpec) -> dict[str, Any]:
        """The `messages.create` arguments for one call; nothing the specification forbids is in them."""
        require_text_only(call, ANTHROPIC)
        if call.rules is None:
            raise ValueError("the call carries no writing rules")
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": dict(call.rules.schema)}}
        if call.effort:
            output_config["effort"] = call.effort
        return {
            "model": call.model,
            "max_tokens": call.max_output_tokens,
            "system": [
                {"type": "text", "text": call.rules.system, "cache_control": {"type": "ephemeral", "ttl": self._cache_ttl}}
            ],
            "messages": [{"role": "user", "content": [{"type": "text", "text": call_text(call)}]}],
            "output_config": output_config,
        }

    def prepare(self, calls: Sequence[CallSpec]) -> list[Request]:
        requests = []
        for call in calls:
            params = self.params_for(call)
            payload = {"custom_id": call.custom_id, "params": params}
            rules = call.rules
            assert rules is not None  # params_for raised otherwise
            requests.append(
                Request(
                    call_id=call.call_id,
                    custom_id=call.custom_id,
                    payload=payload,
                    est_input_tokens=estimate_text_tokens(rules.system, call_text(call), canonical_json(rules.schema)),
                    max_output_tokens=call.max_output_tokens,
                    attempt=call.attempt,
                    size_bytes=len(canonical_json(payload).encode("utf-8")) + 1,
                )
            )
        return requests


# ---------------------------------------------------------------------------- batch


class AnthropicBatchWriter(_AnthropicCommon, Writer):
    """Message Batches. See the module description."""

    def __init__(self, route_name: str, route: RouteConfig, *, client: Any | None = None) -> None:
        Writer.__init__(self, route_name, route)
        self._setup(client)

    def split(self, requests: Sequence[Request]) -> list[list[Request]]:
        return split_requests(requests, min(self.route.max_batch_calls, BATCH_MAX_REQUESTS), BATCH_MAX_BYTES)

    def submit(self, requests: Sequence[Request]) -> Handle:
        try:
            batch = self._client().messages.batches.create(requests=[request.payload for request in requests])
        except RouteUnavailable:
            raise
        except Exception as error:
            kind, _retryable = classify_exception(error)
            if kind in (KIND_AUTH, KIND_BAD_REQUEST):
                raise RouteRejected(kind, describe_error(error)) from None
            raise
        batch_id = str(lookup(batch, "id"))
        return Handle(handle_id=batch_id, call_ids=tuple(r.call_id for r in requests), mode="batch", data={"batch_id": batch_id})

    def poll(self, handle: Handle) -> PollStatus:
        try:
            batch = self._client().messages.batches.retrieve(handle.data["batch_id"])
        except Exception as error:
            if http_status(error) == 404:
                return PollStatus("failed", "the provider no longer knows this batch")
            raise
        return PollStatus("ended" if lookup(batch, "processing_status") == "ended" else "in_progress")

    def results(self, handle: Handle) -> list[CallResult]:
        call_of = {custom_id(call_id): call_id for call_id in handle.call_ids}
        found: list[CallResult] = []
        for item in self._client().messages.batches.results(handle.data["batch_id"]):
            call_id = call_of.get(str(lookup(item, "custom_id")))
            if call_id is not None:
                found.append(parse_batch_item(item, call_id))
        return found


# ---------------------------------------------------------------------------- one call at a time


class AnthropicSyncWriter(_AnthropicCommon, PoolWriter):
    """One `messages.create` call per request, from a pool of workers (route kind "api")."""

    def __init__(self, route_name: str, route: RouteConfig, *, client: Any | None = None) -> None:
        PoolWriter.__init__(self, route_name, route)
        self._setup(client)

    def send(self, request: Request) -> CallResult:
        params = dict(request.payload["params"])
        # An explicit timeout also keeps the SDK from refusing a large token limit on a plain call.
        timeout = float(self.route.timeout_seconds or DEFAULT_SYNC_TIMEOUT)
        try:
            message = self._client().messages.create(timeout=timeout, **params)
        except RouteUnavailable:
            raise
        except Exception as error:
            return failure_from_exception(request.call_id, error)
        return parse_message(request.call_id, message, max_tokens=request.max_output_tokens)
