"""Anthropic writers: Message Batches (route kind "batch") and one call at a time (kind "api").

Claude routes take text only: a call that carries a picture is refused. The system prompt
(the writing rules) is sent as one block with a cache mark, the answer is constrained by
the row schema (`output_config.format`), and `temperature`, `top_p`, `top_k`, a prefill
and a forced `tool_choice` are never sent.

A batch is created with `messages.batches.create`; its id is returned in the handle and
the controller records it before anything waits. Results come back in any order and are
matched to calls by `custom_id`.

A batch cannot be searched for what it holds, so a crash in the instant between the
provider accepting a batch and the controller recording its id is recovered by size and
time: `recover` pages the provider's list of batches (newest first) back to the moment
the calls were prepared, less a margin for clock difference, and takes the newest batch
the controller has not recorded whose request counts add up to the size of the group. The
batch is adopted with `adopted` set in its handle. Matching by size and time can take a
stranger's batch of the same size, so when an adopted batch's results hold none of the
group's calls, those calls fail as a batch mismatch (retryable) and are sent again.

The key is read from ANTHROPIC_API_KEY and never logged. Route options: `cache_ttl`
("5m" or "1h", default "1h").
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Collection, Sequence

from ..config import ConfigError, RouteConfig
from ..failures import KIND_AUTH, KIND_BAD_REQUEST, KIND_CANCELED, KIND_EXPIRED, KIND_MISMATCH, KIND_PROVIDER
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
# Finding a batch again after a crash: how far before the calls were prepared a batch may
# have been created (the provider's clock against this one), how many batches are looked at
# in one page, and the most that are looked at in all.
RECOVER_SKEW = timedelta(minutes=2)
RECOVER_PAGE = 100
RECOVER_MAX_LISTED = 1000
REQUEST_COUNT_FIELDS = ("processing", "succeeded", "errored", "canceled", "expired")

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


def _moment(value: Any) -> datetime | None:
    """A timestamp from the SDK (a datetime) or from plain data (ISO text), in UTC; None when unreadable."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def batch_size(batch: Any) -> int:
    """How many requests a listed batch holds: its request counts added up."""
    counts = lookup(batch, "request_counts")
    return sum(count(lookup(counts, name)) for name in REQUEST_COUNT_FIELDS)


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
        if not found and handle.data.get("adopted"):
            # A batch taken up after a crash was matched by size and time alone, and this one
            # holds none of the calls: somebody else's batch. The calls are sent again.
            return [
                CallResult(
                    call_id=call_id,
                    output=None,
                    error="the adopted batch did not hold these calls",
                    retryable=True,
                    kind=KIND_MISMATCH,
                )
                for call_id in handle.call_ids
            ]
        return found

    def recover(
        self, requests: Sequence[Request], *, since: datetime | None = None, recorded: Collection[str] = ()
    ) -> Handle | None:
        """Find a batch created just before a crash, by its size and when it was created.

        Takes the newest batch the controller has not recorded whose request counts add up to
        the size of one of the groups these requests split into (the last group first, since
        the groups were sent in order). Without `since` only the newest page of batches is
        looked at. The caller asks again with what is left until nothing more is found.
        """
        if not requests:
            return None
        candidates = self._unrecorded_batches(since, recorded)
        for group in reversed(self.split(requests)):
            for batch in candidates:
                if batch_size(batch) != len(group):
                    continue
                batch_id = str(lookup(batch, "id"))
                return Handle(
                    handle_id=batch_id,
                    call_ids=tuple(request.call_id for request in group),
                    mode="batch",
                    data={"batch_id": batch_id, "adopted": True},
                )
        return None

    def _unrecorded_batches(self, since: datetime | None, recorded: Collection[str]) -> list[Any]:
        """Listed batches newer than `since` (less the clock margin) that nobody has recorded, newest first."""
        start = _moment(since)
        bound = start - RECOVER_SKEW if start is not None else None
        limit = RECOVER_MAX_LISTED if bound is not None else RECOVER_PAGE
        found: list[Any] = []
        listing = iter(self._client().messages.batches.list(limit=RECOVER_PAGE))
        for _ in range(limit):  # never ask for one more than the limit: the next page would be fetched for nothing
            batch = next(listing, None)
            if batch is None:
                break
            created = _moment(lookup(batch, "created_at"))
            if bound is not None and created is not None and created < bound:
                break  # the list is newest first, so everything after this is older still
            batch_id = lookup(batch, "id")
            if not batch_id or str(batch_id) in recorded or lookup(batch, "processing_status") == "canceling":
                continue
            found.append(batch)
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
