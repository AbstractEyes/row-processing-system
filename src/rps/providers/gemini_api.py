"""Gemini writers: batch mode with inline requests (route kind "batch") and one call at a time (kind "api").

Each request is a `GenerateContent` call: the task text and, where the routing table allows, a
picture as the user turn; the writing rules as the system instruction; JSON output constrained by
the row schema (`response_json_schema`, in the form the `schema` option names); a thinking level
from the route's effort; and, for a picture, a media resolution. Inline batches stay under the
provider's size limit by splitting.

Route options: `schema` ("plain", "exact", "none" or "openapi", default "plain": the form in which the row schema
is sent, see `rps.providers.schemas.gemini_schema`), `schema_field` ("response_json_schema", the default, or
"response_schema": the request field that carries it), `batch_form` (see below), `media_resolution` ("low",
"medium" or "high", default "medium"), `cache` (true to keep the writing rules in an explicit cache for batches; default
false) and `cache_ttl_seconds` (default 21600). With the cache on, a batch refers to the cache by name instead of repeating the
rules in every request; the cache is created once per model and rules version, its name is
recorded (a `cache_created` event) so a restart reuses it, and its lifetime is extended while a
batch that refers to it is still running. The provider bills a cache for the hours it is kept,
by the size of the cache: each note carries the size in tokens and the stretch of the cache's life
it covers, and the controller charges that stretch to the ledger (a `cache_storage` event). The
one-time cost of making the cache is not stated on the provider's page and is not counted.

A batch carries a digest of its calls in its display name, so a batch created just before a crash
can be found again. Results come back in the order of the requests, each with the metadata of its
request; they are matched to calls by the `custom_id` (or `key`) in it.

The form of a batch request. The batch service validates a request apart from the one-call endpoint, and
a live run had every request of a batch refused ("3: Request contains an invalid argument.") by the batch
service although the one-call endpoint took the same request. The batch pages show an inline request with
the schema in `response_schema`, the request's own metadata under the name `key`, and a cache only by name;
they say nothing of `response_json_schema`, `thinking_config` or a `custom_id`. So a batch route has a
second form, the documented one: the schema in `response_schema`, the metadata key `key`, no explicit
cache. The `response_schema` field takes an OpenAPI Schema object, which has no `additionalProperties` (a live
batch refused it by name: "Unknown name "additional_properties""), so in this form the row schema is sent in
the "openapi" form whatever the `schema` option says (unless that is "none"). The route option `batch_form` is "auto" (the default: the first form, and when every request of a
batch comes back refused as an invalid argument, the same calls again in the documented form, once for the
run), "documented" (always the documented form) or "full" (always the first form, never a second).
`python -m rps.geminiprobe --batch` sends the variants and says which part the service refuses.

The key is read from GEMINI_API_KEY and never logged.
"""

from __future__ import annotations

import dataclasses
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Collection, Sequence

from ..config import ConfigError, RouteConfig
from ..failures import KIND_AUTH, KIND_BAD_REQUEST, KIND_CANCELED, KIND_PROVIDER, PROVIDER_ERROR_CHARS
from ..ids import canonical_json, custom_id
from ..keys import redact
from ..ledger import Usage
from ..prompting import WriterRules
from ..store.remote import describe_error
from .base import Acceptance, CallResult, CallSpec, Handle, PollStatus, Request, RouteRejected, RouteUnavailable, Writer, split_requests
from .common import (
    STOP_BLOCKED,
    STOP_COMPLETE,
    STOP_OTHER,
    STOP_TRUNCATED,
    api_key,
    call_text,
    check_options,
    classify_exception,
    count,
    estimate_text_tokens,
    failure_from_exception,
    finished_call,
    http_status,
    key_acceptance,
    lookup,
    mime_type,
    option,
    picture_for,
)
from .pool import PoolWriter
from .schemas import GEMINI, GEMINI_SCHEMA_MODES, SchemaError, check_schema, gemini_schema

