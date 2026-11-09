"""The API review seat: configuration, the prompt, the requests, the results, the verdict files, the budget and recovery.

Everything runs against a local store and a fake batch client: no network, no key.
"""

from __future__ import annotations

import json
import re
import typing
from pathlib import Path

import pytest
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from pydantic import TypeAdapter

from rps import ids, layout, rules
from rps.config import ConfigError
from rps.ledger import Usage
from rps.providers.schemas import ANTHROPIC, check_schema
from rps.review import api_seat
from rps.review.api_seat import (
    ASK_LINE,
    DEFAULT_PRICES,
    SeatConfig,
    answer_schema,
    cost_of,
    judging_text,
    package_brief,
    reason_codes,
    request_custom_id,
    system_text,
)
from rps.review.client import ReviewerClient
from rps.review.packets import VERDICT_VALUES
from rps.store import LocalStore

from seat_helpers import (
    MODEL,
    REVIEWER,
    FakeAnthropic,
    asked_row,
    make_seat,
    publish,
    run_until_quiet,
    seat_config,
    verdict_file,
    verdict_text,
)


@pytest.fixture
def remote(tmp_path):
    return LocalStore(tmp_path / "remote", fsync=False)


@pytest.fixture
def api():
    return FakeAnthropic()


# ---------------------------------------------------------------------------- configuration


def test_a_minimal_configuration_gets_the_documented_defaults():
    config = SeatConfig.from_dict({"reviewer_id": "claude-haiku", "model": "claude-haiku-5-5", "budget_usd": 5})
    assert (config.effort, config.max_tokens, config.packets_per_batch) == ("high", 16000, 50)
    assert (config.poll_seconds, config.max_attempts, config.extra_instructions) == (60.0, 3, None)
    assert config.budget_usd == 5.0 and isinstance(config.budget_usd, float)
    assert {model: (price.input, price.output) for model, price in config.prices.items()} == {
        "claude-haiku-5-5": (0.10, 0.50),
        "claude-sonnet-5-5": (2.00, 10.00),
    }
    assert SeatConfig.from_dict(config.to_dict()) == config  # what it writes it reads back


def test_unknown_keys_are_refused():
    with pytest.raises(ConfigError, match=r"unknown key\(s\) \['temperature'\]"):
        SeatConfig.from_dict(seat_config(temperature=0))
    with pytest.raises(ConfigError, match="expected an object"):
        SeatConfig.from_dict(["not", "an", "object"])
    with pytest.raises(ConfigError, match="unknown key"):
        SeatConfig.from_dict(seat_config(prices={MODEL: {"input": 1, "output": 2, "cache": 3}}))


def test_a_model_with_no_prices_is_refused_until_its_prices_are_given():
    with pytest.raises(ConfigError, match="no prices for 'claude-opus-9'"):
        SeatConfig.from_dict(seat_config(model="claude-opus-9"))
    config = SeatConfig.from_dict(seat_config(model="claude-opus-9", prices={"claude-opus-9": {"input": 5, "output": 25}}))
    assert config.prices["claude-opus-9"].output == 25.0 and config.prices["claude-haiku-5-5"].input == 0.10  # the defaults stay
    with pytest.raises(ConfigError, match="output"):
        SeatConfig.from_dict(seat_config(prices={MODEL: {"input": 1}}))
    with pytest.raises(ConfigError, match="at least 0"):
        SeatConfig.from_dict(seat_config(prices={MODEL: {"input": -1, "output": 2}}))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"effort": "turbo"}, "effort: must be one of"),
        ({"budget_usd": -1}, "budget_usd: must be at least 0"),
        ({"max_tokens": 0}, "max_tokens: must be at least 1"),
        ({"packets_per_batch": 0}, "packets_per_batch: must be at least 1"),
        ({"max_attempts": 0}, "max_attempts: must be at least 1"),
        ({"max_attempts": 11}, "max_attempts: must be at most 10"),
        ({"poll_seconds": -1}, "poll_seconds: must be at least 0"),
        ({"max_tokens": 1.5}, "whole number"),
        ({"reviewer_id": "two words"}, "reviewer_id"),
        ({"reviewer_id": None}, "reviewer_id"),
        ({"model": ""}, "model: expected a non-empty string"),
        ({"extra_instructions": "  "}, "extra_instructions"),
    ],
)
def test_bad_values_are_refused(overrides, message):
    with pytest.raises(ConfigError, match=message):
        SeatConfig.from_dict(seat_config(**overrides))


