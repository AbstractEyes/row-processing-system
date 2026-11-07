"""Writers: one adapter per model provider, plus a fake for tests."""

from __future__ import annotations

from ..config import ConfigError, RouteConfig
from .anthropic_api import AnthropicWriter
from .base import (
    CallResult,
    CallSpec,
    FakeOutcome,
    FakeProvider,
    FakeWriter,
    Handle,
    PollStatus,
    ProviderError,
    Request,
    SimulatedCrash,
    Writer,
)
from .gemini_api import GeminiWriter
from .openai_api import OpenAIWriter

_WRITERS: dict[str, type[Writer]] = {
    "anthropic": AnthropicWriter,
    "openai": OpenAIWriter,
    "gemini": GeminiWriter,
    "fake": FakeWriter,
}


def make_writer(route_name: str, route: RouteConfig) -> Writer:
    """Build the writer for a route from its provider name."""
    try:
        writer_class = _WRITERS[route.provider]
    except KeyError:
        raise ConfigError(f"route {route_name!r}: no adapter for provider {route.provider!r}") from None
    return writer_class(route_name, route)


__all__ = [
    "AnthropicWriter",
    "CallResult",
    "CallSpec",
    "FakeOutcome",
    "FakeProvider",
    "FakeWriter",
    "GeminiWriter",
    "Handle",
    "OpenAIWriter",
    "PollStatus",
    "ProviderError",
    "Request",
    "SimulatedCrash",
    "Writer",
    "make_writer",
]
