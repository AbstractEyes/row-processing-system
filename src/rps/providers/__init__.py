"""Writers: one adapter per model provider and access path, plus a fake for tests.

    provider    route kind "batch"                    route kind "api" (one call at a time)
    anthropic   AnthropicBatchWriter (Message Batches)   AnthropicSyncWriter
    openai      OpenAIBatchWriter (Batch API)            OpenAISyncWriter
    gemini      GeminiBatchWriter (inline batches)       GeminiSyncWriter
    deepseek    (no batch service)                       DeepSeekWriter (off-peak gate)
    fake        FakeWriter, for tests and dry runs without a network

`make_writer` picks the writer for a route from its provider and kind.
"""

from __future__ import annotations

from ..config import ConfigError, RouteConfig
from .anthropic_api import AnthropicBatchWriter, AnthropicSyncWriter
from .base import (
    Acceptance,
    CallResult,
    CallSpec,
    FakeOutcome,
    FakeProvider,
    FakeWriter,
    Handle,
    PollStatus,
    ProviderError,
    Request,
    RouteRejected,
    RouteUnavailable,
    SimulatedCrash,
    Writer,
)
from .deepseek_api import DeepSeekWriter
from .gemini_api import GeminiBatchWriter, GeminiSyncWriter
from .openai_api import OpenAIBatchWriter, OpenAISyncWriter

_WRITERS: dict[tuple[str, str], type[Writer]] = {
    ("anthropic", "batch"): AnthropicBatchWriter,
    ("anthropic", "api"): AnthropicSyncWriter,
    ("openai", "batch"): OpenAIBatchWriter,
    ("openai", "api"): OpenAISyncWriter,
    ("gemini", "batch"): GeminiBatchWriter,
    ("gemini", "api"): GeminiSyncWriter,
    ("deepseek", "api"): DeepSeekWriter,
}
_PROVIDERS = {provider for provider, _kind in _WRITERS} | {"fake"}


def make_writer(route_name: str, route: RouteConfig) -> Writer:
    """Build the writer for a route from its provider name and kind."""
    if route.provider == "fake":
        return FakeWriter(route_name, route)
    try:
        writer_class = _WRITERS[(route.provider, route.kind)]
    except KeyError:
        if route.provider not in _PROVIDERS:
            raise ConfigError(f"route {route_name!r}: no adapter for provider {route.provider!r}") from None
        offered = sorted(kind for provider, kind in _WRITERS if provider == route.provider)
        raise ConfigError(
            f"route {route_name!r}: provider {route.provider!r} has no {route.kind!r} route; use kind {' or '.join(map(repr, offered))}"
        ) from None
    return writer_class(route_name, route)


__all__ = [
    "Acceptance",
    "AnthropicBatchWriter",
    "AnthropicSyncWriter",
    "CallResult",
    "CallSpec",
    "DeepSeekWriter",
    "FakeOutcome",
    "FakeProvider",
    "FakeWriter",
    "GeminiBatchWriter",
    "GeminiSyncWriter",
    "Handle",
    "OpenAIBatchWriter",
    "OpenAISyncWriter",
    "PollStatus",
    "ProviderError",
    "Request",
    "RouteRejected",
    "RouteUnavailable",
    "SimulatedCrash",
    "Writer",
    "make_writer",
]