API_KEY_ENV = "GEMINI_API_KEY"
THINKING_LEVELS = ("minimal", "low", "medium", "high")
RESOLUTIONS = ("low", "medium", "high")
DEFAULT_SCHEMA_MODE = "plain"
SCHEMA_FIELDS = ("response_json_schema", "response_schema")  # the request field that carries the schema
DEFAULT_SCHEMA_FIELD = "response_json_schema"
BATCH_FORMS = ("auto", "documented", "full")
DEFAULT_BATCH_FORM = "auto"
FORM_FULL = 0  # the request the writer is built to send
FORM_DOCUMENTED = 1  # the request the batch pages show (see the module description)
BATCH_MAX_BYTES = 19 * 1024 * 1024  # inline jobs must stay under 20 MB in total
# A generous count of tokens for one picture, for the cost estimate (the estimate is an upper bound).
IMAGE_TOKENS = {"low": 300, "medium": 600, "high": 1200}
CACHE_REUSE_MARGIN = timedelta(hours=2)

_BLOCKED_FINISHES = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "BLOCKLIST", "RECITATION", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"}
)


def _name(value: Any) -> str:
    """The name of an enum member, however the SDK or a recorded fixture spells it."""
    raw = getattr(value, "name", None) or getattr(value, "value", None) or value
    return str(raw).split(".")[-1] if raw is not None else ""


# ---------------------------------------------------------------------------- parsing


def usage_of(usage: Any) -> Usage:
    """Gemini's usage as disjoint billable buckets.

    The prompt count includes the cached tokens; thinking tokens are counted apart from the
    candidates' tokens and are billed as output.
    """
    prompt = count(lookup(usage, "prompt_token_count"))
    cached = min(count(lookup(usage, "cached_content_token_count")), prompt)
    return Usage(
        input_tokens=prompt - cached,
        cache_read_tokens=cached,
        output_tokens=count(lookup(usage, "candidates_token_count")),
        reasoning_tokens=count(lookup(usage, "thoughts_token_count")),
    )


def parse_response(call_id: str, response: Any, *, max_tokens: int | None = None, minutes: float | None = None) -> CallResult:
    """A GenerateContentResponse as a call result."""
    usage = usage_of(lookup(response, "usage_metadata"))
    common: dict[str, Any] = {"usage": usage, "max_tokens": max_tokens, "minutes": minutes}
    candidates = lookup(response, "candidates") or []
    if not candidates:
        reason = lookup(response, "prompt_feedback", "block_reason")
        if reason:
            return finished_call(call_id, text=None, stop=STOP_BLOCKED, detail=_name(reason), **common)
        return finished_call(call_id, text=None, stop=STOP_OTHER, detail="the response has no candidates", **common)
    candidate = candidates[0]
    parts = lookup(candidate, "content", "parts") or []
    text = "".join(str(lookup(part, "text") or "") for part in parts if lookup(part, "text") and not lookup(part, "thought"))
    reason = _name(lookup(candidate, "finish_reason"))
    if reason == "STOP":
        return finished_call(call_id, text=text, stop=STOP_COMPLETE, **common)
    if reason == "MAX_TOKENS":
        return finished_call(call_id, text=None, stop=STOP_TRUNCATED, **common)
    if reason in _BLOCKED_FINISHES:
        return finished_call(call_id, text=None, stop=STOP_BLOCKED, detail=reason, **common)
    return finished_call(call_id, text=None, stop=STOP_OTHER, detail=reason or "no finish reason", **common)


# The status code of a failed batch item. The batch service reports gRPC codes (3 is INVALID_ARGUMENT:
# a live run got "3: Request contains an invalid argument." for every call, and took it for a passing
# trouble of the provider's); a code in the HTTP range is read as the HTTP status. Codes not listed are
# the provider's own trouble and are retried, and so is any failure that names the explicit cache, which
# is made again when it has lapsed.
_ITEM_BAD_REQUEST = frozenset({3, 5, 9, 11, 400, 404, 413, 422})  # invalid argument, not found, failed precondition, out of range
_ITEM_AUTH = frozenset({7, 16, 401, 403})  # permission denied, unauthenticated
_ITEM_CANCELED = frozenset({1, 499})
# A request the batch service refuses as a whole is reported as an invalid argument: gRPC code 3, or HTTP 400.
_INVALID_ARGUMENT = frozenset({3, 400})