def test_a_budget_is_required_and_a_value_that_looks_like_a_key_is_refused():
    data = seat_config()
    del data["budget_usd"]
    with pytest.raises(ConfigError, match="budget_usd: required"):
        SeatConfig.from_dict(data)
    with pytest.raises(ConfigError, match="looks like an API key"):
        SeatConfig.from_dict(seat_config(extra_instructions="use sk-ant-" + "q" * 30))


def test_a_batch_bills_half_the_list_price_and_the_cache_prices_at_list():
    haiku = SeatConfig.from_dict(seat_config()).prices["claude-haiku-5-5"].batch_rates()
    assert (haiku.input, haiku.output, haiku.cache_write, haiku.cache_read) == pytest.approx((0.05, 0.25, 0.20, 0.01))
    sonnet = SeatConfig.from_dict(seat_config(model="claude-sonnet-5-5")).prices["claude-sonnet-5-5"].batch_rates()
    assert (sonnet.input, sonnet.output, sonnet.cache_write, sonnet.cache_read) == pytest.approx((1.0, 5.0, 4.0, 0.2))
    assert DEFAULT_PRICES["claude-haiku-5-5"] == (0.10, 0.50)


def test_cost_is_the_usage_times_the_rates_with_reasoning_billed_as_output():
    rates = SeatConfig.from_dict(seat_config()).prices[MODEL].batch_rates()
    usage = Usage(input_tokens=1_000, cache_write_tokens=10_000, cache_read_tokens=100_000, output_tokens=2_000, reasoning_tokens=3_000)
    expected = (1_000 * 0.05 + 10_000 * 0.20 + 100_000 * 0.01 + 5_000 * 0.25) / 1_000_000
    assert cost_of(usage, rates) == pytest.approx(expected) == pytest.approx(0.00430)


# ---------------------------------------------------------------------------- the brief and the prompt


def test_the_reason_codes_are_the_ones_in_the_what_to_read_for_table_and_not_the_verdicts():
    codes = reason_codes(rules.review_brief())
    assert {"gloss", "invented", "hint_copy", "should_skip", "person", "minor", "other"} <= set(codes)
    assert not set(codes) & {"keep", "fix", "reject", "code"} and len(codes) == len(set(codes))
    table = "# Brief\n\n## What to read for\n\n| Code | Look for |\n|---|---|\n| alpha | a |\n| beta_2 | b |\n\n## Verdicts\n\n| keep | fine |\n"
    assert reason_codes(table) == ("alpha", "beta_2")
    assert reason_codes("# a brief with no table\n\nkeep or fix\n") == ()


def test_the_package_brief_is_the_rule_pools_with_its_version():
    brief = package_brief()
    version_file = Path(rules.__file__).with_name("VERSION")
    assert brief.version == rules.rules_version() == version_file.read_text(encoding="utf-8").strip()
    assert brief.text == rules.review_brief()


def test_the_system_text_is_the_role_the_judging_rules_the_brief_and_the_extra_instructions_in_that_order():
    brief = package_brief()
    text = system_text(brief, "Be brief in the notes.")
    judging, extra = judging_text().strip(), "Be brief in the notes."
    assert text.index(api_seat.ROLE) < text.index(judging) < text.index(brief.text.strip()) < text.index(extra)
    assert text.startswith(api_seat.ROLE) and text.rstrip().endswith(extra)
    assert extra not in system_text(brief) and system_text(brief, "   ") == system_text(brief)


def test_the_judging_rules_are_plain_and_hold_nothing_of_the_place_they_came_from():
    text = judging_text()
    for word in ("keep", "fix", "reject", "reasons", "notes"):
        assert word in text
    lowered = text.lower()
    # paths, shell commands, model names and a version line
    for leak in ("\\", "powershell", "python -m", "claude", "haiku", "sonnet", "opus", ":/", rules.rules_version()):
        assert leak not in lowered, leak


def test_the_answer_format_is_in_the_dialect_anthropic_takes_and_binds_reasons_to_the_briefs_codes():
    codes = reason_codes(rules.review_brief())
    schema = answer_schema(codes)
    check_schema(schema, ANTHROPIC)
    assert schema["additionalProperties"] is False and schema["required"] == ["verdict", "reasons", "notes"]
    assert schema["properties"]["verdict"]["enum"] == list(VERDICT_VALUES)
    assert schema["properties"]["reasons"]["items"] == {"type": "string", "enum": list(codes)}
    fallback = answer_schema(())
    check_schema(fallback, ANTHROPIC)
    assert fallback["properties"]["reasons"]["items"] == {"type": "string", "pattern": "^[a-z0-9_]{1,40}$"}


