"""Why a call produced no usable output.

Every failed call is recorded with one of these kinds. The kind decides what happens
next: a truncated answer is retried once with a larger token limit, a refusal or a
safety block is recorded and not retried on the same route, a request the provider
calls malformed or a key it rejects stops the route for the run, a prompt over the
provider's long-prompt threshold is refused before it is sent, and everything else
transient is retried as a rebound.
"""

from __future__ import annotations

KIND_TRUNCATED = "truncated"  # the answer stopped at the token limit
KIND_REFUSED = "refused"  # the model declined; not retried on the same route
KIND_BLOCKED = "blocked"  # a safety filter stopped the answer; not retried on the same route
KIND_EXPIRED = "expired"  # the provider let the request lapse
KIND_CANCELED = "canceled"  # the request was canceled before it ran
KIND_INVALID_OUTPUT = "invalid_output"  # blank, cut off or not the expected JSON
KIND_PROVIDER = "provider_error"  # rate limit, server error, timeout, dropped connection
KIND_BAD_REQUEST = "bad_request"  # a bug in the request: stops the route
KIND_AUTH = "auth"  # a key the provider rejects, or a spent balance: stops the route
KIND_UNEXPECTED = "unexpected"  # a stop reason or state nothing here expects
KIND_LONG_PROMPT = "long_prompt"  # the prompt is over the provider's long-prompt threshold; refused before sending
KIND_MISMATCH = "batch_mismatch"  # a batch found again after a crash did not hold the call; sent again

# How much of a provider's error text a failed call keeps. A refused request often names its
# cause only in the details after the first sentence, so the text is kept nearly whole.
PROVIDER_ERROR_CHARS = 1500

FAILURE_KINDS: tuple[str, ...] = (
    KIND_TRUNCATED,
    KIND_REFUSED,
    KIND_BLOCKED,
    KIND_EXPIRED,
    KIND_CANCELED,
    KIND_INVALID_OUTPUT,
    KIND_PROVIDER,
    KIND_BAD_REQUEST,
    KIND_AUTH,
    KIND_UNEXPECTED,
    KIND_LONG_PROMPT,
    KIND_MISMATCH,
)

# A failure of one of these kinds stops the route for the rest of the run.
STOP_ROUTE_KINDS: frozenset[str] = frozenset({KIND_BAD_REQUEST, KIND_AUTH})
