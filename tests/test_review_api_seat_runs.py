"""The API review seat at work: finding packets, reading results, retries, the budget, crashes, status and the command line.

Everything runs against a local store (or a buffered bucket store) and a fake batch client: no network, no key.
"""

from __future__ import annotations

import io
import json
import threading
import time

import pytest

from rps import layout, rules
from rps.config import ConfigError
from rps.layout import controller_actor
from rps.providers.common import estimate_text_tokens
from rps.review import api_seat
from rps.review.api_seat import LONG_PROMPT_TOKENS, RECOVER_WAIT, SeatHalted, user_text
from rps.review.client import ReviewerClient
from rps.store import ActorStore, LocalStore
from rps.store.bucket import BucketStore

from adapter_helpers import ApiError
from fakes import Clock as StoreClock, FakeBucketApi
from harness import World, task_spec
from helpers import make_config
from review_helpers import by_line
from seat_helpers import (
    MODEL,
    PER_REQUEST,
    REVIEWER,
    FakeAnthropic,
    asked_row,
    attempt_of,
    canceled,
    errored,
    expired,
    ledger_records,
    make_seat,
    packet_estimate,
    publish,
    row_of,
    run_until_quiet,
    seat_config,
    succeeded,
    verdict_file,
    verdict_text,
)


@pytest.fixture
def remote(tmp_path):
    return LocalStore(tmp_path / "remote", fsync=False)


@pytest.fixture
def api():
    return FakeAnthropic()


# ---------------------------------------------------------------------------- finding the packets


def test_only_packets_addressed_to_the_reviewer_with_no_verdict_file_are_sent(remote, tmp_path, api):
    mine = publish(remote, rows=2, line="001")
    publish(remote, reviewer="another-seat", rows=2, line="002")
    done = publish(remote, rows=2, line="003")
    desk = ReviewerClient(REVIEWER, remote, tmp_path / "desk", fsync=False)
    desk.download(done.packet_id)
    for item in done.rows:
        desk.record_verdict(done.packet_id, item.row_id, "keep")
    assert desk.push(done.packet_id).pushed  # a verdict file for this packet already exists
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    assert {row_of(request) for request in api.batches.sent} == {item.row_id for item in mine.rows}


def test_a_packet_that_is_out_is_not_sent_again(remote, tmp_path, api):
    publish(remote, rows=2)
    seat, _ = make_seat(remote, tmp_path, api)
    assert seat.step().sent == 1
    assert seat.step().sent == 0 and len(api.batches.batches) == 1
    assert seat.status()["in_flight"] == {"batches": 1, "requests": 2, "packets": 1}


def test_a_packet_for_another_brief_version_is_skipped_said_once_and_never_answered(remote, tmp_path, api):
    old = publish(remote, rows=2, line="001", brief_version="old-brief")
    good = publish(remote, rows=2, line="002")
    seat, printed = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    assert verdict_file(remote, old) is None and verdict_file(remote, good) is not None
    said = [line for line in printed if f"packet {old.packet_id} skipped" in line]
    assert len(said) == 1 and "old-brief" in said[0] and rules.rules_version() in said[0]
    assert {row_of(request) for request in api.batches.sent} == {item.row_id for item in good.rows}
    assert old.packet_id in seat.status()["skipped"]


def test_a_packet_that_cannot_be_read_or_is_not_for_this_reviewer_is_skipped(remote, tmp_path, api):
    remote.write_text(layout.packet_json_path(REVIEWER, "p00000000000a"), "{not json")
    tampered = publish(remote, rows=2, line="001")
    path = layout.packet_json_path(REVIEWER, tampered.packet_id)
    document = json.loads(remote.read_text(path))
    document["reviewer_id"] = "another-seat"
    remote.write_text(path, json.dumps(document))
    good = publish(remote, rows=2, line="002")
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    assert verdict_file(remote, good) is not None and verdict_file(remote, tampered) is None
    assert "malformed" in seat.status()["skipped"]["p00000000000a"]
    assert "not addressed to this reviewer" in seat.status()["skipped"][tampered.packet_id]


# ---------------------------------------------------------------------------- reading the results


def test_results_come_back_in_any_order_and_are_matched_by_custom_id_and_a_stray_result_is_ignored(remote, tmp_path, api):
    by_suffix = {"_0": ("keep", []), "_1": ("fix", ["wording"]), "_2": ("reject", ["person"])}

    def answer(request):
        verdict, reasons = by_suffix[row_of(request)[-2:]]
        return verdict_text(verdict, reasons, f"for {row_of(request)}")

    api.batches.answer = answer
    api.batches.extra_items = [succeeded("not-one-of-ours", verdict_text("reject", ["person"]))]
    first, second = publish(remote, rows=3, line="001"), publish(remote, rows=3, line="002")
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    for packet in (first, second):
        for line in verdict_file(remote, packet):
            verdict, reasons = by_suffix[line["row_id"][-2:]]
            assert (line["verdict"], line["reasons"], line["notes"]) == (verdict, reasons, f"for {line['row_id']}")