def test_a_requests_custom_id_is_within_the_providers_limits_and_the_same_for_the_same_row_and_try():
    packet_id = "p0123456789ab"
    made = [request_custom_id(packet_id, place, attempt) for place in (1, 2, 20, 1000) for attempt in (1, 2, 3)]
    assert len(set(made)) == len(made)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item) for item in made)
    assert request_custom_id(packet_id, 3, 2) == request_custom_id(packet_id, 3, 2)
    assert request_custom_id("p000000000001", 3, 2) != request_custom_id(packet_id, 3, 2)
    assert ids.parse_call_id(ids.call_id_from_custom_id(made[0]))[2] == 1  # the repo's own helpers read it back


# ---------------------------------------------------------------------------- the requests


def sent_requests(remote, tmp_path, **overrides):
    """Publish a packet, let a seat send it, and return (packet, the requests sent, the seat)."""
    api = FakeAnthropic()
    packet = publish(remote, rows=3, **overrides)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    return packet, api.batches.sent, seat


def test_one_request_is_sent_for_each_row_and_each_goes_alone(remote, tmp_path):
    packet, requests, _seat = sent_requests(remote, tmp_path)
    assert len(requests) == 3
    assert [asked_row(request)["row_id"] for request in requests] == [item.row_id for item in packet.rows]
    for request in requests:
        assert [message["role"] for message in request["params"]["messages"]] == ["user"]
        text = request["params"]["messages"][0]["content"][0]["text"]
        mine = asked_row(request)["row_id"]
        others = [item.row_id for item in packet.rows if item.row_id != mine]
        assert mine in text and not any(other in text for other in others)  # the row it is about, and no other row's
    assert len({request["custom_id"] for request in requests}) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request["custom_id"]) for request in requests)


def test_a_request_holds_what_the_specification_names_and_nothing_it_forbids(remote, tmp_path):
    _packet, requests, seat = sent_requests(remote, tmp_path)
    params = requests[0]["params"]
    assert set(params) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert set(params) <= set(typing.get_type_hints(MessageCreateParamsNonStreaming))  # real SDK parameter names
    assert params["model"] == MODEL and params["max_tokens"] == 16000
    for forbidden in ("temperature", "top_p", "top_k", "tool_choice", "tools", "stop_sequences", "thinking"):
        assert forbidden not in params
    assert params["messages"][0]["role"] == "user"  # nothing is pre-filled
    assert params["output_config"] == {"format": {"type": "json_schema", "schema": seat._schema}, "effort": "high"}
    check_schema(params["output_config"]["format"]["schema"], ANTHROPIC)
    for request in requests:
        TypeAdapter(Request).validate_python(request)  # the SDK's own type for a batch request reads it


def test_the_system_block_carries_the_prompt_with_a_cache_mark_and_is_the_same_for_every_request(remote, tmp_path):
    _packet, requests, _seat = sent_requests(remote, tmp_path)
    systems = [request["params"]["system"] for request in requests]
    assert systems[0] == systems[1] == systems[2]
    (block,) = systems[0]
    assert block["type"] == "text" and block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert block["text"] == system_text(package_brief())
    assert len(block["text"]) > 2000  # far above the shortest prompt that is cached


def test_the_user_message_is_the_blind_view_and_the_task_as_packet_json_holds_them(remote, tmp_path):
    packet, requests, _seat = sent_requests(remote, tmp_path)
    document = json.loads(remote.read_text(layout.packet_json_path(REVIEWER, packet.packet_id)))
    for request, entry in zip(requests, document["rows"]):
        assert asked_row(request) == {"row_id": entry["row_id"], "row": entry["row"], "task": entry["task"]}
        assert request["params"]["messages"][0]["content"][0]["text"].endswith(ASK_LINE)
    assert asked_row(requests[0])["task"] == {"line_id": "001", "item_id": "img-001", "spec": "a cat"}


def test_a_request_never_names_the_writer_or_its_model(remote, tmp_path):
    _packet, requests, _seat = sent_requests(remote, tmp_path)
    for request in requests:
        wire = json.dumps(request)
        assert "fake-model" not in wire  # neither the label's model nor the row's own
        row = asked_row(request)["row"]
        assert "writer" not in row and "writer" not in json.dumps(request["params"]["messages"])  # the label is not in the view
        assert row["source"]["model"] == ""  # the row's own model name is blanked in the view


def test_a_request_is_text_only_even_when_the_packet_has_pictures(remote, tmp_path):
    packet, requests, _seat = sent_requests(remote, tmp_path, pictures=True)
    assert packet.rows[0].image_name  # the packet does carry pictures
    for request in requests:
        (message,) = request["params"]["messages"]
        assert [block["type"] for block in message["content"]] == ["text"]
        assert "image" not in json.dumps(request["params"]["messages"]).replace('"image": null', "")


