"""Helpers the adapters share: keys, pictures, options, errors and finished-call results.

Nothing here talks to a provider. Keys are read from environment variables only, and
every error text that leaves this module has been through `redact`.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Collection, Mapping

from ..config import ConfigError, RouteConfig
from ..failures import (
    KIND_AUTH,
    KIND_BAD_REQUEST,
    KIND_BLOCKED,
    KIND_INVALID_OUTPUT,
    KIND_PROVIDER,
    KIND_REFUSED,
    KIND_TRUNCATED,
    KIND_UNEXPECTED,
    PROVIDER_ERROR_CHARS,
)
from ..ledger import Usage
from ..prompting import render_task_text
from ..routing import check_image_allowed
from ..store.remote import describe_error
from .base import Acceptance, CallResult, CallSpec

MIME_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}

# Error class names that mean "the network or the service hiccuped; asking again may work".
_TRANSIENT_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "TimeoutException",
        "NetworkError",
        "TransportError",
        "ServerError",
        "InternalServerError",
        "ServiceUnavailableError",
        "OverloadedError",
        "RateLimitError",
        "GatewayTimeoutError",
        "DeadlineExceededError",
        "RequestTimeoutError",
    }
)


# ---------------------------------------------------------------------------- keys and options


def api_key(env_name: str) -> str | None:
    """The key in an environment variable, or None when it is missing or blank."""
    value = os.environ.get(env_name, "").strip()
    return value or None


def key_acceptance(route: RouteConfig, env_name: str) -> Acceptance:
    """A route takes calls only while it is switched on and its key is in the environment."""
    if not route.is_enabled:
        return Acceptance(False, "it is switched off in the configuration")
    if api_key(env_name) is None:
        return Acceptance(False, f"{env_name} is not set")
    return Acceptance(True)


# ---------------------------------------------------------------------------- calls


def call_text(call: CallSpec) -> str:
    """The user text of a call: the controller's rendering, or the same rendering made here."""
    if call.task_text is not None:
        return call.task_text
    return render_task_text(
        call.spec, call.feedback, rules=call.rules, repair=call.repair_of is not None, picture=call.image is not None
    )


def picture_for(call: CallSpec, provider: str) -> tuple[bytes, str] | None:
    """The picture a call carries as (bytes, extension), after the routing table allowed it for this provider.

    Raises ImageRoutingError for a picture that may not go to the provider; adapters call this
    on every call, so no picture reaches a provider by another path.
    """
    if call.image is None:
        return None
    check_image_allowed(call.image_class, provider, call.image_policy)
    return call.image, call.image_ext or "jpg"


def check_options(route: RouteConfig, route_name: str, allowed: Collection[str]) -> None:
    """Raise ConfigError for a route option the adapter does not know (a typo would be silent otherwise)."""
    unknown = sorted(set(route.options) - set(allowed))
    if unknown:
        raise ConfigError(f"route {route_name!r}: unknown option(s) {unknown}; this adapter knows {sorted(allowed)}")


def option(
    route: RouteConfig,
    route_name: str,
    name: str,
    default: Any,
    *,
    choices: Collection[Any] | None = None,
    kind: type | tuple[type, ...] | None = None,
) -> Any:
    """A route option with a default, checked against the allowed choices and type."""
    value = route.options.get(name, default)
    if value is None:
        return default
    if kind is not None:
        kinds = kind if isinstance(kind, tuple) else (kind,)
        if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
            names = " or ".join(k.__name__ for k in kinds)
            raise ConfigError(f"route {route_name!r}: option {name!r} must be {names}, got {value!r}")
    if choices is not None and value not in choices:
        raise ConfigError(f"route {route_name!r}: option {name!r} must be one of {sorted(map(str, choices))}, got {value!r}")
    return value


# ---------------------------------------------------------------------------- pictures


def mime_type(ext: str | None) -> str:
    """The media type of a picture from its file extension."""
    key = (ext or "").lower().lstrip(".")
    try:
        return MIME_TYPES[key]
    except KeyError:
        raise ValueError(f"unsupported picture type {ext!r}; use one of {sorted(MIME_TYPES)}") from None