@pytest.mark.parametrize(
    "text, why",
    [
        ("not json at all", "not valid JSON"),
        ('["keep"]', "not a JSON object"),
        (json.dumps({"verdict": "keep", "reasons": [], "notes": "", "row_id": "row-1"}), "unexpected field"),
        (json.dumps({"verdict": "keep", "reasons": [], "notes": "", "score": 3}), "unexpected field"),
        (verdict_text("fix", [], "a note"), "needs at least one reason"),
        (verdict_text("reject", [], "a note"), "needs at least one reason"),
        (verdict_text("keep", [], "x" * 2001), "at most 2000"),
        (json.dumps({"verdict": "maybe", "reasons": [], "notes": ""}), "verdict must be one of"),
        (json.dumps({"verdict": "keep", "reasons": ["Not A Code"], "notes": ""}), "not a short code"),
        (json.dumps({"verdict": "fix", "reasons": ["a"] * 11, "notes": ""}), "at most 10 codes"),
        (json.dumps({"verdict": "keep", "reasons": None, "notes": ""}), "list of at most"),
        ("", "not valid JSON"),
    ],
)
def test_an_answer_that_fails_the_packages_checks_is_not_a_verdict(text, why):
    verdict, problem = api_seat.read_answer(text, "row-1")
    assert verdict is None and why in problem


def test_a_valid_answer_becomes_a_verdict_with_the_row_id_the_seat_adds():
    assert api_seat.read_answer(verdict_text("fix", ["wording", "padding"], "thought 2 repeats thought 1"), "row-1") == (
        {"row_id": "row-1", "verdict": "fix", "reasons": ["wording", "padding"], "notes": "thought 2 repeats thought 1"},
        "",
    )
    assert api_seat.read_answer(json.dumps({"verdict": "keep"}), "row-9")[0] == {"row_id": "row-9", "verdict": "keep", "reasons": [], "notes": ""}


@pytest.mark.parametrize("bad", ["this is not json", verdict_text("fix", [], "no reason given"), verdict_text("keep", [], "x" * 2001)])
def test_a_row_whose_answer_is_unusable_is_sent_again_in_a_later_batch_until_it_is_valid(remote, tmp_path, api, bad):
    api.batches.answer = lambda request: bad if row_of(request).endswith("_1") and attempt_of(request) < 3 else verdict_text("keep")
    packet = publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    batches = list(api.batches.batches.values())
    assert [len(batch["requests"]) for batch in batches] == [3, 1, 1]
    assert [attempt_of(request) for batch in batches for request in batch["requests"]] == [1, 1, 1, 2, 3]
    assert {row_of(request) for batch in batches[1:] for request in batch["requests"]} == {packet.rows[1].row_id}
    assert len(verdict_file(remote, packet)) == 3
    assert seat.status()["failures"] == {"invalid": 2}


def test_a_row_that_keeps_failing_leaves_its_packet_unanswered_and_no_verdict_is_made_up(remote, tmp_path, api):
    api.batches.answer = lambda request: "no json" if row_of(request).endswith("_1") else verdict_text("keep")
    packet = publish(remote, rows=3)
    seat, printed = make_seat(remote, tmp_path, api)  # three attempts by default
    run_until_quiet(seat, api)
    assert [len(batch["requests"]) for batch in api.batches.batches.values()] == [3, 1, 1]  # the rows that were fine are not sent again
    assert verdict_file(remote, packet) is None
    (left,) = seat.status()["left_unanswered"].items()
    assert left[0] == packet.packet_id and f"row {packet.rows[1].row_id} failed 3 attempts" in left[1] and "last: the answer is not valid JSON" in left[1]
    assert [line for line in printed if "left unanswered" in line] == [f"[seat {REVIEWER}] packet {packet.packet_id} left unanswered: {left[1]}"]
    assert len(ledger_records(remote, "gave_up")) == 1
    seat.step()
    seat.step()
    assert len(api.batches.batches) == 3 and len([line for line in printed if "left unanswered" in line]) == 1  # nothing more is sent or said


def test_a_packet_left_unanswered_is_taken_up_again_when_more_attempts_are_allowed(remote, tmp_path, api):
    api.batches.answer = lambda request: "no json" if row_of(request).endswith("_1") and attempt_of(request) < 4 else verdict_text("keep")
    packet = publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    assert verdict_file(remote, packet) is None
    again, _ = make_seat(remote, tmp_path, api, max_attempts=4)  # a new run with a larger allowance
    assert again.status()["left_unanswered"]  # it knows from the ledger
    run_until_quiet(again, api)
    assert len(verdict_file(remote, packet)) == 3
    assert [attempt_of(request) for request in list(api.batches.batches.values())[-1]["requests"]] == [4]
    assert again.status()["left_unanswered"] == {}


def test_a_refusal_is_a_failed_attempt_that_records_the_category_and_is_still_billed(remote, tmp_path, api):
    declined = {"type": "refusal", "category": "cyber", "explanation": None}
    api.batches.answer = lambda request: (
        succeeded(request["custom_id"], "", stop="refusal", stop_details=declined) if row_of(request).endswith("_0") else verdict_text("keep")
    )
    publish(remote, rows=3)
    seat, printed = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.end()
    seat.step()
    assert seat.status()["failures"] == {"refusal": 1}
    (failure,) = ledger_records(remote, "settled")[0]["failed"]
    assert failure["kind"] == "refusal" and "cyber" in failure["reason"] and failure["attempt"] == 1
    assert seat.status()["spend_usd"] == pytest.approx(3 * PER_REQUEST)  # the declined request was billed too
    assert any("1 refusal" in line for line in printed)


