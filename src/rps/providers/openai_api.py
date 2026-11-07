"""OpenAI writers: the Batch API on /v1/responses (route kind "batch") and one call at a time (kind "api").

Each request is a Responses call: the writing rules as `instructions`, one user message with
the task text and, where the routing table allows, a picture, a strict JSON schema as the
answer format, `store: false`, and the rules version as `prompt_cache_key`. Pictures go in as
data URLs; the `detail` route option ("low", "high", "original" or "auto", default "auto")
sets how closely the model looks.

A batch is a JSONL file of requests uploaded with `purpose="batch"` (and an expiry, as a
backstop), then `batches.create`. The batch id and the input file id are returned in the
handle and recorded by the controller before anything waits. The batch carries a digest of
its calls in its metadata, so a batch created just before a crash can be found again.
Results arrive in an output file and an error file, in any order; they are matched to calls by
`custom_id`. Once the controller has recorded the results, `release` deletes the three files
(the input holds the pictures).

The key is read from OPENAI_API_KEY and never logged. Route options: `detail`, and
`file_expiry_days` (the upload's expiry, default 7).
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Sequence

from ..config import ConfigError, RouteConfig
from ..failures import KIND_AUTH, KIND_BAD_REQUEST, KIND_BLOCKED, KIND_CANCELED, KIND_EXPIRED, KIND_PROVIDER, KIND_UNEXPECTED
from ..ids import canonical_json, custom_id
from ..ledger import Usage
from ..prompting import WriterRules
from ..store.remote import describe_error
from .base import Acceptance, CallResult, CallSpec, Handle, PollStatus, Request, RouteRejected, RouteUnavailable, Writer, split_requests
from .common import (
    STOP_BLOCKED,
    STOP_COMPLETE,
    STOP_OTHER,
    STOP_REFUSED,
    STOP_TRUNCATED,
    api_key,
    call_text,
    check_options,
    classify_exception,
    count,
    data_url,
    estimate_text_tokens,
    failure_from_exception,
    finished_call,
    http_status,
    key_acceptance,
    lookup,
    option,
    picture_for,
)
from .pool import PoolWriter
from .schemas import OPENAI, SchemaError, check_schema

API_KEY_ENV = "OPENAI_API_KEY"
DETAILS = ("low", "high", "original", "auto")
BATCH_MAX_REQUESTS = 50_000
BATCH_MAX_BYTES = 190 * 1024 * 1024  # the limit is 200 MB
# A generous count of tokens for one picture, for the cost estimate (the estimate is an upper bound).
IMAGE_TOKENS = {"low": 400, "auto": 2500, "high": 2500, "original": 6000}
MIN_FILE_EXPIRY_SECONDS = 3600
MAX_FILE_EXPIRY_SECONDS = 30 * 24 * 3600


# ---------------------------------------------------------------------------- parsing


def usage_of(usage: Any) -> Usage:
    """OpenAI's usage as disjoint billable buckets.

    `input_tokens` includes the cached and the cache-write tokens and `output_tokens` includes
    the reasoning tokens; each is split off so a token is counted once.
    """
    total_in = count(lookup(usage, "input_tokens"))
    cached = count(lookup(usage, "input_tokens_details", "cached_tokens"))
    written = count(lookup(usage, "input_tokens_details", "cache_write_tokens"))
    total_out = count(lookup(usage, "output_tokens"))
    reasoning = min(count(lookup(usage, "output_tokens_details", "reasoning_tokens")), total_out)
    return Usage(
        input_tokens=max(0, total_in - cached - written),
        cache_write_tokens=written,
        cache_read_tokens=cached,
        output_tokens=total_out - reasoning,
        reasoning_tokens=reasoning,
    )


def parse_response(call_id: str, body: Any, *, max_tokens: int | None = None, minutes: float | None = None) -> CallResult:
    """A Responses result (an SDK object or the plain dict of a batch file) as a call result."""
    usage = usage_of(lookup(body, "usage"))
    texts: list[str] = []
    refusal: str | None = None
    for item in lookup(body, "output") or []:
        if lookup(item, "type") != "message":
            continue
        for part in lookup(item, "content") or []:
            kind = lookup(part, "type")
            if kind == "output_text":
                texts.append(str(lookup(part, "text") or ""))
            elif kind == "refusal" and refusal is None:
                refusal = str(lookup(part, "refusal") or "")
    common: dict[str, Any] = {"usage": usage, "max_tokens": max_tokens, "minutes": minutes}
    if refusal is not None:
        return finished_call(call_id, text=None, stop=STOP_REFUSED, detail=refusal, **common)
    status = lookup(body, "status")
    if status == "completed":
        return finished_call(call_id, text="".join(texts), stop=STOP_COMPLETE, **common)
    if status == "incomplete":
        reason = lookup(body, "incomplete_details", "reason")
        if reason == "max_output_tokens":
            return finished_call(call_id, text=None, stop=STOP_TRUNCATED, **common)
        if reason == "content_filter":
            return finished_call(call_id, text=None, stop=STOP_BLOCKED, detail="content filter", **common)
        return finished_call(call_id, text=None, stop=STOP_OTHER, detail=f"incomplete: {reason}", **common)
    if status == "failed":
        code = str(lookup(body, "error", "code") or "")
        message = str(lookup(body, "error", "message") or "")[:200]
        if code == "invalid_prompt":
            return CallResult(call_id=call_id, output=None, usage=usage, error=f"{code}: {message}", retryable=False, kind=KIND_BLOCKED, minutes=minutes)
        return CallResult(call_id=call_id, output=None, usage=usage, error=f"{code or 'failed'}: {message}", retryable=True, kind=KIND_PROVIDER, minutes=minutes)
    return finished_call(call_id, text=None, stop=STOP_OTHER, detail=f"status {status}", **common)


def _http_failure(call_id: str, status_code: int | None, body: Any) -> CallResult:
    """A batch line whose HTTP status was not 200."""
    error = lookup(body, "error")
    code = str(lookup(error, "code") or lookup(error, "type") or "")
    message = str(lookup(error, "message") or "")[:200]
    text = f"HTTP {status_code}: {code} {message}".strip()
    if status_code in (401, 403) or code == "insufficient_quota":
        return CallResult(call_id=call_id, output=None, error=text, retryable=False, kind=KIND_AUTH)
    if status_code in (400, 404, 413, 422):
        return CallResult(call_id=call_id, output=None, error=text, retryable=False, kind=KIND_BAD_REQUEST)
    return CallResult(call_id=call_id, output=None, error=text, retryable=True, kind=KIND_PROVIDER)


def parse_batch_line(line: Any, call_id: str) -> CallResult:
    """One line of a batch's output or error file as a call result."""
    response = lookup(line, "response")
    if response is None:
        error = lookup(line, "error")
        code = str(lookup(error, "code") or "")
        message = str(lookup(error, "message") or "")[:200]
        text = f"{code}: {message}".strip(": ") or "the request has no response"
        if code == "batch_expired":
            return CallResult(call_id=call_id, output=None, error=text, retryable=True, kind=KIND_EXPIRED)
        if code == "batch_cancelled":
            return CallResult(call_id=call_id, output=None, error=text, retryable=True, kind=KIND_CANCELED)
        return CallResult(call_id=call_id, output=None, error=text, retryable=True, kind=KIND_UNEXPECTED)
    status_code = lookup(response, "status_code")
    body = lookup(response, "body")
    if status_code == 200:
        return parse_response(call_id, body)
    return _http_failure(call_id, status_code if isinstance(status_code, int) else None, body)


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    """The objects in a JSONL file's text (blank and unreadable lines are skipped)."""
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            lines.append(item)
    return lines


