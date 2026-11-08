"""The tool that sends the Gemini writer's exact request once for each form of the row schema."""

from __future__ import annotations

import copy
import json

import pytest
from google.genai import types

from rps import rules as pool
from rps.geminiprobe import PROBE_ORDER, main, probe_gemini
from rps.ledger import PriceTable

from adapter_helpers import ApiError, Namespace

MODEL = "gemini-3.8-flash"


def generated(text='{"skip": false, "reason": "", "class": "", "rows": []}', finish="STOP"):
    return types.GenerateContentResponse.model_validate(
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finish_reason": finish}],
            "usage_metadata": {"prompt_token_count": 6000, "candidates_token_count": 40, "total_token_count": 6040},
        }
    )


class Strict:
    """A fake `generate_content` that refuses any schema with a list of allowed values, as the live service did."""

    def __init__(self, refuse_plain=False):
        self.sent = []
        self.refuse_plain = refuse_plain

    def __call__(self, *, model, contents, config):
        schema = config.response_json_schema
        self.sent.append(schema)
        if schema is not None and (self.refuse_plain or "enum" in json.dumps(schema)):
            raise ApiError(400, "400 INVALID_ARGUMENT. {'error': {'message': 'enum[0]: cannot be empty', 'status': 'INVALID_ARGUMENT'}}")
        return generated()


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")


def probe(strict, **kwargs):
    lines = []
    report = probe_gemini("flash-batch", client=Namespace(models=Namespace(generate_content=strict)), printer=lines.append, **kwargs)
    return report, lines


def test_the_probe_sends_the_exact_request_once_per_form_and_stops_at_the_first_one_taken(with_key):
    strict = Strict()
    report, lines = probe(strict)
    assert [outcome.mode for outcome in report.outcomes] == ["exact", "plain"]
    assert [o.accepted for o in report.outcomes] == [False, True]
    assert report.accepted_mode == "plain" and report.ok and report.refused is None
    assert len(strict.sent) == 2 and "enum" in json.dumps(strict.sent[0]) and "enum" not in json.dumps(strict.sent[1])
    text = "\n".join(lines)
    assert "enum[0]: cannot be empty" in text  # the provider's own words are printed
    assert 'RESULT: Gemini took the schema in the form "plain" and refused \'exact\'' in text
    assert "not-a-real-key" not in text
    assert report.outcomes[1].cost_usd > 0 and report.outcomes[1].answer_head.startswith('{"skip"')


def test_the_probe_can_try_every_form_and_falls_through_to_no_schema(with_key):
    strict = Strict(refuse_plain=True)
    report, _lines = probe(strict, stop_at_first=False)
    assert [o.mode for o in report.outcomes] == list(PROBE_ORDER)
    assert [o.accepted for o in report.outcomes] == [False, False, True]
    assert strict.sent[2] is None and report.accepted_mode == "none"


def test_the_probe_says_so_when_every_form_is_refused(with_key):
    def refuse_all(*, model, contents, config):
        raise ApiError(400, "the request is refused for another reason")

    report, lines = probe(refuse_all)
    assert not report.ok and report.accepted_mode is None and len(report.outcomes) == 3
    assert "refused every form" in lines[-1]


def test_the_probe_sends_the_task_text_and_rules_of_the_pool_with_the_routes_settings(with_key):
    seen = {}

    def keep(*, model, contents, config):
        seen.update(model=model, text=contents[0].parts[0].text, config=config)
        return generated()

    probe(keep, modes=("plain",))
    item = pool.examples()[0]["item"]
    assert seen["model"] == MODEL and item["ids"][0] in seen["text"]
    assert seen["config"].system_instruction == pool.writer_system() and seen["config"].max_output_tokens == 8192
    assert seen["config"].thinking_config.thinking_level == types.ThinkingLevel.MEDIUM


def test_the_probe_takes_an_item_of_the_callers(with_key):
    seen = {}

    def keep(*, model, contents, config):
        seen["text"] = contents[0].parts[0].text
        return generated()

    item = copy.deepcopy(pool.examples()[2]["item"])
    probe(keep, modes=("none",), item=item)
    assert item["ids"][0] in seen["text"]


def test_the_probe_refuses_to_start_before_sending_anything(monkeypatch, with_key):
    sent = []
    client = Namespace(models=Namespace(generate_content=lambda **kw: sent.append(kw)))
    lines = []
    report = probe_gemini("luna-sync", client=client, printer=lines.append)
    assert report.refused and "openai" in report.refused
    assert probe_gemini("no-such-route", client=client, printer=lines.append).refused
    assert probe_gemini("flash-batch", modes=("turbo",), client=client, printer=lines.append).refused
    over = probe_gemini("flash-batch", cap_usd=0.001, client=client, printer=lines.append)
    assert over.refused and "cap" in over.refused and over.estimate_usd > 0.001
    monkeypatch.delenv("GEMINI_API_KEY")
    nokey = probe_gemini("flash-batch", client=client, printer=lines.append)
    assert nokey.refused and "GEMINI_API_KEY" in nokey.refused
    assert sent == []


def test_the_probe_refuses_a_model_the_price_table_does_not_know(with_key):
    prices = PriceTable.from_dict({"date": "2026-11-07", "batch_discount": 0.5, "models": {"other": {"input": 1.0, "output": 2.0}}})
    report = probe_gemini("flash-batch", prices=prices, client=Namespace(), printer=lambda line: None)
    assert report.refused and MODEL in report.refused


def test_the_command_line_returns_the_outcome_as_an_exit_code(with_key, monkeypatch):
    import rps.geminiprobe as module

    strict = Strict()
    real = module.probe_gemini
    monkeypatch.setattr(
        module, "probe_gemini", lambda *a, **k: real(*a, client=Namespace(models=Namespace(generate_content=strict)), printer=lambda line: None, **k)
    )
    assert main(["--route", "flash-batch"]) == 0
    assert main(["--route", "luna-sync"]) == 2
    strict.refuse_plain = True
    assert main(["--route", "flash-batch", "--max-output-tokens", "4096"]) == 0  # the last form sends no schema
