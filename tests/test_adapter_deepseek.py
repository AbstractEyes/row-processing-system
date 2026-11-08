"""The DeepSeek writer: request building, the off-peak gate, result parsing, usage and the one-call flow.

The fixtures are the SDK's own chat completion type, built from recorded-response-shaped data.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime, timezone

import pytest
from openai.types.chat import ChatCompletion

from rps.config import ConfigError
from rps.ledger import Usage
from rps.providers import DeepSeekWriter
from rps.providers.deepseek_api import parse_completion, peak_ends, tier_at, usage_of
from rps.routing import ImageRoutingError

from adapter_helpers import ANSWER, ApiError, Namespace, Recorder, call, route, rules

MODEL = "deepseek-flash"
PICTURE = b"\xff\xd8\xff\xe0 pretend jpeg bytes"


def utc(day, hour, minute=0):
    """November 2026: the 9th is a Monday, the 13th a Friday, the 14th a Saturday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def make_route(**overrides):
    fields = {"enabled": True}
    fields.update(overrides)
    return route("deepseek", MODEL, "api", effort=None, **fields)


def make_call(line="001", **overrides):
    return call(line, provider="deepseek", model=MODEL, kind="api", effort=None, **overrides)


def completion(text=ANSWER, finish="stop", usage=None):
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1_800_000_000,
            "model": MODEL,
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}}],
            "usage": usage
            or {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_cache_hit_tokens": 30,
                "prompt_cache_miss_tokens": 70,
            },
        }
    )


# ---------------------------------------------------------------------------- hours


@pytest.mark.parametrize(
    "when, peak",
    [
        (utc(9, 0, 59), False),
        (utc(9, 1, 0), True),
        (utc(9, 3, 59), True),
        (utc(9, 4, 0), False),
        (utc(9, 5, 59), False),
        (utc(9, 6, 0), True),
        (utc(9, 9, 59), True),
        (utc(9, 10, 0), False),
        (utc(9, 23, 0), False),
        (utc(13, 8, 0), True),  # Friday
        (utc(14, 2, 0), False),  # Saturday
        (utc(15, 8, 0), False),  # Sunday
    ],
)
def test_peak_hours_are_monday_to_friday_01_to_04_and_06_to_10_utc(when, peak):
    assert (peak_ends(when) is not None) is peak
    assert tier_at(when) == ("peak" if peak else "offpeak")


def test_the_end_of_the_current_window_is_reported():
    assert peak_ends(utc(9, 2, 30)) == utc(9, 4, 0)
    assert peak_ends(utc(9, 7, 0)) == utc(9, 10, 0)


# ---------------------------------------------------------------------------- the route's gate