def inlined_error_text(error: Any) -> str:
    """The text of a failed batch item: the code, the provider's message, and its details when it gives any."""
    code = lookup(error, "code")
    message = str(lookup(error, "message") or "").strip()
    text = f"{code}: {message}" if code else (message or "the request failed")
    details = [str(item) for item in (lookup(error, "details") or []) if item]
    if details:
        text += " | details: " + "; ".join(details)
    return redact(text)[:PROVIDER_ERROR_CHARS]


def parse_inlined(item: Any, call_id: str) -> CallResult:
    """One inlined response of a finished batch as a call result."""
    error = lookup(item, "error")
    if error is not None and lookup(item, "response") is None:
        code = lookup(error, "code")
        text = inlined_error_text(error)
        if "cache" in text.lower():
            return CallResult(call_id=call_id, output=None, error=text, retryable=True, kind=KIND_PROVIDER)
        if code in _ITEM_BAD_REQUEST:
            return CallResult(call_id=call_id, output=None, error=text, retryable=False, kind=KIND_BAD_REQUEST)
        if code in _ITEM_AUTH:
            return CallResult(call_id=call_id, output=None, error=text, retryable=False, kind=KIND_AUTH)
        if code in _ITEM_CANCELED:
            return CallResult(call_id=call_id, output=None, error=text, retryable=True, kind=KIND_CANCELED)
        return CallResult(call_id=call_id, output=None, error=text, retryable=True, kind=KIND_PROVIDER)
    return parse_response(call_id, lookup(item, "response"))


def _refused_as_invalid(item: Any) -> bool:
    """True for a batch item that came back with no answer and an invalid-argument error."""
    error = lookup(item, "error")
    return error is not None and lookup(item, "response") is None and lookup(error, "code") in _INVALID_ARGUMENT


def _with_note(result: CallResult, note: str, *, retry: bool = False) -> CallResult:
    """A refused item's result with a note after the words of the provider (within the length an error keeps).

    With `retry` the refusal is read as a passing trouble, so the controller sends the call again.
    """
    suffix = f" | {note}"
    base = (result.error or "")[: max(0, PROVIDER_ERROR_CHARS - len(suffix))]
    changes: dict[str, Any] = {"error": base + suffix}
    if retry:
        changes.update(retryable=True, kind=KIND_PROVIDER)
    return dataclasses.replace(result, **changes)


def digest_of(custom_ids: Sequence[str]) -> str:
    """A short digest of a group of custom ids, kept in the batch's display name so the batch can be found again."""
    return hashlib.sha256("\n".join(sorted(custom_ids)).encode("utf-8")).hexdigest()[:24]


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------- shared by both writers


