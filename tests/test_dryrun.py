"""The dry-run tool: a few real calls through one route, under the test cap."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from rps import dryrun
from rps.config import ConfigError, RouteConfig
from rps.ledger import Usage
from rps.providers import Acceptance, FakeOutcome, FakeProvider, FakeWriter

GOOD = json.dumps({"answer": "ok", "number": 1})
SRC = Path(__file__).resolve().parents[1] / "src"


def fake_route(kind="batch", **overrides):
    fields = dict(provider="fake", model="fake-model", kind=kind, budget_usd=5.0, max_output_tokens=300, workers=2)
    fields.update(overrides)
    return RouteConfig(**fields)


def good(payload):
    return FakeOutcome(output=json.dumps({"answer": "ok", "number": int(payload["line"])}), usage=Usage(input_tokens=1000, output_tokens=200))


class Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def run(tmp_path, *, behavior=good, calls=5, writer=None, config=None, polls=0, **options):
    printed = []
    clock = Clock()
    provider = FakeProvider()
    config = config or fake_route()
    writer = writer or FakeWriter("luna-batch", config, provider=provider, behavior=behavior, polls_until_ended=polls)
    options.setdefault("spend_file", tmp_path / "spend.json")
    report = dryrun.dry_run(
        "luna-batch", calls, route_config=config, writer=writer, printer=printed.append, sleep=clock.sleep, clock=clock, **options
    )
    return report, printed, writer, clock


def test_a_good_run_reports_every_answer_the_usage_and_the_cost(tmp_path):
    report, printed, writer, _clock = run(tmp_path)
    assert report.ok and report.refused is None and len(report.results) == 5
    assert [answer["number"] for answer in report.outputs] == [1, 2, 3, 4, 5]
    assert report.usage == Usage(input_tokens=5000, output_tokens=1000)
    # the fake model's batch rate: 5 x (1000 input + 200 output tokens)
    assert report.cost_usd == pytest.approx(5 * (1000 * 1.0 + 200 * 2.0) / 1e6 * 0.5)
    assert 0 < report.cost_usd <= report.estimate_usd
    text = "\n".join(printed)
    assert "RESULT: all calls came back with a good answer" in text and '{"answer": "ok", "number": 3}' in text
    assert writer.provider.submit_count == 1


def test_the_calls_carry_the_plumbing_prompt_and_never_the_writing_rules(tmp_path):
    seen = []

    class Spy(FakeWriter):
        def prepare(self, calls):
            seen.extend(calls)
            return super().prepare(calls)

    config = fake_route()
    run(tmp_path, writer=Spy("luna-batch", config, behavior=good))
    assert {call.purpose for call in seen} == {"test"}
    assert all(call.rules.system == dryrun.SYSTEM and call.rules.schema == dryrun.SCHEMA for call in seen)
    assert all("json" in call.rules.system.lower() for call in seen)
    from rps import rules as pool

    assert all(call.rules.version != pool.rules_version() and call.rules.system != pool.writer_system() for call in seen)
    assert seen[0].task_text == "Call 1. Set answer to the word ok and number to 1."


def test_the_batch_id_is_on_disk_before_the_wait_begins(tmp_path):
    seen = {}
    clock = Clock()

    def sleep(seconds):
        files = list(tmp_path.glob("dryrun_luna-batch_*.json"))
        seen["files"] = files
        seen["saved"] = json.loads(files[0].read_text(encoding="utf-8"))
        clock.sleep(seconds)

    config = fake_route()
    writer = FakeWriter("luna-batch", config, behavior=good, polls_until_ended=2)
    dryrun.dry_run("luna-batch", 3, route_config=config, writer=writer, spend_file=tmp_path / "spend.json", printer=lambda t: None, sleep=sleep, clock=clock)
    assert len(seen["files"]) == 1
    assert seen["saved"]["route"] == "luna-batch" and len(seen["saved"]["call_ids"]) == 3
    assert seen["saved"]["handles"][0]["handle_id"].startswith("fake-batch-")


def test_a_run_that_would_pass_the_test_cap_is_refused_before_anything_is_sent(tmp_path):
    report, printed, writer, _clock = run(tmp_path, cap_usd=0.0000001)
    assert report.refused and "test cap" in report.refused and not report.ok
    assert writer.provider.submit_count == 0 and report.results == []
    assert any("refused" in line for line in printed)


def test_earlier_dry_runs_count_against_the_cap(tmp_path):
    first, _printed, _writer, _clock = run(tmp_path, cap_usd=0.01)
    assert first.ok and first.spent_usd == pytest.approx(first.cost_usd)
    saved = json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))
    assert saved["luna-batch"] == pytest.approx(first.cost_usd)
    # room for the estimate of one more run is gone once the first run's cost is added
    second, _printed, writer, _clock = run(tmp_path, cap_usd=first.cost_usd + first.estimate_usd / 2)
    assert second.refused and writer.provider.submit_count == 0
    third, _printed, _writer, _clock = run(tmp_path, cap_usd=first.cost_usd + first.estimate_usd * 2)
    assert third.ok and third.spent_usd == pytest.approx(first.cost_usd * 2)


@pytest.mark.parametrize("calls", [0, -1, dryrun.MAX_CALLS + 1])
def test_a_dry_run_sends_only_a_handful_of_calls(tmp_path, calls):
    report, _printed, writer, _clock = run(tmp_path, calls=calls)
    assert report.refused and writer.provider.submit_count == 0


def test_a_route_that_is_closed_is_refused_with_the_reason(tmp_path):
    class Closed(FakeWriter):
        def accepting(self):
            return Acceptance(False, "OPENAI_API_KEY is not set")

    config = fake_route()
    report, _printed, writer, _clock = run(tmp_path, writer=Closed("luna-batch", config, behavior=good))
    assert report.refused == "route luna-batch is closed: OPENAI_API_KEY is not set"
    assert writer.provider.submit_count == 0


def test_rules_a_route_cannot_send_are_refused(tmp_path):
    class Picky(FakeWriter):
        def check_rules(self, rules):
            raise ConfigError("this route cannot send these rules")

    config = fake_route()
    report, _printed, _writer, _clock = run(tmp_path, writer=Picky("luna-batch", config, behavior=good))
    assert report.refused == "this route cannot send these rules"


def test_an_answer_that_does_not_fit_the_schema_and_a_failed_call_are_reported_as_such(tmp_path):
    def behavior(payload):
        line = payload["line"]
        if line == "001":
            return FakeOutcome(output=json.dumps({"answer": "ok"}))  # no number
        if line == "002":
            return FakeOutcome(error="HTTP 500: oops", retryable=True, kind="provider_error")
        return good(payload)

    report, printed, _writer, _clock = run(tmp_path, behavior=behavior, calls=3)
    assert not report.ok
    text = "\n".join(printed)
    assert "wrong form" in text and "failed (provider_error)" in text and "something did not work" in text


def test_the_wait_polls_until_the_batch_ends_and_says_how_it_is_going(tmp_path):
    report, printed, _writer, clock = run(tmp_path, polls=3, poll_seconds=7)
    assert report.ok and clock.slept == [7, 7, 7]
    assert any("checking again in 7 s" in line for line in printed)


def test_a_stopped_wait_can_be_picked_up_again_without_sending_anything_twice(tmp_path):
    provider = FakeProvider()
    config = fake_route()
    clock = Clock()
    writer = FakeWriter("luna-batch", config, provider=provider, behavior=good, polls_until_ended=1000)
    printed = []
    first = dryrun.dry_run(
        "luna-batch", 3, route_config=config, writer=writer, spend_file=tmp_path / "spend.json", printer=printed.append,
        sleep=clock.sleep, clock=clock, wait_minutes=1, poll_seconds=20,
    )
    assert first.interrupted and not first.ok and provider.submit_count == 1
    assert first.handle_file and Path(first.handle_file).exists()
    assert any("resume=" in line for line in printed)
    # the batch finishes later; a new tool instance on the same provider collects it
    provider.poll_counts.clear()
    again = FakeWriter("luna-batch", config, provider=provider, behavior=good, polls_until_ended=0)
    second = dryrun.dry_run(
        "luna-batch", route_config=config, writer=again, spend_file=tmp_path / "spend.json", printer=printed.append,
        sleep=clock.sleep, clock=clock, resume=first.handle_file,
    )
    assert second.ok and len(second.results) == 3 and provider.submit_count == 1


def test_a_picture_goes_only_to_a_route_that_may_see_it(tmp_path):
    picture = tmp_path / "p.jpg"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(picture)
    sent = []

    class Spy(FakeWriter):
        def prepare(self, calls):
            sent.extend((call.image is not None, call.image_class) for call in calls)
            return super().prepare(calls)

    config = fake_route()
    report, _printed, _writer, _clock = run(tmp_path, writer=Spy("luna-batch", config, behavior=good), image=picture, image_class="coco", calls=2)
    assert report.ok and sent == [(True, "coco"), (True, "coco")]
    blocked, _printed, writer, _clock = run(tmp_path, image=picture, image_class="deepfashion", calls=2)
    assert blocked.refused and "deepfashion" in blocked.refused and writer.provider.submit_count == 0
    claude = fake_route(provider="anthropic", model="claude-haiku-5-5")
    claude_run, _printed, _writer, _clock = run(
        tmp_path, image=picture, image_class="coco", config=claude, writer=FakeWriter("haiku-batch", claude, behavior=good), calls=2
    )
    assert claude_run.refused is None and claude_run.ok  # the table lets a Claude route see the pictures of a public dataset
    for closed in ("deepfashion", "hico"):
        claude_closed, _printed, _writer, _clock = run(
            tmp_path, image=picture, image_class=closed, config=claude, writer=FakeWriter("haiku-batch", claude, behavior=good), calls=2
        )
        assert claude_closed.refused and "claude routes" in claude_closed.refused and closed in claude_closed.refused
    missing, _printed, _writer, _clock = run(tmp_path, image=tmp_path / "nope.jpg")
    assert missing.refused and "cannot read" in missing.refused


def test_the_standard_routes_are_capped_and_deepseek_is_switched_on_with_its_gate_off_for_the_test():
    config = dryrun.route_for("deepseek-sync", None, 0.25, 400, 5)
    assert config.is_enabled and config.options == {"offpeak_only": False}
    assert config.budget_usd == 0.25 and config.purpose_caps == {"test": 0.25} and config.max_output_tokens == 400
    luna = dryrun.route_for("luna-batch", None, 0.25, 400, 5)
    assert luna.provider == "openai" and luna.kind == "batch" and luna.model == "gpt-6-luna" and luna.workers == 5
    with pytest.raises(ConfigError, match="unknown route"):
        dryrun.route_for("nowhere", None, 0.25, 400, 5)


def test_the_command_line_refuses_a_route_with_no_key_and_prints_why(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = dryrun.main(["--route", "luna-batch", "--calls", "5", "--spend-file", str(tmp_path / "spend.json")])
    assert code == 2
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().out
    assert dryrun.main(["--route", "nowhere"]) == 2


def test_the_module_runs_from_the_command_line(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(PYTHONPATH=str(SRC), HF_TOKEN="")
    done = subprocess.run(
        [sys.executable, "-m", "rps.dryrun", "--route", "haiku-batch", "--spend-file", str(tmp_path / "spend.json")],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120,
    )
    assert done.returncode == 2
    assert "ANTHROPIC_API_KEY is not set" in done.stdout and not (tmp_path / "spend.json").exists()