def test_an_answer_cut_off_at_the_token_limit_or_ended_any_other_way_is_a_failed_attempt(remote, tmp_path, api):
    def answer(request):
        if row_of(request).endswith("_0"):
            return succeeded(request["custom_id"], '{"verdict": "ke', stop="max_tokens")
        if row_of(request).endswith("_1"):
            return succeeded(request["custom_id"], verdict_text("keep"), stop="tool_use")
        return verdict_text("keep")

    api.batches.answer = answer
    publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.end()
    seat.step()
    assert seat.status()["failures"] == {"max_tokens": 1, "stopped": 1}
    reasons = {failure["kind"]: failure["reason"] for failure in ledger_records(remote, "settled")[0]["failed"]}
    assert "token limit" in reasons["max_tokens"] and "tool_use" in reasons["stopped"]


def test_a_result_that_did_not_succeed_is_a_failed_attempt_and_costs_nothing(remote, tmp_path, api):
    def answer(request):
        kind = row_of(request)[-1]
        return {"0": errored, "1": canceled, "2": expired}[kind](request["custom_id"])

    api.batches.answer = answer
    packet = publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.end()
    seat.step()
    assert seat.status()["failures"] == {"errored": 1, "canceled": 1, "expired": 1}
    assert seat.status()["spend_usd"] == 0.0
    assert verdict_file(remote, packet) is None
    assert len(api.batches.batches) == 2 and attempt_of(list(api.batches.batches.values())[1]["requests"][0]) == 2  # all three go again


def test_a_partial_batch_keeps_the_valid_verdicts_and_only_the_failed_row_goes_again(remote, tmp_path, api):
    api.batches.answer = lambda request: errored(request["custom_id"]) if row_of(request).endswith("_1") and attempt_of(request) == 1 else verdict_text("keep")
    packet = publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.end()
    seat.step()
    kept = remote.read_text(f"review/{REVIEWER}/seat/judged/{packet.packet_id}.jsonl")
    assert [json.loads(line)["row_id"] for line in kept.splitlines()] == [packet.rows[0].row_id, packet.rows[2].row_id]
    assert verdict_file(remote, packet) is None  # a packet is answered only when every row has a verdict
    second = list(api.batches.batches.values())[1]["requests"]
    assert [row_of(request) for request in second] == [packet.rows[1].row_id] and attempt_of(second[0]) == 2
    api.batches.end()
    assert seat.step().answered == 1
    assert [line["verdict"] for line in verdict_file(remote, packet)] == ["keep"] * 3
    assert not remote.exists(f"review/{REVIEWER}/seat/judged/{packet.packet_id}.jsonl")  # the scratch file goes with the answer


# ---------------------------------------------------------------------------- the budget


def test_a_batchs_estimate_is_the_input_at_the_dearest_rate_plus_the_token_limit_of_output_for_every_request(remote, tmp_path, api):
    publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    (planned,) = ledger_records(remote, "planned")
    document = json.loads(remote.read_text(layout.packet_json_path(REVIEWER, planned["requests"][0]["packet_id"])))
    expected = sum(
        ((seat._base_tokens + estimate_text_tokens(user_text(entry))) * 0.20 + 16000 * 0.25) / 1_000_000 for entry in document["rows"]
    )
    assert planned["estimate_usd"] == pytest.approx(expected, rel=1e-6)  # 0.20 is the one-hour cache write, the dearest input rate
    assert seat.status()["held_usd"] == planned["estimate_usd"] > 0


