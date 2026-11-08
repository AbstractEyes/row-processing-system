"""A real google-genai client whose HTTP requests go to a recorder instead of the network.

The SDK builds the URL and the body of every request itself (the API version in the path, the names of the
fields), and those are what the Gemini service answers to. A test that goes through the SDK's own code, down to
the HTTP call, sees what a live run sends. Nothing leaves the process.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from google import genai  # noqa: E402

REAL_CLIENT = genai.Client  # kept apart from the patch, so patching twice in one test does not stack
JOB ={"name": "batches/job-1", "state": "JOB_STATE_PENDING"}
CACHE = {"name": "cachedContents/cache-1", "model": "models/gemini-3.8-flash", "usageMetadata": {"totalTokenCount": 5200}}


class Wire:
    """Records each request the SDK makes (method, URL, JSON body) and answers it with a canned body.

    A request whose URL has one of the `missing` parts in it is answered the way the live service answered a
    path it does not have: 404 and an empty body.
    """

    def __init__(self, job: dict[str, Any] | None = None, cache: dict[str, Any] | None = None, missing: tuple[str, ...] = ()) -> None:
        self.job = job or JOB
        self.cache = cache or CACHE
        self.missing = missing
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, request: Any) -> Any:
        body = json.loads(request.content) if request.content else None
        url = str(request.url)
        self.requests.append((request.method, url, body))
        if any(part in url for part in self.missing):
            return httpx2.Response(404)
        return httpx2.Response(200, json=self.cache if "cachedContents" in url else self.job)

    def to(self, part: str) -> list[tuple[str, str, Any]]:
        """The requests whose URL has `part` in it."""
        return [item for item in self.requests if part in item[1]]


def send_through(monkeypatch: pytest.MonkeyPatch, wire: Wire) -> None:
    """Make every client a writer builds talk to `wire`; the key is a made-up one."""
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")

    def build(*args: Any, **kwargs: Any) -> Any:
        options = kwargs["http_options"].model_copy(update={"httpx_client": httpx2.Client(transport=httpx2.MockTransport(wire))})
        return REAL_CLIENT(*args, **{**kwargs, "http_options": options})

    monkeypatch.setattr(genai, "Client", build)


def names_in(node: Any, wanted: set[str], path: str = "") -> list[str]:
    """Every path at which one of the keys `wanted` appears in a nested structure of dicts and lists."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in wanted:
                found.append(f"{path}/{key}")
            found.extend(names_in(value, wanted, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(names_in(value, wanted, f"{path}[{index}]"))
    return found