def jsonl_text(content: Any) -> str:
    """The text of a downloaded file (the SDK's binary response, or plain text or bytes)."""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode("utf-8")
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return content.read().decode("utf-8")


def digest_of(custom_ids: Sequence[str]) -> str:
    """A short digest of a group of custom ids, kept in the batch's metadata so the batch can be found again."""
    return hashlib.sha256("\n".join(sorted(custom_ids)).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------- shared by both writers


class _OpenAICommon:
    """Request building, checks and the client, shared by the batch and the synchronous writer."""

    needs_rules = True
    route: RouteConfig
    route_name: str

    def _setup(self, client: Any | None) -> None:
        check_options(self.route, self.route_name, {"detail", "file_expiry_days"})
        self._detail = option(self.route, self.route_name, "detail", "auto", choices=DETAILS, kind=str)
        days = option(self.route, self.route_name, "file_expiry_days", 7, kind=(int, float))
        self._file_expiry = int(min(MAX_FILE_EXPIRY_SECONDS, max(MIN_FILE_EXPIRY_SECONDS, days * 24 * 3600)))
        self._client_obj = client
        self._client_lock = threading.Lock()

    def accepting(self) -> Acceptance:
        return key_acceptance(self.route, API_KEY_ENV)

    def check_rules(self, rules: WriterRules) -> None:
        try:
            check_schema(rules.schema, OPENAI)
        except SchemaError as error:
            raise ConfigError(f"route {self.route_name!r}: {error}") from None

    def _client(self) -> Any:
        with self._client_lock:
            if self._client_obj is None:
                key = api_key(API_KEY_ENV)
                if key is None:
                    raise RouteUnavailable(f"{API_KEY_ENV} is not set")
                import openai  # imported here: the SDK is slow to load

                options: dict[str, Any] = {"api_key": key, "max_retries": self.route.max_retries}
                if self.route.timeout_seconds:
                    options["timeout"] = self.route.timeout_seconds
                self._client_obj = openai.OpenAI(**options)
            return self._client_obj

    def body_for(self, call: CallSpec) -> dict[str, Any]:
        """The /v1/responses body for one call."""
        if call.rules is None:
            raise ValueError("the call carries no writing rules")
        content: list[dict[str, Any]] = [{"type": "input_text", "text": call_text(call)}]
        picture = picture_for(call, OPENAI)
        if picture is not None:
            content.append({"type": "input_image", "image_url": data_url(*picture), "detail": self._detail})
        body: dict[str, Any] = {
            "model": call.model,
            "instructions": call.rules.system,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {"type": "json_schema", "name": call.rules.schema_name, "schema": dict(call.rules.schema), "strict": True}
            },
            "max_output_tokens": call.max_output_tokens,
            "store": False,
            "prompt_cache_key": call.rules.version,
        }
        if call.effort:
            body["reasoning"] = {"effort": call.effort}
        return body

    def prepare(self, calls: Sequence[CallSpec]) -> list[Request]:
        requests = []
        for call in calls:
            body = self.body_for(call)
            rules = call.rules
            assert rules is not None  # body_for raised otherwise
            line = {"custom_id": call.custom_id, "method": "POST", "url": "/v1/responses", "body": body}
            picture_tokens = IMAGE_TOKENS[self._detail] if call.image is not None else 0
            requests.append(
                Request(
                    call_id=call.call_id,
                    custom_id=call.custom_id,
                    payload=line,
                    est_input_tokens=estimate_text_tokens(rules.system, call_text(call), canonical_json(rules.schema)) + picture_tokens,
                    max_output_tokens=call.max_output_tokens,
                    attempt=call.attempt,
                    size_bytes=len(json.dumps(line, ensure_ascii=False).encode("utf-8")) + 1,
                )
            )
        return requests


