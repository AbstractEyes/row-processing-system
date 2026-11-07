"""DeepSeek writer: synchronous chat completions from a pool of workers (route kind "api").

DeepSeek has no batch service and no schema mode. Each call is one chat completion in JSON mode
(`response_format: json_object`): the system message holds the writing rules, which must contain
the word "json" and an example row (the rules are checked for the word before a run starts); the
user message holds the task text and, where the routing table allows, a picture as a data URL
(user messages only; the `detail` route option, "low" or "high", default "low"). The assessor
checks the answer, since the provider does not. Blank content, even with a normal finish, counts
as a failed parse and is sent once more.

Calls go out only off-peak: not in Monday to Friday 01:00 to 04:00 or 06:00 to 10:00 UTC, when
tokens cost double. The route reports itself closed "for now" during those hours, calls already
queued wait at the edge, and every result is marked with the tier it was billed at (`peak` or
`offpeak`, by the time it was sent). The route option `offpeak_only` (default true) turns the
gate off, for a small test at any hour.

The route is off unless the configuration turns it on (`enabled: true`). The key is read from
DEEPSEEK_API_KEY and never logged. Workers are the route's `workers` setting (cap 2,500).
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, time as Time, timezone
from typing import Any, Callable, Sequence

from ..config import ConfigError, RouteConfig
from ..failures import KIND_PROVIDER
from .base import Acceptance, CallResult, CallSpec, Request, RouteUnavailable
from .common import (
    STOP_BLOCKED,
    STOP_COMPLETE,
    STOP_OTHER,
    STOP_TRUNCATED,
    api_key,
    call_text,
    check_options,
    count,
    data_url,
    estimate_text_tokens,
    failure_from_exception,
    finished_call,
    key_acceptance,
    lookup,
    option,
    picture_for,
)
from ..ids import canonical_json
from ..ledger import Usage
from ..prompting import WriterRules
from .pool import PoolWriter
from .schemas import DEEPSEEK

API_KEY_ENV = "DEEPSEEK_API_KEY"
BASE_URL = "https://api.deepseek.com"
DETAILS = ("low", "high")
IMAGE_TOKENS = 1024  # the provider's cap per picture
TIER_PEAK = "peak"
TIER_OFFPEAK = "offpeak"

# Peak hours, UTC, Monday to Friday: (start, end) with the end not included.
PEAK_WINDOWS: tuple[tuple[Time, Time], ...] = ((Time(1, 0), Time(4, 0)), (Time(6, 0), Time(10, 0)))

_STOP_BY_FINISH = {"stop": STOP_COMPLETE, "length": STOP_TRUNCATED, "content_filter": STOP_BLOCKED}


# ---------------------------------------------------------------------------- hours


def peak_ends(moment: datetime) -> datetime | None:
    """When the peak window that `moment` is in ends, or None when it is off-peak."""
    moment = moment.astimezone(timezone.utc)
    if moment.weekday() >= 5:
        return None
    clock = moment.time()
    for start, end in PEAK_WINDOWS:
        if start <= clock < end:
            return moment.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return None


def tier_at(moment: datetime) -> str:
    return TIER_PEAK if peak_ends(moment) is not None else TIER_OFFPEAK


# ---------------------------------------------------------------------------- parsing


def usage_of(usage: Any) -> Usage:
    """DeepSeek's usage as disjoint billable buckets.

    Cache hits and misses partition the prompt; reasoning tokens are part of the completion
    tokens and are split off. Without the cache fields the whole prompt counts as plain input.
    """
    hit = count(lookup(usage, "prompt_cache_hit_tokens"))
    miss = lookup(usage, "prompt_cache_miss_tokens")
    prompt = count(lookup(usage, "prompt_tokens"))
    plain = count(miss) if miss is not None else max(0, prompt - hit)
    completion = count(lookup(usage, "completion_tokens"))
    reasoning = min(count(lookup(usage, "completion_tokens_details", "reasoning_tokens")), completion)
    return Usage(input_tokens=plain, cache_read_tokens=hit, output_tokens=completion - reasoning, reasoning_tokens=reasoning)


def parse_completion(
    call_id: str, completion: Any, *, max_tokens: int | None = None, minutes: float | None = None, tier: str | None = None
) -> CallResult:
    """A chat completion as a call result."""
    usage = usage_of(lookup(completion, "usage"))
    choices = lookup(completion, "choices") or []
    common: dict[str, Any] = {"usage": usage, "max_tokens": max_tokens, "minutes": minutes, "tier": tier}
    if not choices:
        return finished_call(call_id, text=None, stop=STOP_OTHER, detail="the response has no choices", **common)
    choice = choices[0]
    finish = lookup(choice, "finish_reason")
    if finish == "insufficient_system_resource":
        # The service was too busy to finish the call; asking again may work.
        return CallResult(
            call_id=call_id,
            output=None,
            usage=usage,
            error="the service was too busy to finish the call",
            retryable=True,
            minutes=minutes,
            kind=KIND_PROVIDER,
            tier=tier,
        )
    stop = _STOP_BY_FINISH.get(finish, STOP_OTHER)
    text = lookup(choice, "message", "content")
    detail = "" if stop != STOP_OTHER else str(finish or "no finish reason")
    if stop == STOP_BLOCKED:
        detail = "content filter"
    return finished_call(call_id, text=text if stop == STOP_COMPLETE else None, stop=stop, detail=detail, **common)


# ---------------------------------------------------------------------------- the writer


class DeepSeekWriter(PoolWriter):
    """Synchronous chat completions with an off-peak gate. See the module description."""

    def __init__(
        self,
        route_name: str,
        route: RouteConfig,
        *,
        client: Any | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(route_name, route)
        check_options(route, route_name, {"detail", "offpeak_only"})
        self._detail = option(route, route_name, "detail", "low", choices=DETAILS, kind=str)
        self._offpeak_only = bool(option(route, route_name, "offpeak_only", True, kind=bool))
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._client_obj = client
        self._client_lock = threading.Lock()

    # -- checks

    def accepting(self) -> Acceptance:
        base = key_acceptance(self.route, API_KEY_ENV)
        if not base.ok:
            return base
        if self._offpeak_only:
            ends = peak_ends(self._now())
            if ends is not None:
                return Acceptance(False, f"peak hours (double price) until {ends.strftime('%H:%M')} UTC", later=True)
        return self.unless_paused(Acceptance(True))

    def gate(self) -> float | None:
        if not self._offpeak_only:
            return None
        moment = self._now()
        ends = peak_ends(moment)
        if ends is None:
            return None
        return max(1.0, (ends - moment).total_seconds())

    def check_rules(self, rules: WriterRules) -> None:
        if "json" not in rules.system.lower():
            raise ConfigError(
                f"route {self.route_name!r}: DeepSeek's JSON mode needs the word 'json' (and an example row) in the writing rules"
            )

    def _client(self) -> Any:
        with self._client_lock:
            if self._client_obj is None:
                key = api_key(API_KEY_ENV)
                if key is None:
                    raise RouteUnavailable(f"{API_KEY_ENV} is not set")
                import openai  # imported here: the SDK is slow to load

                options: dict[str, Any] = {"api_key": key, "base_url": BASE_URL, "max_retries": self.route.max_retries}
                if self.route.timeout_seconds:
                    options["timeout"] = self.route.timeout_seconds
                self._client_obj = openai.OpenAI(**options)
            return self._client_obj

    # -- requests

    def arguments_for(self, call: CallSpec) -> dict[str, Any]:
        """The `chat.completions.create` arguments for one call."""
        if call.rules is None:
            raise ValueError("the call carries no writing rules")
        text = call_text(call)
        picture = picture_for(call, DEEPSEEK)
        user: Any = text
        if picture is not None:
            user = [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": data_url(*picture), "detail": self._detail}},
            ]
        return {
            "model": call.model,
            "messages": [{"role": "system", "content": call.rules.system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "max_tokens": call.max_output_tokens,
        }

    def prepare(self, calls: Sequence[CallSpec]) -> list[Request]:
        requests = []
        for call in calls:
            arguments = self.arguments_for(call)
            rules = call.rules
            assert rules is not None  # arguments_for raised otherwise
            picture_tokens = IMAGE_TOKENS if call.image is not None else 0
            requests.append(
                Request(
                    call_id=call.call_id,
                    custom_id=call.custom_id,
                    payload={"arguments": arguments},
                    est_input_tokens=estimate_text_tokens(rules.system, call_text(call)) + picture_tokens,
                    max_output_tokens=call.max_output_tokens,
                    attempt=call.attempt,
                    size_bytes=len(canonical_json(arguments).encode("utf-8")),
                )
            )
        return requests

    def send(self, request: Request) -> CallResult:
        tier = tier_at(self._now())
        try:
            completion = self._client().chat.completions.create(**request.payload["arguments"])
        except RouteUnavailable:
            raise
        except Exception as error:
            return replace(failure_from_exception(request.call_id, error), tier=tier)
        return parse_completion(request.call_id, completion, max_tokens=request.max_output_tokens, tier=tier)