def test_the_results_settle_the_usage_that_was_billed_and_the_estimate_is_released(remote, tmp_path, api):
    from rps.ledger import Usage
    from rps.review.api_seat import SeatConfig, cost_of

    usages = {
        "_0": {"input_tokens": 200, "output_tokens": 100, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0},
        "_1": {"input_tokens": 200, "output_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3000},
        "_2": {"input_tokens": 200, "output_tokens": 500, "output_tokens_details": {"thinking_tokens": 400}},
    }
    api.batches.answer = lambda request: succeeded(request["custom_id"], verdict_text("keep"), usage=usages[row_of(request)[-2:]])
    publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    assert seat.status()["held_usd"] > 0 and seat.status()["spend_usd"] == 0
    api.batches.end()
    seat.step()
    rates = SeatConfig.from_dict(seat_config()).prices[MODEL].batch_rates()
    expected = (
        cost_of(Usage(input_tokens=200, cache_write_tokens=3000, output_tokens=100), rates)
        + cost_of(Usage(input_tokens=200, cache_read_tokens=3000, output_tokens=100), rates)
        + cost_of(Usage(input_tokens=200, output_tokens=100, reasoning_tokens=400), rates)
    )
    status = seat.status()
    assert status["spend_usd"] == pytest.approx(expected) and status["held_usd"] == 0
    (settled,) = ledger_records(remote, "settled")
    assert settled["usage"] == {"input_tokens": 600, "cache_write_tokens": 3000, "cache_read_tokens": 3000, "output_tokens": 300, "reasoning_tokens": 400}


def test_a_batch_that_does_not_fit_the_budget_is_not_sent_and_the_seat_says_so(remote, tmp_path, api):
    packet = publish(remote, rows=3)
    probe, _ = make_seat(remote, tmp_path / "probe", FakeAnthropic())
    estimate = packet_estimate(probe, remote, packet)
    seat, printed = make_seat(remote, tmp_path, api, budget_usd=estimate * 0.99)
    assert seat.step().sent == 0 and api.batches.batches == {}
    assert ledger_records(remote) == []  # nothing was planned
    notice = [line for line in printed if "nothing sent" in line]
    assert len(notice) == 1 and f"packet {packet.packet_id} needs about" in notice[0] and "budget is left" in notice[0]
    seat.step()
    assert len([line for line in printed if "nothing sent" in line]) == 1  # said once, not on every pass
    exact, _ = make_seat(remote, tmp_path / "exact", api, budget_usd=estimate)  # what exactly fits is sent
    assert exact.step().sent == 1


def test_a_batch_is_cut_to_the_packets_the_budget_has_room_for_and_what_is_out_is_held(remote, tmp_path, api):
    packets = [publish(remote, rows=2, line=f"00{number}") for number in (1, 2, 3)]
    probe, _ = make_seat(remote, tmp_path / "probe", FakeAnthropic())
    each = packet_estimate(probe, remote, packets[0])
    seat, printed = make_seat(remote, tmp_path, api, budget_usd=each * 2.5)
    assert seat.step().requests == 4  # two whole packets
    assert any("no more sent" in line and "budget is left" in line for line in printed)
    assert seat.step().sent == 0  # what is out holds its estimate, so the third packet waits for it
    assert seat.status()["held_usd"] == pytest.approx(2 * each)
    run_until_quiet(seat, api)
    assert all(verdict_file(remote, packet) is not None for packet in packets)  # the estimates were released, so it went out
    assert [len(batch["requests"]) for batch in api.batches.batches.values()] == [4, 2]


def test_what_was_spent_in_earlier_runs_counts_against_the_budget(remote, tmp_path, api):
    publish(remote, rows=3, line="001")
    first, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(first, api)
    spent = first.status()["spend_usd"]
    assert spent == pytest.approx(3 * PER_REQUEST)
    later = publish(remote, rows=3, line="002")
    second, printed = make_seat(remote, tmp_path, api, budget_usd=spent / 2)  # a new run, with less than was already spent
    assert second.status()["spend_usd"] == spent
    assert second.step().sent == 0 and any("nothing sent" in line for line in printed)
    third, _ = make_seat(remote, tmp_path, api, budget_usd=5.0)
    run_until_quiet(third, api)
    assert verdict_file(remote, later) is not None and third.status()["spend_usd"] == pytest.approx(6 * PER_REQUEST)


def test_a_row_too_long_to_be_priced_is_never_sent(remote, tmp_path, api):
    long_row = publish(remote, rows=1, line="001", texts=["word " * 90_000])
    good = publish(remote, rows=2, line="002")
    assert len("word " * 90_000) // 3 > LONG_PROMPT_TOKENS
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    assert verdict_file(remote, long_row) is None and verdict_file(remote, good) is not None
    assert "longer than" in seat.status()["skipped"][long_row.packet_id]


# ---------------------------------------------------------------------------- crashes


def test_a_seat_started_after_a_crash_collects_the_batches_in_the_ledger(remote, tmp_path, api):
    packet = publish(remote, rows=3)
    first, _ = make_seat(remote, tmp_path, api)
    first.step()
    api.batches.end()
    del first  # the process is gone
    second, printed = make_seat(remote, tmp_path, api)
    assert second.status()["in_flight"]["batches"] == 1 and second.status()["held_usd"] > 0
    result = second.step()
    assert (result.settled, result.answered, result.sent) == (1, 1, 0)
    assert len(api.batches.sent) == 3 and len(verdict_file(remote, packet)) == 3  # nothing was sent twice
    assert any("batch msgbatch_0001 done: 3 judged, 0 failed" in line for line in printed)


def test_the_batch_id_and_the_estimate_are_in_the_ledger_before_anything_waits(remote, tmp_path, api):
    publish(remote, rows=2)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    planned, submitted = ledger_records(remote)
    assert (planned["kind"], submitted["kind"]) == ("planned", "submitted")
    assert planned["key"] == submitted["key"] and submitted["batch_id"] == "msgbatch_0001"
    assert planned["estimate_usd"] > 0 and planned["model"] == MODEL
    assert [item["attempt"] for item in planned["requests"]] == [1, 1]
    assert {item["custom_id"] for item in planned["requests"]} == {request["custom_id"] for request in api.batches.sent}


def test_a_send_that_never_reached_the_provider_is_dropped_after_a_wait_and_sent_again(remote, tmp_path, api):
    packet = publish(remote, rows=2)
    seat, printed = make_seat(remote, tmp_path, api)
    api.batches.fail_create = ConnectionError("the network went away")
    with pytest.raises(ConnectionError):
        seat.step()
    assert api.batches.batches == {} and seat.status()["in_flight"]["batches"] == 1  # planned: held, not sent
    api.batches.fail_create = None
    assert seat.step().sent == 0 and api.batches.listed >= 1  # it looked for the batch, found none, and waits
    api.clock.advance(seconds=RECOVER_WAIT.total_seconds() + 1)
    assert seat.step().sent == 1  # now it drops the plan and sends again
    assert [record["kind"] for record in ledger_records(remote)] == ["planned", "dropped", "planned", "submitted"]
    assert any("dropped a send of 2 requests" in line for line in printed)
    run_until_quiet(seat, api)
    assert len(verdict_file(remote, packet)) == 2 and len(api.batches.batches) == 1  # the one batch is the only one the provider made


def test_a_send_whose_reply_was_lost_is_found_by_its_size_and_time_and_not_sent_twice(remote, tmp_path, api):
    packet = publish(remote, rows=3)
    seat, printed = make_seat(remote, tmp_path, api)
    api.batches.lose_reply = TimeoutError("no reply")
    with pytest.raises(TimeoutError):
        seat.step()
    assert len(api.batches.batches) == 1  # the provider made it
    api.batches.lose_reply = None
    seat.step()
    assert len(api.batches.batches) == 1 and seat.status()["in_flight"]["batches"] == 1
    assert [record["kind"] for record in ledger_records(remote)] == ["planned", "submitted"]
    assert ledger_records(remote, "submitted")[0]["adopted"] is True
    assert any("took up batch msgbatch_0001" in line for line in printed)
    run_until_quiet(seat, api)
    assert len(verdict_file(remote, packet)) == 3 and len(api.batches.batches) == 1


def test_a_batch_taken_up_by_its_size_that_holds_none_of_the_requests_is_dropped_and_its_rows_go_again(remote, tmp_path, api):
    packet = publish(remote, rows=2)
    seat, printed = make_seat(remote, tmp_path, api)
    api.batches.fail_create = ConnectionError("down")
    with pytest.raises(ConnectionError):
        seat.step()
    api.batches.fail_create = None
    api.batches.create(requests=[{"custom_id": "a-strangers-1", "params": {}}, {"custom_id": "a-strangers-2", "params": {}}])  # same size, made after the plan
    api.batches.answer = lambda request: verdict_text("keep") if not request["custom_id"].startswith("a-strangers") else "x"
    api.batches.end()
    seat.step()  # takes up the stranger's batch, reads its results and finds none of its own
    assert any("holds none of the requests" in line for line in printed)
    assert [record["kind"] for record in ledger_records(remote)][:3] == ["planned", "submitted", "dropped"]
    assert seat.status()["spend_usd"] == 0 and seat.status()["failures"] == {}
    run_until_quiet(seat, api)
    assert len(verdict_file(remote, packet)) == 2


def test_a_crash_between_keeping_the_verdicts_and_settling_the_batch_collects_it_again_and_changes_nothing(remote, tmp_path, api):
    packet = publish(remote, rows=3)
    first, _ = make_seat(remote, tmp_path, api)
    first.step()
    api.batches.end()
    real_add = first.ledger.add

    def die_on_settle(record, **options):
        if record["kind"] == "settled":
            raise RuntimeError("killed")
        return real_add(record, **options)

    first.ledger.add = die_on_settle
    with pytest.raises(RuntimeError, match="killed"):
        first.step()
    kept = remote.read_text(f"review/{REVIEWER}/seat/judged/{packet.packet_id}.jsonl").splitlines()
    assert len(kept) == 3 and ledger_records(remote, "settled") == []  # the verdicts are safe, the batch is not marked
    second, _ = make_seat(remote, tmp_path, api)
    second.step()
    assert len(ledger_records(remote, "settled")) == 1 and len(verdict_file(remote, packet)) == 3
    assert second.status()["spend_usd"] == pytest.approx(3 * PER_REQUEST)  # the spend counts once


def test_a_batch_the_provider_no_longer_knows_fails_its_requests_and_the_whole_estimate_counts_as_spent(remote, tmp_path, api):
    packet = publish(remote, rows=2)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    held = seat.status()["held_usd"]
    api.batches.unknown.add("msgbatch_0001")
    seat.step()
    assert seat.status()["failures"] == {"lost": 2} and seat.status()["spend_usd"] == held
    run_until_quiet(seat, api)
    assert len(verdict_file(remote, packet)) == 2
    assert [attempt_of(request) for request in list(api.batches.batches.values())[1]["requests"]] == [2, 2]


def test_a_batch_whose_results_can_no_longer_be_read_is_settled_as_lost_at_its_whole_estimate(remote, tmp_path, api):
    packet = publish(remote, rows=2)
    seat, printed = make_seat(remote, tmp_path, api)
    seat.step()
    held = seat.status()["held_usd"]
    api.batches.end()
    api.batches.results_error = ApiError(404, "the results have expired")
    seat.step()
    assert seat.status()["failures"] == {"lost": 2} and seat.status()["spend_usd"] == held and seat.status()["held_usd"] > 0  # and the rows went again
    assert any("2 failed (2 lost)" in line for line in printed)
    api.batches.results_error = None
    run_until_quiet(seat, api)
    assert len(verdict_file(remote, packet)) == 2


def test_a_result_of_a_kind_the_seat_does_not_know_is_a_failed_attempt(remote, tmp_path, api):
    class Strange:
        custom_id = None
        result = type("Result", (), {"type": "paused"})()

    def answer(request):
        strange = Strange()
        strange.custom_id = request["custom_id"]
        return strange

    api.batches.answer = answer
    publish(remote, rows=1)
    seat, _ = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.end()
    seat.step()
    (failure,) = ledger_records(remote, "settled")[0]["failed"]
    assert failure["kind"] == "errored" and "unknown result type 'paused'" in failure["reason"]


def test_a_ledger_record_this_version_cannot_read_is_left_out_and_the_rest_is_kept(remote, tmp_path, api):
    publish(remote, rows=2)
    first, _ = make_seat(remote, tmp_path, api)
    first.step()
    path = f"review/{REVIEWER}/seat/ledger.jsonl"
    remote.append_lines(path, [json.dumps({"kind": "planned", "key": "k1"}), json.dumps({"kind": "settled", "key": "k2", "usd": "x"}), json.dumps({"kind": "future-kind"})])
    second, _ = make_seat(remote, tmp_path, api)
    assert second.status()["in_flight"]["batches"] == 1 and second.status()["spend_usd"] == 0


def test_a_batch_that_is_over_is_remembered_by_its_key_and_id_only(remote, tmp_path, api):
    publish(remote, rows=3)
    seat, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(seat, api)
    (batch,) = seat.ledger.batches.values()
    assert batch.state == "settled" and batch.batch_id == "msgbatch_0001" and batch.asks == ()
    assert seat.status()["in_flight"] == {"batches": 0, "requests": 0, "packets": 0}


def test_verdicts_kept_for_a_packet_a_desk_seat_answered_first_are_dropped_without_a_word(remote, tmp_path, api):
    api.batches.answer = lambda request: errored(request["custom_id"]) if row_of(request).endswith("_1") else verdict_text("keep")
    packet = publish(remote, rows=3)
    seat, printed = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.end()
    seat.step()  # two verdicts are kept, one row goes again
    desk = ReviewerClient(REVIEWER, remote, tmp_path / "desk", fsync=False)
    desk.download(packet.packet_id)
    for item in packet.rows:
        desk.record_verdict(packet.packet_id, item.row_id, "reject", ["person"], "a person stands in the doorway, thought 1")
    assert desk.push(packet.packet_id).pushed
    api.batches.end()
    seat.step()
    assert [line["verdict"] for line in verdict_file(remote, packet)] == ["reject"] * 3  # the file stays the desk seat's
    assert not remote.exists(f"review/{REVIEWER}/seat/judged/{packet.packet_id}.jsonl")
    assert not any("taken back" in line for line in printed)


def test_a_torn_ledger_line_is_skipped(remote, tmp_path, api):
    publish(remote, rows=2)
    first, _ = make_seat(remote, tmp_path, api)
    first.step()
    remote.append_lines(f"review/{REVIEWER}/seat/ledger.jsonl", ['{"kind": "settled", "key": "tor'])
    second, _ = make_seat(remote, tmp_path, api)
    assert second.status()["in_flight"]["batches"] == 1


def test_verdicts_kept_for_a_packet_the_controller_took_back_are_dropped_and_no_file_is_written(remote, tmp_path, api):
    api.batches.answer = lambda request: errored(request["custom_id"]) if row_of(request).endswith("_1") else verdict_text("keep")
    packet = publish(remote, rows=3)
    seat, printed = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.end()
    seat.step()  # two verdicts are kept, one row goes again
    for path in remote.list(f"packets/{REVIEWER}/"):  # the controller withdraws the packet
        remote.delete(path)
    api.batches.end()
    seat.step()
    assert verdict_file(remote, packet) is None
    assert not remote.exists(f"review/{REVIEWER}/seat/judged/{packet.packet_id}.jsonl")
    assert any("taken back" in line for line in printed)


# ---------------------------------------------------------------------------- status and running


def test_the_status_counts_packets_rows_verdicts_spend_what_is_out_and_failures(remote, tmp_path, api):
    def answer(request):
        row = row_of(request)
        if "_002_" in row and row.endswith("_1"):
            return "no json"
        verdict, reasons = {"_0": ("keep", ()), "_1": ("fix", ["wording"]), "_2": ("reject", ["person"])}[row[-2:]]
        return verdict_text(verdict, reasons, "n")

    api.batches.answer = answer
    publish(remote, rows=3, line="001")
    publish(remote, rows=2, line="002")
    seat, _ = make_seat(remote, tmp_path, api)
    assert seat.status()["packets_answered"] == 0 and seat.status()["running"] is False
    run_until_quiet(seat, api)
    status = seat.status()
    assert status["packets_answered"] == 1 and status["rows_judged"] == 4
    assert (status["keep"], status["fix"], status["reject"]) == (2, 1, 1)
    assert status["failures"] == {"invalid": 3} and len(status["left_unanswered"]) == 1
    assert status["in_flight"] == {"batches": 0, "requests": 0, "packets": 0} and status["waiting_packets"] == 1
    assert status["spend_usd"] == pytest.approx(7 * PER_REQUEST) and status["held_usd"] == 0
    assert (status["reviewer_id"], status["model"], status["effort"], status["budget_usd"]) == (REVIEWER, MODEL, "high", 5.0)
    text = seat.format_status()
    for expected in ("packets answered 1; rows judged 4 (keep 2, fix 1, reject 1)", "failed attempts: 3 invalid", "left unanswered: 1 packets", "of $5.00"):
        assert expected in text
    json.dumps(status)  # it is plain data


def test_the_counts_survive_a_restart(remote, tmp_path, api):
    publish(remote, rows=3)
    first, _ = make_seat(remote, tmp_path, api)
    run_until_quiet(first, api)
    second, _ = make_seat(remote, tmp_path, api)
    keys = ("packets_answered", "rows_judged", "keep", "spend_usd")
    assert {key: second.status()[key] for key in keys} == {key: first.status()[key] for key in keys}


def test_run_steps_until_it_is_stopped(remote, tmp_path, api):
    api.batches.auto_end = True
    packet = publish(remote, rows=2)
    stop = threading.Event()
    seat, _ = make_seat(remote, tmp_path, api)
    seat._print = lambda line: stop.set() if "answered" in line else None
    assert seat.run(stop) == "stopped"
    assert len(verdict_file(remote, packet)) == 2 and seat.running is False
    assert seat.run(stop) == "stopped"  # a stop that is already set ends it at once


def test_a_seat_stops_for_good_when_the_provider_refuses_the_key_or_the_request(remote, tmp_path, api):
    publish(remote, rows=2)
    api.batches.fail_create = ApiError(401, "invalid x-api-key")
    seat, printed = make_seat(remote, tmp_path, api)
    assert seat.run(threading.Event()) == "halted"
    assert "refused the request" in seat.status()["halted"] and seat.status()["in_flight"]["batches"] == 0 and seat.status()["held_usd"] == 0
    assert [record["kind"] for record in ledger_records(remote)] == ["planned", "dropped"]
    assert any(line.startswith(f"[seat {REVIEWER}] stopped: ") for line in printed)
    assert api.batches.batches == {}


def test_a_pass_that_fails_is_said_and_the_next_pass_goes_on(remote, tmp_path, api):
    api.batches.auto_end = True
    packet = publish(remote, rows=2)
    stop = threading.Event()
    lines = []

    def printer(line):
        lines.append(line)
        if "pass failed" in line:
            api.batches.retrieve_error = None
        if "answered" in line:
            stop.set()

    seat, _ = make_seat(remote, tmp_path, api)
    seat._print = printer
    seat.step()  # sent
    api.batches.retrieve_error = ApiError(503, "unavailable")
    assert seat.run(stop) == "stopped"
    assert any("pass failed (ApiError (HTTP 503): unavailable); 1 in a row" in line for line in lines)
    assert len(verdict_file(remote, packet)) == 2 and seat.last_error is None


def test_a_seat_that_keeps_failing_stops_after_a_run_of_failed_passes(remote, tmp_path, api, monkeypatch):
    monkeypatch.setattr(api_seat, "MAX_FAILED_STEPS", 3)
    publish(remote, rows=2)
    seat, printed = make_seat(remote, tmp_path, api)
    seat.step()
    api.batches.retrieve_error = ApiError(500, "boom")
    assert seat.run(threading.Event()) == "failing"
    assert len([line for line in printed if "pass failed" in line]) == 3
    assert "3 passes in a row failed" in seat.status()["halted"] and "boom" in seat.status()["last_error"]


def test_start_in_thread_runs_the_seat_beside_the_caller_and_stop_ends_it(remote, tmp_path, api):
    api.batches.auto_end = True
    packet = publish(remote, rows=2)
    handle = api_seat.start_in_thread(
        seat_config(poll_seconds=0.01), store=remote, client=api, work_dir=tmp_path / "seat-work", printer=lambda line: None, fsync=False
    )
    deadline = time.monotonic() + 30
    while handle.status()["packets_answered"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    status = handle.status()
    assert status["packets_answered"] == 1 and status["rows_judged"] == 2 and status["running"] is True
    assert handle.alive and "running" in handle.format_status()
    assert handle.stop(timeout=30) is True and not handle.alive
    assert handle.status()["running"] is False and len(verdict_file(remote, packet)) == 2


def test_without_a_key_the_seat_will_not_start(remote, tmp_path):
    with pytest.raises(SeatHalted, match="ANTHROPIC_API_KEY is not set"):
        api_seat.ApiSeat(seat_config(), remote, work_dir=tmp_path, fsync=False)


def test_the_client_is_made_from_the_environment_key_and_the_key_is_never_shown(remote, tmp_path, monkeypatch):
    import anthropic

    made = {}

    class Recorder:
        def __init__(self, **options):
            made.update(options)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test-value-from-the-environment")
    monkeypatch.setattr(anthropic, "Anthropic", Recorder)
    seat = api_seat.ApiSeat(seat_config(), remote, work_dir=tmp_path, printer=lambda line: None, fsync=False)
    assert isinstance(seat._api, Recorder) and made == {"api_key": "a-test-value-from-the-environment"}
    shown = seat.format_status() + json.dumps(seat.status()) + json.dumps(seat.config.to_dict())
    assert "a-test-value" not in shown


def test_it_works_on_a_buffered_bucket_store_and_leaves_nothing_unsent(tmp_path):
    bucket = FakeBucketApi()
    clock = StoreClock()

    def store():
        return BucketStore("owner/state/test", api=bucket, clock=clock, sleep=clock.sleep, rng=lambda: 1.0, cache_dir=tmp_path / "cache")

    controller_store = store()
    packet = publish(controller_store, rows=3)
    controller_store.flush(force=True)
    api = FakeAnthropic()
    seat_store = store()
    seat, _ = make_seat(seat_store, tmp_path, api)
    seat.step()
    assert seat_store.pending_count == 0  # everything the pass wrote has really been sent
    assert f"test/review/{REVIEWER}/seat/ledger.jsonl" in bucket.files
    api.batches.end()
    seat.step()
    assert seat_store.pending_count == 0
    assert f"test/review/{REVIEWER}/{packet.packet_id}.jsonl" in bucket.files
    assert f"test/review/{REVIEWER}/seat/judged/{packet.packet_id}.jsonl" not in bucket.files
    assert ActorStore(store(), controller_actor("20261108T110000Z-aaaa")).read_text(layout.verdicts_path(REVIEWER, packet.packet_id))  # the controller reads it like any other


# ---------------------------------------------------------------------------- the command line


def write_files(tmp_path, **seat_overrides):
    work = tmp_path / "run-work"
    run_config = tmp_path / "run.json"
    run_config.write_text(json.dumps({"work_dir": str(work)}), encoding="utf-8")
    seat_file = tmp_path / "seat.json"
    seat_file.write_text(
        json.dumps(seat_config(run_config=str(run_config), work_dir=str(tmp_path / "seat-local"), **seat_overrides)), encoding="utf-8"
    )
    return seat_file, LocalStore(work / "state", fsync=False)


def test_the_command_line_runs_one_pass_with_the_run_configurations_store(tmp_path, monkeypatch, api):
    seat_file, state = write_files(tmp_path)
    packet = publish(state, rows=3)
    monkeypatch.setattr(api_seat, "make_client", lambda: api)
    out, err = io.StringIO(), io.StringIO()
    assert api_seat.main(["--config", str(seat_file), "--once"], out, err) == api_seat.EXIT_OK
    assert "sent batch msgbatch_0001: 1 packets, 3 rows" in out.getvalue() and f"reviewer {REVIEWER} (" in out.getvalue() and err.getvalue() == ""
    api.batches.end()
    out = io.StringIO()
    assert api_seat.main(["--config", str(seat_file), "--once"], out, err) == api_seat.EXIT_OK  # a new process finds the batch in the ledger
    assert "answered 1 packets (3 rows: 3 keep, 0 fix, 0 reject)" in out.getvalue() and "packets answered 1" in out.getvalue()
    assert len(verdict_file(state, packet)) == 3


def test_the_command_line_reports_a_configuration_it_cannot_use(tmp_path, monkeypatch, api):
    monkeypatch.setattr(api_seat, "make_client", lambda: api)
    err = io.StringIO()
    assert api_seat.main(["--config", str(tmp_path / "missing.json"), "--once"], io.StringIO(), err) == api_seat.EXIT_FAILED
    assert "cannot read the seat configuration" in err.getvalue()
    for data, message in (
        (seat_config(speed="fast"), "unknown key"),
        (seat_config(), "needs a state store"),
        (seat_config(model="claude-opus-9"), "no prices for 'claude-opus-9'"),
    ):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(data), encoding="utf-8")
        err = io.StringIO()
        assert api_seat.main(["--config", str(bad), "--once"], io.StringIO(), err) == api_seat.EXIT_FAILED
        assert message in err.getvalue()


def test_the_command_line_exits_with_the_refused_code_when_the_provider_turns_the_key_away(tmp_path, monkeypatch, api):
    seat_file, state = write_files(tmp_path)
    publish(state, rows=2)
    api.batches.fail_create = ApiError(403, "forbidden")
    monkeypatch.setattr(api_seat, "make_client", lambda: api)
    out, err = io.StringIO(), io.StringIO()
    code = api_seat.main(["--config", str(seat_file)], out, err)  # without --once it would run until stopped; this halts at once
    assert code == api_seat.EXIT_REFUSED and "stopped: the provider refused the request" in out.getvalue()


def test_an_interrupt_ends_the_command_line_cleanly_and_says_where_things_stand(tmp_path, monkeypatch, api):
    seat_file, state = write_files(tmp_path)
    publish(state, rows=2)
    monkeypatch.setattr(api_seat, "make_client", lambda: api)

    def interrupted(self, stop=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(api_seat.ApiSeat, "run", interrupted)
    out, err = io.StringIO(), io.StringIO()
    assert api_seat.main(["--config", str(seat_file)], out, err) == api_seat.EXIT_OK
    assert "interrupted; batches already sent are collected the next time the seat starts" in out.getvalue()
    assert f"reviewer {REVIEWER} (" in out.getvalue() and err.getvalue() == ""


# ---------------------------------------------------------------------------- beside the controller


def test_the_controller_reads_the_seats_verdict_files_like_any_other_and_needs_no_change(tmp_path):
    reviewers = [{"id": REVIEWER, "family": "anthropic"}, {"id": "rev-b", "family": "beta"}]
    config = make_config(
        review={
            "packet_size": 3,
            "brief_version": rules.rules_version(),  # no writing rules are loaded here, so the packets carry this
            "reviewers": reviewers,
            "provider_families": {"fake": "writer-family"},
            "policy": {"audit_fraction": 0.0},
        }
    )
    world = World(tmp_path, config=config)
    controller = world.controller()
    controller.add_task(task_spec())
    api = FakeAnthropic()
    api.batches.auto_end = True
    api.batches.answer = lambda request: (
        verdict_text("reject", ["invented"], "thought 1 invents a color")
        if asked_row(request)["task"]["line_id"] == "005"
        else verdict_text("keep")
    )
    seat = api_seat.ApiSeat(seat_config(), world.state_store, client=api, work_dir=tmp_path / "seat", printer=lambda line: None, fsync=False)
    for _round in range(30):
        controller.step()
        seat.step()  # sends what the controller published
        seat.step()  # collects it and pushes the verdict files
        world.run_reviewers(by_line({"005": "reject"}), only="rev-b")  # the desk seat, for the row sent on to a further reviewer
        if controller.is_idle() and not controller.state.open_packets():
            break
    else:
        raise AssertionError("the run did not settle")
    received = [event.payload for event in world.events_of("verdict_received") if event.payload["reviewer_id"] == REVIEWER]
    assert received and {payload["family"] for payload in received} == {"anthropic"}
    assert {payload["verdict"] for payload in received} == {"keep", "reject"}
    assert [payload["reasons"] for payload in received if payload["verdict"] == "reject"] == [["invented"]]
    shipped = {row["row_id"].split("_")[1] for row in world.shipped_rows()}
    assert shipped == {"001", "002", "003", "004", "006"}  # two rejects discarded the row of line 005, the seat's and the desk's
    assert seat.status()["packets_answered"] == 1 and seat.status()["rows_judged"] == 3 and seat.status()["halted"] is None  # lines 001, 003 and 005


def test_open_seat_needs_a_store_from_somewhere(api):
    with pytest.raises(ConfigError, match="needs a state store"):
        api_seat.open_seat(seat_config(), client=api)
