"""The format block of a rule set is for the provider that has no schema mode, and for no other.

DeepSeek's JSON mode guarantees valid JSON and nothing about the keys, so its writer appends the block
to the system message (see test_adapter_deepseek). Every other writer takes a schema: what it sends, and
what it estimates the call at, must be the same with a block as without one.
"""

from __future__ import annotations

import json

import pytest

from rps.providers import make_writer

from adapter_helpers import call, route, rules

BLOCK = "Answer with a json object. Give depth as a word (shallow, medium or deep), never as a number."

ROUTES = [
    ("anthropic", "claude-haiku-5-5", "batch"),
    ("anthropic", "claude-haiku-5-5", "api"),
    ("openai", "gpt-6-luna", "batch"),
    ("openai", "gpt-6-luna", "api"),
    ("gemini", "gemini-3.8-flash", "batch"),
    ("gemini", "gemini-3.8-flash", "api"),
]


def requests_for(provider, model, kind, **rule_fields):
    writer = make_writer("r1", route(provider, model, kind, enabled=True))
    spec = call("001", provider=provider, model=model, kind=kind, rules=rules(**rule_fields))
    return writer.prepare([spec])


@pytest.mark.parametrize("provider, model, kind", ROUTES)
def test_a_provider_with_a_schema_sends_the_same_request_with_or_without_the_block(provider, model, kind):
    [plain] = requests_for(provider, model, kind)
    [shaped] = requests_for(provider, model, kind, json_format=BLOCK)
    assert json.dumps(shaped.payload, sort_keys=True, default=str) == json.dumps(plain.payload, sort_keys=True, default=str)
    assert BLOCK not in json.dumps(shaped.payload, default=str)
    assert shaped.est_input_tokens == plain.est_input_tokens and shaped.size_bytes == plain.size_bytes