def test_the_route_is_off_unless_the_configuration_turns_it_on(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    off = DeepSeekWriter("deepseek-sync", route("deepseek", MODEL, "api"), now=lambda: utc(9, 12))
    assert not off.accepting().ok and "switched off" in off.accepting().reason
    on = DeepSeekWriter("deepseek-sync", make_route(), now=lambda: utc(9, 12))
    assert on.accepting().ok


def test_without_its_key_the_route_stays_closed(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    writer = DeepSeekWriter("deepseek-sync", make_route(), now=lambda: utc(9, 12))
    acceptance = writer.accepting()
    assert not acceptance.ok and "DEEPSEEK_API_KEY" in acceptance.reason and acceptance.later is False


def test_during_peak_hours_the_route_is_closed_for_now_and_calls_wait_at_the_gate(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    writer = DeepSeekWriter("deepseek-sync", make_route(), now=lambda: utc(9, 7, 30))
    acceptance = writer.accepting()
    assert not acceptance.ok and acceptance.later is True and "10:00 UTC" in acceptance.reason
    assert writer.gate() == 2.5 * 3600
    off_peak = DeepSeekWriter("deepseek-sync", make_route(), now=lambda: utc(9, 12))
    assert off_peak.gate() is None


def test_the_gate_can_be_turned_off_for_a_small_test_at_any_hour(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    writer = DeepSeekWriter("deepseek-sync", make_route(options={"offpeak_only": False}), now=lambda: utc(9, 7, 30))
    assert writer.accepting().ok and writer.gate() is None


# ---------------------------------------------------------------------------- request building


def test_a_request_is_a_json_mode_chat_completion_with_the_rules_as_the_system_message():
    writer = DeepSeekWriter("deepseek-sync", make_route())
    [request] = writer.prepare([make_call()])
    arguments = request.payload["arguments"]
    assert set(arguments) == {"model", "messages", "response_format", "max_tokens"}
    assert arguments["model"] == MODEL and arguments["max_tokens"] == 2000
    assert arguments["response_format"] == {"type": "json_object"}
    assert arguments["messages"] == [
        {"role": "system", "content": rules().system},
        {"role": "user", "content": "Write the row for 001."},
    ]
    for forbidden in ("temperature", "top_p", "tools", "tool_choice", "reasoning_effort"):
        assert forbidden not in arguments


FORMAT = 'Answer with a json object. Give depth as a word (shallow, medium or deep), never as a number.'


def test_a_rule_sets_format_block_follows_the_rules_in_the_system_message():
    writer = DeepSeekWriter("deepseek-sync", make_route())
    [plain] = writer.prepare([make_call()])
    [shaped] = writer.prepare([make_call(rules=rules(json_format=FORMAT))])
    system, user = shaped.payload["arguments"]["messages"]
    assert system == {"role": "system", "content": rules().system.rstrip() + "\n\n" + FORMAT}
    assert user == {"role": "user", "content": "Write the row for 001."}  # the task text is not touched
    assert shaped.est_input_tokens > plain.est_input_tokens  # the block is part of what the call is billed for
    others = {key: value for key, value in shaped.payload["arguments"].items() if key != "messages"}
    assert others == {key: value for key, value in plain.payload["arguments"].items() if key != "messages"}


def test_a_rule_set_without_a_format_block_is_sent_as_it_is():
    for found in (None, "", "   \n", "<!-- to be written -->", "<!-- a note\nover two lines -->\n"):
        [request] = DeepSeekWriter("deepseek-sync", make_route()).prepare([make_call(rules=rules(json_format=found))])
        assert request.payload["arguments"]["messages"][0] == {"role": "system", "content": rules().system}


def test_notes_in_html_comments_are_not_sent_with_the_format_block():
    block = "<!-- placeholder: the text is written elsewhere -->\nGive depth as a word.\n<!-- end -->"
    [request] = DeepSeekWriter("deepseek-sync", make_route()).prepare([make_call(rules=rules(json_format=block))])
    assert request.payload["arguments"]["messages"][0]["content"] == rules().system.rstrip() + "\n\nGive depth as a word."


def test_the_word_json_may_come_from_the_format_block():
    writer = DeepSeekWriter("deepseek-sync", make_route())
    plain = "Write the rows as plain text."
    writer.check_rules(rules(system=plain, json_format=FORMAT))
    with pytest.raises(ConfigError, match="format block"):
        writer.check_rules(rules(system=plain, json_format="Give depth as a word."))


@pytest.mark.parametrize("effort", ["none", "low", "high", "max", "medium"])
def test_a_route_with_an_effort_sends_it_as_reasoning_effort_and_the_thinking_settings_are_the_providers_otherwise(effort):
    writer = DeepSeekWriter("deepseek-sync", route("deepseek", MODEL, "api", effort=effort, enabled=True))
    [request] = writer.prepare([call("001", provider="deepseek", model=MODEL, kind="api", effort=effort)])
    arguments = request.payload["arguments"]
    assert arguments["reasoning_effort"] == effort
    assert set(arguments) == {"model", "messages", "response_format", "max_tokens", "reasoning_effort"}
    [plain] = DeepSeekWriter("deepseek-sync", make_route()).prepare([make_call()])
    assert "reasoning_effort" not in plain.payload["arguments"]  # a route with no effort leaves the provider's default alone


def test_an_effort_the_provider_does_not_take_is_refused_before_a_run():
    with pytest.raises(ConfigError, match="effort"):
        DeepSeekWriter("deepseek-sync", route("deepseek", MODEL, "api", effort="turbo", enabled=True))


def test_a_picture_goes_in_the_user_message_with_the_configured_detail_when_its_class_is_allowed():
    for image_class in ("public_common", "coco", "ffhq"):
        writer = DeepSeekWriter("deepseek-sync", make_route(options={"detail": "high"}))
        [request] = writer.prepare([make_call(image=PICTURE, image_ext="jpg", image_class=image_class)])
        system, user = request.payload["arguments"]["messages"]
        assert system["role"] == "system" and isinstance(system["content"], str)  # system images return 400
        expected = "data:image/jpeg;base64," + base64.b64encode(PICTURE).decode()
        assert user["content"] == [
            {"type": "text", "text": "Write the row for 001."},
            {"type": "image_url", "image_url": {"url": expected, "detail": "high"}},
        ]
    default = DeepSeekWriter("deepseek-sync", make_route()).prepare([make_call(image=PICTURE, image_ext="jpg", image_class="coco")])[0]
    assert default.payload["arguments"]["messages"][1]["content"][1]["image_url"]["detail"] == "low"


@pytest.mark.parametrize("image_class", ["deepfashion", "hico", "synthetic", "mystery", None])
def test_a_picture_of_a_class_the_table_does_not_allow_never_reaches_the_request(image_class):
    writer = DeepSeekWriter("deepseek-sync", make_route())
    with pytest.raises(ImageRoutingError):
        writer.prepare([make_call(image=PICTURE, image_ext="jpg", image_class=image_class)])


def test_the_rules_must_mention_json_for_json_mode():
    writer = DeepSeekWriter("deepseek-sync", make_route())
    writer.check_rules(rules())
    with pytest.raises(ConfigError, match="json"):
        writer.check_rules(rules(system="Write the rows as plain text."))


def test_the_pools_system_prompt_mentions_json():
    from rps import rules as pool

    DeepSeekWriter("deepseek-sync", make_route()).check_rules(rules(system=pool.writer_system()))


def test_the_route_is_checked_before_a_run():
    with pytest.raises(ConfigError, match="unknown option"):
        DeepSeekWriter("r", make_route(options={"detial": "low"}))
    with pytest.raises(ConfigError, match="detail"):
        DeepSeekWriter("r", make_route(options={"detail": "ultra"}))


# ---------------------------------------------------------------------------- results and usage


def test_usage_uses_the_cache_hit_and_miss_counts_and_splits_off_reasoning():
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 400,
        "total_tokens": 1400,
        "prompt_cache_hit_tokens": 600,
        "prompt_cache_miss_tokens": 400,
        "completion_tokens_details": {"reasoning_tokens": 150},
    }
    assert usage_of(completion(usage=usage).usage) == Usage(input_tokens=400, cache_read_tokens=600, output_tokens=250, reasoning_tokens=150)
    plain = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}
    assert usage_of(completion(usage=plain).usage) == Usage(input_tokens=80, output_tokens=20)


def test_a_normal_finish_gives_the_text_and_the_tier_it_was_billed_at():
    result = parse_completion("c1", completion(), tier="offpeak")
    assert result.output == ANSWER and result.error is None and result.tier == "offpeak"
    assert result.usage == Usage(input_tokens=70, cache_read_tokens=30, output_tokens=50)


def test_a_completion_cut_off_at_the_limit_is_a_retryable_truncation():
    result = parse_completion("c1", completion(text='{"sk', finish="length"), max_tokens=2000, tier="peak")
    assert result.kind == "truncated" and result.retryable is True and "2000" in result.error and result.tier == "peak"


def test_a_content_filter_stop_is_a_block_that_is_not_retried():
    result = parse_completion("c1", completion(text="", finish="content_filter"))
    assert result.kind == "blocked" and result.retryable is False


def test_blank_content_with_a_normal_finish_is_a_failed_parse_that_is_retried():
    for blank in ("", "  \n"):
        result = parse_completion("c1", completion(text=blank))
        assert result.kind == "invalid_output" and result.retryable is True


def test_a_busy_service_is_retried_and_any_other_finish_is_not():
    # DeepSeek's own finish reason is not one of the SDK's literals, so the response is a plain dict here.
    busy = parse_completion("c1", {"choices": [{"finish_reason": "insufficient_system_resource", "message": {"content": ""}}]})
    assert busy.kind == "provider_error" and busy.retryable is True
    odd = parse_completion("c1", completion(finish="tool_calls"))
    assert odd.kind == "unexpected" and odd.retryable is False


# ---------------------------------------------------------------------------- sending


def fake_client(result=None):
    return Namespace(chat=Namespace(completions=Namespace(create=Recorder(result if result is not None else completion()))))


def test_a_call_is_sent_with_the_prepared_arguments_and_marked_with_the_tier_of_its_time(monkeypatch):
    client = fake_client()
    clock = [utc(9, 12)]
    writer = DeepSeekWriter("deepseek-sync", make_route(), client=client, now=lambda: clock[0])
    [request] = writer.prepare([make_call()])
    off = writer.send(request)
    clock[0] = utc(9, 7)
    peak = writer.send(request)
    assert client.chat.completions.create.calls[0]["model"] == MODEL
    assert (off.tier, peak.tier) == ("offpeak", "peak")
    assert off.output == ANSWER


def test_an_error_becomes_a_failed_result_of_the_right_kind_and_still_carries_the_tier():
    for status, kind, retryable in [(402, "auth", False), (401, "auth", False), (429, "provider_error", True), (500, "provider_error", True), (400, "bad_request", False)]:
        writer = DeepSeekWriter("deepseek-sync", make_route(), client=fake_client(ApiError(status)), now=lambda: utc(9, 12))
        result = writer.send(writer.prepare([make_call()])[0])
        assert (result.kind, result.retryable, result.output, result.tier) == (kind, retryable, None, "offpeak")


def test_calls_run_through_the_pool_and_wait_at_the_gate_until_it_opens(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    clock = [utc(9, 7)]  # peak
    client = fake_client()
    writer = DeepSeekWriter("deepseek-sync", make_route(workers=2), client=client, now=lambda: clock[0])
    writer.gate_poll_seconds = 0.01  # a worker at the gate looks again every 10 ms in this test
    handle = writer.submit(writer.prepare([make_call("001"), make_call("002")]))
    assert writer.poll(handle).state == "in_progress"
    time.sleep(0.1)
    assert client.chat.completions.create.calls == []  # nothing goes out during peak hours
    clock[0] = utc(9, 11)
    deadline = time.monotonic() + 10
    while writer.poll(handle).state != "ended":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert len(client.chat.completions.create.calls) == 2
    writer.close()


def test_the_client_is_built_for_deepseeks_address_from_the_environment_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key")
    writer = DeepSeekWriter("deepseek-sync", make_route(max_retries=3, timeout_seconds=90))
    client = writer._client()
    assert str(client.base_url).startswith("https://api.deepseek.com") and client.max_retries == 3