# ---------------------------------------------------------------------------- batch


class OpenAIBatchWriter(_OpenAICommon, Writer):
    """The Batch API. See the module description."""

    def __init__(self, route_name: str, route: RouteConfig, *, client: Any | None = None) -> None:
        Writer.__init__(self, route_name, route)
        self._setup(client)

    def split(self, requests: Sequence[Request]) -> list[list[Request]]:
        return split_requests(requests, min(self.route.max_batch_calls, BATCH_MAX_REQUESTS), BATCH_MAX_BYTES)

    def submit(self, requests: Sequence[Request]) -> Handle:
        client = self._client()
        # The key order of each body is kept as built: the schema's property order shapes the answer.
        text = "".join(json.dumps(request.payload, ensure_ascii=False, separators=(",", ":")) + "\n" for request in requests)
        metadata = {
            "rps_digest": digest_of([request.custom_id for request in requests]),
            "rps_count": str(len(requests)),
            "rps_route": self.route_name[:64],
        }
        try:
            uploaded = client.files.create(
                file=("rps-batch.jsonl", text.encode("utf-8")),
                purpose="batch",
                expires_after={"anchor": "created_at", "seconds": self._file_expiry},
            )
            file_id = str(lookup(uploaded, "id"))
            try:
                batch = client.batches.create(
                    input_file_id=file_id, endpoint="/v1/responses", completion_window="24h", metadata=metadata
                )
            except Exception:
                self._delete_quietly(file_id)
                raise
        except RouteUnavailable:
            raise
        except Exception as error:
            kind, _retryable = classify_exception(error)
            if kind in (KIND_AUTH, KIND_BAD_REQUEST):
                raise RouteRejected(kind, describe_error(error)) from None
            raise
        batch_id = str(lookup(batch, "id"))
        return Handle(
            handle_id=batch_id,
            call_ids=tuple(request.call_id for request in requests),
            mode="batch",
            data={"batch_id": batch_id, "input_file_id": file_id},
        )

    def poll(self, handle: Handle) -> PollStatus:
        try:
            batch = self._client().batches.retrieve(handle.data["batch_id"])
        except Exception as error:
            if http_status(error) == 404:
                return PollStatus("failed", "the provider no longer knows this batch")
            raise
        status = lookup(batch, "status")
        if status == "completed":
            return PollStatus("ended")
        if status == "expired":
            has_files = lookup(batch, "output_file_id") or lookup(batch, "error_file_id")
            return PollStatus("ended") if has_files else PollStatus("expired", "no request finished within 24 hours")
        if status == "failed":
            errors = lookup(batch, "errors", "data") or []
            note = str(lookup(errors[0], "message") or "") if errors else ""
            return PollStatus("failed", note[:200] or None)
        if status == "cancelled":
            return PollStatus("canceled")
        return PollStatus("in_progress")

    def results(self, handle: Handle) -> list[CallResult]:
        client = self._client()
        batch = client.batches.retrieve(handle.data["batch_id"])
        call_of = {custom_id(call_id): call_id for call_id in handle.call_ids}
        found: dict[str, CallResult] = {}
        for field in ("output_file_id", "error_file_id"):
            file_id = lookup(batch, field)
            if not file_id:
                continue
            for line in parse_jsonl(jsonl_text(client.files.content(str(file_id)))):
                call_id = call_of.get(str(line.get("custom_id")))
                if call_id is None:
                    continue
                result = parse_batch_line(line, call_id)
                # A request can appear in both files only if the provider retried it; the output file wins.
                if call_id not in found or field == "output_file_id":
                    found[call_id] = result
        return list(found.values())

    def recover(self, requests: Sequence[Request]) -> Handle | None:
        """Find a batch created just before a crash, by the digest of its custom ids in its metadata."""
        if not requests:
            return None
        groups = {digest_of([r.custom_id for r in group]): group for group in self.split(requests)}
        for batch in self._client().batches.list(limit=100):
            metadata = lookup(batch, "metadata") or {}
            group = groups.get(metadata.get("rps_digest")) if isinstance(metadata, dict) else None
            if group is None or lookup(batch, "status") in ("failed", "cancelled", "cancelling"):
                continue
            batch_id = str(lookup(batch, "id"))
            return Handle(
                handle_id=batch_id,
                call_ids=tuple(r.call_id for r in group),
                mode="batch",
                data={"batch_id": batch_id, "input_file_id": str(lookup(batch, "input_file_id"))},
            )
        return None

    def release(self, handle: Handle) -> None:
        """Delete the input, output and error files once their contents are recorded."""
        file_ids = [handle.data.get("input_file_id")]
        try:
            batch = self._client().batches.retrieve(handle.data["batch_id"])
            file_ids += [lookup(batch, "output_file_id"), lookup(batch, "error_file_id")]
        except Exception:
            pass  # the files still expire on their own
        for file_id in file_ids:
            if file_id:
                self._delete_quietly(str(file_id))

    def _delete_quietly(self, file_id: str) -> None:
        try:
            self._client().files.delete(file_id)
        except Exception:
            pass  # already gone, or it expires on its own


# ---------------------------------------------------------------------------- one call at a time


class OpenAISyncWriter(_OpenAICommon, PoolWriter):
    """One `responses.create` call per request, from a pool of workers (route kind "api")."""

    def __init__(self, route_name: str, route: RouteConfig, *, client: Any | None = None) -> None:
        PoolWriter.__init__(self, route_name, route)
        self._setup(client)

    def send(self, request: Request) -> CallResult:
        body = dict(request.payload["body"])
        try:
            response = self._client().responses.create(**body)
        except RouteUnavailable:
            raise
        except Exception as error:
            return failure_from_exception(request.call_id, error)
        return parse_response(request.call_id, response, max_tokens=request.max_output_tokens)
