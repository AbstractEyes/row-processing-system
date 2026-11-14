"""The standard routes: which provider, model and access path each named route uses.

    route           provider    model              path                      pictures
    haiku-batch     anthropic   claude-haiku-5-5   Message Batches           public common, COCO, FFHQ, museum
    luna-batch      openai      gpt-6-luna         Batch API (/v1/responses) public common, COCO, FFHQ, museum
    flash-batch     gemini      gemini-3.8-flash   inline batch              public common, COCO, FFHQ, museum
    deepseek-sync   deepseek    deepseek-flash     one call at a time        public common, COCO, FFHQ, museum
    luna-sync                                      one call at a time: the route GPT is used through, at any size
    haiku-sync, flash-sync                         one call at a time, for small tests

The effort of each route is the provider's own default for the model (medium); the probe phase
compares levels. DeepSeek stays switched off until a configuration turns it on. A configuration
names routes of its own (`routes` in the configuration); these are the starting points for it
and the routes the dry-run tool tests.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import ConfigError, RouteConfig

STANDARD_ROUTES: Mapping[str, Mapping[str, Any]] = {
    "haiku-batch": {"provider": "anthropic", "model": "claude-haiku-5-5", "kind": "batch", "effort": "medium"},
    "haiku-sync": {"provider": "anthropic", "model": "claude-haiku-5-5", "kind": "api", "effort": "medium"},
    "luna-batch": {"provider": "openai", "model": "gpt-6-luna", "kind": "batch", "effort": "medium"},
    "luna-sync": {"provider": "openai", "model": "gpt-6-luna", "kind": "api", "effort": "medium"},
    "flash-batch": {"provider": "gemini", "model": "gemini-3.8-flash", "kind": "batch", "effort": "medium"},
    "flash-sync": {"provider": "gemini", "model": "gemini-3.8-flash", "kind": "api", "effort": "medium"},
    "deepseek-sync": {"provider": "deepseek", "model": "deepseek-flash", "kind": "api", "effort": None},
}


def standard_route(name: str, **overrides: Any) -> RouteConfig:
    """The route configuration for a standard route name, with any setting overridden.

    Its budget and caps are zero unless the overrides give them: a route spends nothing
    until a configuration says how much it may.
    """
    try:
        fields = dict(STANDARD_ROUTES[name])
    except KeyError:
        raise ConfigError(f"unknown standard route {name!r}; use one of {sorted(STANDARD_ROUTES)}") from None
    fields.update(overrides)
    return RouteConfig(**fields)