def test_the_effort_the_model_the_token_limit_and_the_extra_instructions_come_from_the_configuration(remote, tmp_path):
    api = FakeAnthropic()
    publish(remote, rows=2)
    seat, _ = make_seat(
        remote, tmp_path, api, model="claude-sonnet-5-5", effort="max", max_tokens=9000, extra_instructions="Always cite the thought number."
    )
    seat.step()
    params = api.batches.sent[0]["params"]
    assert (params["model"], params["max_tokens"], params["output_config"]["effort"]) == ("claude-sonnet-5-5", 9000, "max")
    assert params["system"][0]["text"].rstrip().endswith("Always cite the thought number.")


# ---------------------------------------------------------------------------- the flow


def test_a_packet_is_answered_through_a_batch_and_the_verdict_file_is_the_clients(remote, tmp_path):
    def answer(request):
        if asked_row(request)["row_id"].endswith("_1"):
            return verdict_text("fix", ["wording"], "the caption is two sentences")
        return verdict_text("keep")

    api = FakeAnthropic(answer=answer)
    packet = publish(remote, rows=3)
    seat, printed = make_seat(remote, tmp_path, api)
    assert seat.step().sent == 1
    assert verdict_file(remote, packet) is None  # nothing is written before the answers are in
    api.batches.end()
    result = seat.step()
    assert (result.settled, result.answered) == (1, 1)
    lines = verdict_file(remote, packet)
    assert [line["row_id"] for line in lines] == [item.row_id for item in packet.rows]  # in the packet's order
    assert [line["verdict"] for line in lines] == ["keep", "fix", "keep"]
    assert lines[1] == {"row_id": packet.rows[1].row_id, "verdict": "fix", "reasons": ["wording"], "notes": "the caption is two sentences"}
    assert set(lines[0]) == {"row_id", "verdict", "reasons", "notes"}
    assert any("sent batch msgbatch_0001: 1 packets, 3 rows" in line for line in printed)
    assert any("answered 1 packets (3 rows: 2 keep, 1 fix, 0 reject)" in line for line in printed)
    assert seat.step().sent == 0  # and the next pass has nothing to do


def test_the_verdict_file_is_identical_in_form_to_one_a_desk_seat_makes_by_hand(remote, tmp_path):
    api = FakeAnthropic(answer=lambda request: verdict_text("reject", ["person", "invented"], "a person stands in the doorway, thought 2"))
    packet = publish(remote, rows=2)
    desk_packet = publish(remote, reviewer="desk-seat", rows=2)  # the same rows, addressed to a desk seat
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    desk = ReviewerClient("desk-seat", remote, tmp_path / "desk", fsync=False)
    desk.download(desk_packet.packet_id)
    for item in desk_packet.rows:
        desk.record_verdict(desk_packet.packet_id, item.row_id, "reject", ["person", "invented"], "a person stands in the doorway, thought 2")
    assert desk.push(desk_packet.packet_id).pushed
    assert [row.row_id for row in packet.rows] == [row.row_id for row in desk_packet.rows]
    seat_text = remote.read_text(layout.verdicts_path(REVIEWER, packet.packet_id))
    desk_text = remote.read_text(layout.verdicts_path("desk-seat", desk_packet.packet_id))
    assert seat_text == desk_text and seat_text.endswith("\n")


def test_the_clients_burn_applies_the_local_pictures_are_gone_after_the_push(remote, tmp_path):
    api = FakeAnthropic()
    packet = publish(remote, rows=2, pictures=True)
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    assert verdict_file(remote, packet) is not None
    assert not list((tmp_path / "seat-work").rglob("*.jpg"))  # nothing of the pictures is left on this machine
    assert remote.list(f"{layout.packet_dir(REVIEWER, packet.packet_id)}/images/")  # the controller's copies are the controller's to burn


def test_the_seat_writes_only_where_a_reviewer_may(remote, tmp_path):
    api = FakeAnthropic()
    publish(remote, rows=2)
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    written = [path for path in remote.list("") if not path.startswith("packets/")]
    assert written and all(path.startswith(f"review/{REVIEWER}/") for path in written)
    assert not [path for path in written if layout.parse_events_path(path)]
    seat_files = [path for path in written if f"/{api_seat.SEAT_DIR}/" in path]
    assert seat_files == [f"review/{REVIEWER}/seat/ledger.jsonl"]  # the partial verdicts are gone once the packet is answered