def base64_text(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def data_url(data: bytes, ext: str | None) -> str:
    """A picture as a data URL, the form chat and response APIs take inline."""
    return f"data:{mime_type(ext)};base64,{base64_text(data)}"


# ---------------------------------------------------------------------------- sizes


def estimate_text_tokens(*texts: str) -> int:
    """A generous token count for text: one token per three characters, rounded up."""
    return sum((len(text) + 2) // 3 for text in texts)


# ---------------------------------------------------------------------------- errors


def http_status(error: BaseException) -> int | None:
    """The HTTP status an SDK error carries, if any (`status_code`, `response.status_code` or `code`)."""
    for holder in (error, getattr(error, "response", None)):
        for name in ("status_code", "code"):
            value = getattr(holder, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                return value
    return None


def _spent_balance(error: BaseException, status: int | None) -> bool:
    text = f"{getattr(error, 'code', '')} {getattr(error, 'type', '')} {error}".lower()
    return status == 402 or any(
        phrase in text
        for phrase in ("insufficient_quota", "exceeded your current quota", "credit balance is too low", "insufficient balance")
    )


def classify_exception(error: BaseException) -> tuple[str, bool]:
    """(failure kind, retryable) for an exception an SDK call raised.

    A rejected key or a spent balance, and a request the provider calls malformed, are
    not retryable (they stop the route); rate limits, server errors, timeouts and dropped
    connections are. Anything else is a bug here and is not retried.
    """
    status = http_status(error)
    if _spent_balance(error, status) or status in (401, 403):
        return KIND_AUTH, False
    if status in (400, 404, 413, 422):
        return KIND_BAD_REQUEST, False
    if status is not None and (status in (408, 409, 425, 429, 529) or status >= 500):
        return KIND_PROVIDER, True
    if any(cls.__name__ in _TRANSIENT_NAMES for cls in type(error).__mro__):
        return KIND_PROVIDER, True
    return KIND_UNEXPECTED, False


def is_rate_limit(error: BaseException) -> bool:
    """True when a provider turned a call away for its rate limit: HTTP 429 that is not a spent balance.

    The provider's own client has already retried the call a few times (with the provider's
    `retry-after` pause) before it raises this, so what reaches here is a limit that stayed.
    """
    status = http_status(error)
    if _spent_balance(error, status):
        return False
    if status is not None:
        return status == 429
    return any(cls.__name__ == "RateLimitError" for cls in type(error).__mro__)


def failure_from_exception(call_id: str, error: BaseException, *, minutes: float | None = None) -> CallResult:
    """A failed call result for an exception raised while sending one call."""
    kind, retryable = classify_exception(error)
    return CallResult(
        call_id=call_id,
        output=None,
        error=describe_error(error, PROVIDER_ERROR_CHARS),
        retryable=retryable,
        minutes=minutes,
        kind=kind,
        rate_limited=is_rate_limit(error),
    )


_ANTHROPIC_ERROR_KINDS = {
    "invalid_request_error": (KIND_BAD_REQUEST, False),
    "not_found_error": (KIND_BAD_REQUEST, False),
    "request_too_large": (KIND_BAD_REQUEST, False),
    "authentication_error": (KIND_AUTH, False),
    "permission_error": (KIND_AUTH, False),
    "billing_error": (KIND_AUTH, False),
    "rate_limit_error": (KIND_PROVIDER, True),
    "api_error": (KIND_PROVIDER, True),
    "overloaded_error": (KIND_PROVIDER, True),
    "timeout_error": (KIND_PROVIDER, True),
    "gateway_timeout_error": (KIND_PROVIDER, True),
}


def classify_error_type(type_name: str | None) -> tuple[str, bool]:
    """(kind, retryable) for the `type` of a provider's error object (Anthropic's batch results use these)."""
    return _ANTHROPIC_ERROR_KINDS.get(str(type_name), (KIND_UNEXPECTED, True))


# ---------------------------------------------------------------------------- finished calls

STOP_COMPLETE = "complete"
STOP_TRUNCATED = "truncated"
STOP_REFUSED = "refused"
STOP_BLOCKED = "blocked"
STOP_OTHER = "other"


def finished_call(
    call_id: str,
    *,
    text: str | None,
    usage: Usage,
    stop: str,
    detail: str = "",
    max_tokens: int | None = None,
    minutes: float | None = None,
    tier: str | None = None,
) -> CallResult:
    """A model's answer as a call result.

    `stop` says how the answer ended: complete (text is the answer; blank text is a failed
    parse), truncated (the token limit cut it off: retried once with a larger limit),
    refused or blocked (recorded, not retried on this route), other (nothing here expects it).
    The call's usage is kept in every case: the provider billed it.
    """
    common: dict[str, Any] = {"call_id": call_id, "usage": usage, "minutes": minutes, "tier": tier, "output": None}
    detail = detail.strip()
    if stop == STOP_COMPLETE:
        if text is None or not text.strip():
            return CallResult(
                **common, error="the model returned no text", retryable=True, kind=KIND_INVALID_OUTPUT, stop_reason=stop
            )
        common["output"] = text
        return CallResult(**common, stop_reason=stop)
    if stop == STOP_TRUNCATED:
        limit = f" ({max_tokens} tokens)" if max_tokens else ""
        return CallResult(
            **common, error=f"the answer was cut off at the token limit{limit}", retryable=True, kind=KIND_TRUNCATED, stop_reason=stop
        )
    if stop == STOP_REFUSED:
        said = f": {detail[:160]}" if detail else ""
        return CallResult(**common, error=f"the model declined to answer{said}", retryable=False, kind=KIND_REFUSED, stop_reason=stop)
    if stop == STOP_BLOCKED:
        why = f" ({detail[:80]})" if detail else ""
        return CallResult(**common, error=f"a safety filter stopped the answer{why}", retryable=False, kind=KIND_BLOCKED, stop_reason=stop)
    why = f": {detail[:120]}" if detail else ""
    return CallResult(
        **common, error=f"the answer ended for a reason nothing here expects{why}", retryable=False, kind=KIND_UNEXPECTED, stop_reason=stop
    )


def lookup(data: Any, *path: str) -> Any:
    """Walk attribute or key names through SDK objects and plain dicts; None when any step is missing."""
    node = data
    for name in path:
        if node is None:
            return None
        node = node.get(name) if isinstance(node, Mapping) else getattr(node, name, None)
    return node


def count(value: Any) -> int:
    """A token count from an SDK field that may be None or missing."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0