class _GeminiCommon:
    """Request building, checks and the client, shared by the batch and the synchronous writer."""

    needs_rules = True
    route: RouteConfig
    route_name: str

    def _setup(self, client: Any | None, *, batch: bool = False) -> None:
        allowed = {"schema", "schema_field", "media_resolution", "cache", "cache_ttl_seconds"}
        check_options(self.route, self.route_name, allowed | ({"batch_form"} if batch else set()))
        self._schema_mode = option(self.route, self.route_name, "schema", DEFAULT_SCHEMA_MODE, choices=GEMINI_SCHEMA_MODES, kind=str)
        self._schema_field = option(self.route, self.route_name, "schema_field", DEFAULT_SCHEMA_FIELD, choices=SCHEMA_FIELDS, kind=str)
        self._batch_form = (
            option(self.route, self.route_name, "batch_form", DEFAULT_BATCH_FORM, choices=BATCH_FORMS, kind=str)
            if batch
            else DEFAULT_BATCH_FORM
        )
        self._resolution = option(self.route, self.route_name, "media_resolution", "medium", choices=RESOLUTIONS, kind=str)
        self._use_cache = bool(option(self.route, self.route_name, "cache", False, kind=bool))
        self._cache_ttl = int(option(self.route, self.route_name, "cache_ttl_seconds", 21600, kind=int))
        if self._cache_ttl < 60:
            raise ConfigError(f"route {self.route_name!r}: option 'cache_ttl_seconds' must be at least 60")
        if self.route.effort is not None and self.route.effort not in THINKING_LEVELS:
            raise ConfigError(f"route {self.route_name!r}: effort must be one of {list(THINKING_LEVELS)}, got {self.route.effort!r}")
        self._client_obj = client
        self._client_lock = threading.Lock()

    def accepting(self) -> Acceptance:
        return key_acceptance(self.route, API_KEY_ENV)

    def check_rules(self, rules: WriterRules) -> None:
        if self._schema_mode == "none":
            return  # no schema is sent
        try:
            check_schema(rules.schema, GEMINI)
            # A batch route may step down to the documented form, which sends the OpenAPI form of the
            # schema: a schema that cannot be written so is found here, not in the middle of a run.
            gemini_schema(rules.schema, "openapi")
        except SchemaError as error:
            raise ConfigError(f"route {self.route_name!r}: {error}") from None

    def _schema_mode_now(self) -> str:
        """The form of the row schema that requests are built with now."""
        return self._schema_mode

    def answer_schema(self, rules: WriterRules) -> dict[str, Any] | None:
        """The row schema in the form this route sends (None when the route sends none)."""
        return gemini_schema(rules.schema, self._schema_mode_now())

    def _client(self) -> Any:
        with self._client_lock:
            if self._client_obj is None:
                key = api_key(API_KEY_ENV)
                if key is None:
                    raise RouteUnavailable(f"{API_KEY_ENV} is not set")
                from google import genai  # imported here: the SDK is slow to load
                from google.genai import types

                http: dict[str, Any] = {"retry_options": types.HttpRetryOptions(attempts=self.route.max_retries + 1)}
                if self.route.timeout_seconds:
                    http["timeout"] = int(self.route.timeout_seconds * 1000)
                self._client_obj = genai.Client(api_key=key, http_options=types.HttpOptions(**http))
            return self._client_obj

    def _form(self) -> int:
        """The form requests are built in now (a batch route may step down to the documented one)."""
        return FORM_FULL

    def _schema_field_name(self) -> str:
        return self._schema_field

    def _metadata_key(self) -> str:
        return "custom_id"

    def inlined_request_for(self, call: CallSpec) -> Any:
        """The `InlinedRequest` for one call. A Gemini request is a plain contents list plus a config."""
        from google.genai import types

        if call.rules is None:
            raise ValueError("the call carries no writing rules")
        parts = [types.Part(text=call_text(call))]
        picture = picture_for(call, GEMINI)
        if picture is not None:
            parts.append(types.Part.from_bytes(data=picture[0], mime_type=mime_type(picture[1])))
        settings: dict[str, Any] = {
            "system_instruction": call.rules.system,
            "response_mime_type": "application/json",
            "max_output_tokens": call.max_output_tokens,
        }
        form = self.answer_schema(call.rules)
        if form is not None:
            settings[self._schema_field_name()] = form
        if call.effort:
            settings["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel(call.effort.upper()))
        if picture is not None:
            settings["media_resolution"] = types.MediaResolution(f"MEDIA_RESOLUTION_{self._resolution.upper()}")
        return types.InlinedRequest(
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(**settings),
            metadata={self._metadata_key(): call.custom_id},
        )

    def prepare(self, calls: Sequence[CallSpec]) -> list[Request]:
        requests = []
        for call in calls:
            inlined = self.inlined_request_for(call)
            rules = call.rules
            assert rules is not None  # inlined_request_for raised otherwise
            picture_tokens = IMAGE_TOKENS[self._resolution] if call.image is not None else 0
            form = self.answer_schema(rules)
            requests.append(
                Request(
                    call_id=call.call_id,
                    custom_id=call.custom_id,
                    payload={
                        "request": inlined,
                        "model": call.model,
                        "rules_version": rules.version,
                        "system": rules.system,
                        "form": self._form(),
                    },
                    est_input_tokens=estimate_text_tokens(rules.system, call_text(call), canonical_json(form) if form is not None else "")
                    + picture_tokens,
                    max_output_tokens=call.max_output_tokens,
                    attempt=call.attempt,
                    size_bytes=len(inlined.model_dump_json(exclude_none=True).encode("utf-8")) + 1,
                )
            )
        return requests


# ---------------------------------------------------------------------------- batch


class GeminiBatchWriter(_GeminiCommon, Writer):
    """Batch mode with inline requests. See the module description."""

    def __init__(self, route_name: str, route: RouteConfig, *, client: Any | None = None, now: Any = None) -> None:
        Writer.__init__(self, route_name, route)
        self._setup(client, batch=True)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._step = FORM_DOCUMENTED if self._batch_form == "documented" else FORM_FULL
        self._caches: dict[str, dict[str, Any]] = {}  # "model|rules version" -> {name, expires, tokens}
        self._notes: list[tuple[str, dict[str, Any]]] = []

    @property
    def bills_cache_storage(self) -> bool:  # type: ignore[override]
        """True with the explicit cache on: the provider bills the hours the cache is kept."""
        return self._use_cache

    def split(self, requests: Sequence[Request]) -> list[list[Request]]:
        return split_requests(requests, self.route.max_batch_calls, BATCH_MAX_BYTES)

    # -- the form of a request

    def _form(self) -> int:
        return self._step

    def _schema_field_name(self) -> str:
        return "response_schema" if self._step >= FORM_DOCUMENTED else self._schema_field

    def _schema_mode_now(self) -> str:
        """The documented form sends the OpenAPI form of the schema, the one `response_schema` takes."""
        return "openapi" if self._step >= FORM_DOCUMENTED and self._schema_mode != "none" else self._schema_mode

    def _metadata_key(self) -> str:
        return "key" if self._step >= FORM_DOCUMENTED else "custom_id"

    def use_form(self, form: int) -> None:
        """Build the requests that follow in this form (0 the first one, 1 the documented one)."""
        self._step = form

    # -- the explicit cache

    def restore(self, notes: Sequence[Any]) -> None:
        for note in notes:
            if note.get("route") == self.route_name and note.get("key") and note.get("name"):
                self._caches[str(note["key"])] = {
                    "name": str(note["name"]),
                    "expires": str(note.get("expires", "")),
                    "tokens": note.get("tokens"),
                }

    def drain_notes(self) -> list[tuple[str, dict[str, Any]]]:
        notes, self._notes = self._notes, []
        return notes

    def _remember(
        self,
        key: str,
        name: str,
        expires: datetime,
        model: str,
        *,
        tokens: int | None,
        estimated: bool = False,
        created: datetime | None = None,
        extended_from: str | None = None,
    ) -> None:
        """Keep a cache in memory and queue the note that records it.

        The note carries what the storage fee needs: the cache's size in tokens and the stretch of
        its life it covers (from `created`, for a new cache, or from the earlier expiry, for one
        that was kept alive longer). `estimated` says the size is a guess because the provider
        did not report it.
        """
        self._caches[key] = {"name": name, "expires": _iso(expires), "tokens": tokens}
        note: dict[str, Any] = {
            "route": self.route_name,
            "key": key,
            "name": name,
            "expires": _iso(expires),
            "model": model,
            "tokens": tokens,
        }
        if estimated:
            note["tokens_estimated"] = True
        if created is not None:
            note["created"] = _iso(created)
        if extended_from:
            note["extended_from"] = extended_from
        self._notes.append(("cache_created", note))

    def _ensure_cache(self, model: str, rules_version: str, system: str) -> str:
        """The name of a live cache holding the rules for this model, created if none is known."""
        from google.genai import types

        key = f"{model}|{rules_version}"
        known = self._caches.get(key)
        if known:
            ends = _parse_iso(known["expires"])
            if ends is not None and ends > self._now() + CACHE_REUSE_MARGIN:
                return known["name"]
        cache = self._client().caches.create(
            model=model,
            config=types.CreateCachedContentConfig(
                system_instruction=system, ttl=f"{self._cache_ttl}s", display_name=f"rps-{rules_version}"[:128]
            ),
        )
        created = lookup(cache, "create_time") or self._now()
        expires = lookup(cache, "expire_time") or (created + timedelta(seconds=self._cache_ttl))
        tokens = count(lookup(cache, "usage_metadata", "total_token_count"))
        estimated = tokens == 0
        if estimated:
            tokens = estimate_text_tokens(system)  # the provider reported no size; a generous guess
        self._remember(key, str(lookup(cache, "name")), expires, model, tokens=tokens, estimated=estimated, created=created)
        return str(lookup(cache, "name"))

    def _extend_cache(self, name: str) -> None:
        """Keep a cache alive while a batch that refers to it is still running."""
        from google.genai import types

        for key, known in self._caches.items():
            if known["name"] != name:
                continue
            ends = _parse_iso(known["expires"])
            if ends is not None and ends - self._now() > timedelta(seconds=self._cache_ttl / 2):
                return
            updated = self._client().caches.update(name=name, config=types.UpdateCachedContentConfig(ttl=f"{self._cache_ttl}s"))
            expires = lookup(updated, "expire_time") or (self._now() + timedelta(seconds=self._cache_ttl))
            model = key.split("|", 1)[0]
            tokens = count(lookup(updated, "usage_metadata", "total_token_count")) or known.get("tokens")
            self._remember(key, name, expires, model, tokens=tokens, extended_from=known["expires"])
            return

    # -- the writer steps

    def submit(self, requests: Sequence[Request]) -> Handle:
        client = self._client()
        from google.genai import types

        inlined = [request.payload["request"] for request in requests]
        form = min(int(request.payload.get("form", FORM_FULL)) for request in requests)
        data: dict[str, Any] = {"form": form}
        try:
            if self._use_cache and form == FORM_FULL:
                first = requests[0].payload
                cache_name = self._ensure_cache(first["model"], first["rules_version"], first["system"])
                inlined = [self._with_cache(item, cache_name) for item in inlined]
                data["cache_name"] = cache_name
            job = client.batches.create(
                model=requests[0].payload["model"],
                src=inlined,
                config=types.CreateBatchJobConfig(display_name=f"rps-{digest_of([r.custom_id for r in requests])}"),
            )
        except RouteUnavailable:
            raise
        except Exception as error:
            kind, _retryable = classify_exception(error)
            if kind in (KIND_AUTH, KIND_BAD_REQUEST):
                raise RouteRejected(kind, describe_error(error, PROVIDER_ERROR_CHARS)) from None
            raise
        name = str(lookup(job, "name"))
        data["name"] = name
        return Handle(handle_id=name, call_ids=tuple(r.call_id for r in requests), mode="batch", data=data)

    @staticmethod
    def _with_cache(item: Any, cache_name: str) -> Any:
        """A copy of an inlined request that refers to the cache instead of carrying the rules."""
        config = item.config.model_copy(update={"system_instruction": None, "cached_content": cache_name})
        return item.model_copy(update={"config": config})

    def poll(self, handle: Handle) -> PollStatus:
        try:
            job = self._client().batches.get(name=handle.data["name"])
        except Exception as error:
            if http_status(error) == 404:
                return PollStatus("failed", "the provider no longer knows this batch")
            raise
        state = _name(lookup(job, "state"))
        if state in ("JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"):
            return PollStatus("ended")
        if state == "JOB_STATE_FAILED":
            return PollStatus("failed", inlined_error_text(lookup(job, "error")) if lookup(job, "error") is not None else None)
        if state == "JOB_STATE_CANCELLED":
            return PollStatus("canceled")
        if state == "JOB_STATE_EXPIRED":
            return PollStatus("expired", "the batch was not finished within 48 hours")
        cache_name = handle.data.get("cache_name")
        if cache_name:
            try:
                self._extend_cache(str(cache_name))
            except Exception:
                pass  # a missed extension shows up as failed requests, which are sent again
        return PollStatus("in_progress")

    def results(self, handle: Handle) -> list[CallResult]:
        job = self._client().batches.get(name=handle.data["name"])
        call_of = {custom_id(call_id): call_id for call_id in handle.call_ids}
        items: list[tuple[Any, str]] = []
        for item in lookup(job, "dest", "inlined_responses") or []:
            metadata = lookup(item, "metadata") or {}
            call_id = call_of.get(str(metadata.get("custom_id") or metadata.get("key")))
            if call_id is not None:
                items.append((item, call_id))
        found = [parse_inlined(item, call_id) for item, call_id in items]
        if items and all(_refused_as_invalid(item) for item, _call_id in items):
            return self._refused_as_a_whole(handle, found)
        return found

    def _refused_as_a_whole(self, handle: Handle, found: list[CallResult]) -> list[CallResult]:
        """Every request of a batch came back refused as an invalid argument: the form of the request is at fault.

        The calls are sent again in the documented form, unless the batch already was in it or the route is
        meant to stay in the first form: then the refusal is final and says where to look. A batch in which
        some requests came back and others did not is never read this way; that is the requests' own fault.
        """
        form = int(handle.data.get("form", FORM_FULL) or FORM_FULL)
        if form >= FORM_DOCUMENTED:
            note = (
                "the batch was in the form the batch pages document (schema in response_schema, metadata key, no explicit "
                f"cache); python -m rps.geminiprobe --batch --route {self.route_name} says which part of the request the service refuses"
            )
            return [_with_note(result, note) for result in found]
        if self._batch_form == "full":
            note = 'the route option "batch_form" is "full", so the calls are not sent again in the documented form'
            return [_with_note(result, note) for result in found]
        self._step = FORM_DOCUMENTED
        note = (
            "every request of the batch was refused as sent; the calls are sent again in the form the batch pages "
            "document (schema in response_schema, metadata key, no explicit cache)"
        )
        return [_with_note(result, note, retry=True) for result in found]

    def recover(
        self, requests: Sequence[Request], *, since: datetime | None = None, recorded: Collection[str] = ()
    ) -> Handle | None:
        """Find a batch created just before a crash, by the digest of its custom ids in its display name."""
        from google.genai import types

        if not requests:
            return None
        groups = {f"rps-{digest_of([r.custom_id for r in group])}": group for group in self.split(requests)}
        listing = self._client().batches.list(config=types.ListBatchJobsConfig(page_size=50))
        for index, job in enumerate(listing):
            if index >= 100:
                break
            group = groups.get(str(lookup(job, "display_name")))
            if group is None or _name(lookup(job, "state")) in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
                continue
            name = str(lookup(job, "name"))
            if name in recorded:
                continue
            return Handle(handle_id=name, call_ids=tuple(r.call_id for r in group), mode="batch", data={"name": name})
        return None


# ---------------------------------------------------------------------------- one call at a time


class GeminiSyncWriter(_GeminiCommon, PoolWriter):
    """One `generate_content` call per request, from a pool of workers (route kind "api")."""

    def __init__(self, route_name: str, route: RouteConfig, *, client: Any | None = None) -> None:
        PoolWriter.__init__(self, route_name, route)
        self._setup(client)

    def accepting(self) -> Acceptance:
        return self.unless_paused(_GeminiCommon.accepting(self))

    def send(self, request: Request) -> CallResult:
        inlined = request.payload["request"]
        try:
            response = self._client().models.generate_content(
                model=request.payload["model"], contents=inlined.contents, config=inlined.config
            )
        except RouteUnavailable:
            raise
        except Exception as error:
            return failure_from_exception(request.call_id, error)
        return parse_response(request.call_id, response, max_tokens=request.max_output_tokens)
