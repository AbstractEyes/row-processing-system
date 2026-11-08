"""The controller's handling of routes: closed and stopped routes, token-limit retries, partial results,
clean-up, remembered caches, pictures and the checks made before a run."""

from __future__ import annotations

import json

import pytest

from rps import ids
from rps.config import ConfigError
from rps.ledger import PriceTable, Usage
from rps.prompting import make_rules
from rps.providers import Acceptance, CallSpec, FakeOutcome, FakeWriter, PollStatus, RouteRejected
from rps.store.base import StoreError
from rps.tasks import SpecLine, TaskSpec

from harness import World, fake_image_loader, keep_all, task_spec
from helpers import make_config

GOOD = json.dumps({"id": "ok", "thoughts": ["fine"]})


def ok_behavior(payload):
    return FakeOutcome(output=GOOD)


class Switch(FakeWriter):
    """A fake writer whose `accepting` answer the test sets."""

    answer = Acceptance(True)

    def accepting(self):
        return type(self).answer


def with_writer(world, factory):
    world.writer_class = factory
    return world


# ---------------------------------------------------------------------------- closed and stopped routes


def test_a_route_closed_for_the_run_leaves_its_calls_alone_and_the_run_ends_idle_with_a_note(tmp_path):
    class Closed(Switch):
        answer = Acceptance(False, "ACME_API_KEY is not set")

    world = World(tmp_path, writer_class=Closed, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    summary = controller.run()
    assert summary.reason == "idle" and world.provider.submit_count == 0
    assert any("route fake-batch is closed for this run: ACME_API_KEY is not set" in problem for problem in summary.problems)
    assert controller.status()["routes"]["fake-batch"] == {"state": "closed", "reason": "ACME_API_KEY is not set"}
    assert "route fake-batch: closed (ACME_API_KEY is not set)" in controller.format_status()
    assert not world.events_of("call_prepared")


def test_a_route_closed_for_now_keeps_the_run_waiting_and_sends_when_it_opens(tmp_path):
    class Hours(Switch):
        answer = Acceptance(False, "peak hours", later=True)

    world = World(tmp_path, writer_class=Hours, behavior=ok_behavior, polls_until_ended=0)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    for _ in range(3):
        controller.step()
    assert world.provider.submit_count == 0 and not controller.is_idle()
    assert "route fake-batch: closed for now (peak hours)" in controller.format_status()
    Hours.answer = Acceptance(True)
    world.drive(controller, keep_all)
    assert world.provider.submit_count == 1 and len(world.shipped_rows()) == 1


def test_a_route_switched_off_in_the_configuration_stays_closed_whatever_the_writer_says(tmp_path):
    config = make_config(
        routes={"fake-batch": {"provider": "fake", "model": "fake-model", "kind": "batch", "enabled": False, "budget_usd": 10, "purpose_caps": {"production": 5}}}
    )
    world = World(tmp_path, config=config, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    summary = controller.run()
    assert summary.reason == "idle" and world.provider.submit_count == 0
    assert any("switched off in the configuration" in problem for problem in summary.problems)


def test_a_request_the_provider_rejects_stops_the_route_for_the_rest_of_the_run(tmp_path):
    class Rejecting(FakeWriter):
        def submit(self, requests):
            raise RouteRejected("bad_request", "the schema is wrong")

    world = World(tmp_path, writer_class=Rejecting, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    summary = controller.run()
    assert summary.reason == "idle"
    [halted] = world.events_of("route_halted")
    assert halted.payload["route"] == "fake-batch" and halted.payload["kind"] == "bad_request" and "schema is wrong" in halted.payload["reason"]
    assert any("stopped for this run (bad_request)" in problem for problem in summary.problems)
    assert controller.route_state("fake-batch")[0] == "closed"
    # the next run starts with the route open again (the key or the request may have been fixed)
    again = world.controller()
    assert again.route_state("fake-batch")[0] == "open"


def test_a_call_result_of_kind_auth_stops_the_route(tmp_path):
    def behavior(payload):
        return FakeOutcome(error="HTTP 401: invalid key", retryable=False, kind="auth")

    world = World(tmp_path, behavior=behavior, polls_until_ended=0)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003")))
    controller.run()
    assert [e.payload["kind"] for e in world.events_of("route_halted")] == ["auth"]
    assert world.provider.submit_count == 1  # nothing was sent after the rejection
    failed = world.events_of("call_failed")
    assert len(failed) == 3 and all(e.payload["kind"] == "auth" and e.payload["retryable"] is False for e in failed)


# ---------------------------------------------------------------------------- failures and retries


def test_an_answer_cut_off_at_the_limit_is_retried_once_with_a_larger_limit_and_no_advice(tmp_path):
    def behavior(payload):
        if payload["attempt"] == 1:
            return FakeOutcome(error="the answer was cut off at the token limit (4096 tokens)", retryable=True, kind="truncated")
        return FakeOutcome(output=GOOD)

    world = World(tmp_path, behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    first, second = world.events_of("call_prepared")
    assert first.payload["max_output_tokens"] == 4096 and second.payload["max_output_tokens"] == 8192
    assert second.payload["feedback"] == [] and second.payload["purpose"] == "rebound"
    assert world.events_of("call_failed")[0].payload["kind"] == "truncated"
    assert len(world.shipped_rows()) == 1


def test_the_larger_limit_follows_the_routes_truncation_factor(tmp_path):
    config = make_config(
        routes={"fake-batch": {"provider": "fake", "model": "fake-model", "kind": "batch", "max_output_tokens": 1000, "truncation_factor": 3, "budget_usd": 10, "purpose_caps": {"production": 5, "rebound": 5}}}
    )
    world = World(tmp_path, config=config, behavior=lambda p: FakeOutcome(error="cut", retryable=True, kind="truncated") if p["attempt"] == 1 else FakeOutcome(output=GOOD))
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    assert [e.payload["max_output_tokens"] for e in world.events_of("call_prepared")] == [1000, 3000]


def test_a_restart_rebuilds_a_prepared_call_with_the_limit_it_was_prepared_with(tmp_path):
    seen = []

    def behavior(payload):
        seen.append((payload["attempt"], payload["max_output_tokens"]))
        if payload["attempt"] == 1:
            return FakeOutcome(error="cut", retryable=True, kind="truncated")
        return FakeOutcome(output=GOOD)

    world = World(tmp_path, behavior=behavior)
    first = world.controller()
    first.add_task(task_spec(("001",)))
    first.step()
    first.step()  # attempt 1 comes back truncated; attempt 2 is prepared with the larger limit and submitted
    second = world.controller()
    second.state_store.flush(force=True)
    world.drive(second, keep_all)
    assert (2, 8192) in seen


def test_a_refusal_is_recorded_and_the_line_is_not_sent_again(tmp_path):
    def behavior(payload):
        return FakeOutcome(error="the model declined to answer", retryable=False, kind="refused")

    world = World(tmp_path, behavior=behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    assert len(world.events_of("call_prepared")) == 1
    assert controller.state.job_progress(2)["lines_exhausted"] == 1
    assert world.events_of("call_failed")[0].payload["kind"] == "refused"


def test_an_unmarked_error_is_recorded_with_a_kind_from_its_retryability(tmp_path):
    outcomes = {"001": FakeOutcome(error="overloaded", retryable=True), "002": FakeOutcome(error="odd", retryable=False)}
    world = World(tmp_path, behavior=lambda p: outcomes[p["line"]] if p["attempt"] == 1 else FakeOutcome(output=GOOD))
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller, keep_all)
    kinds = {e.payload["call_id"].split(":")[1]: e.payload["kind"] for e in world.events_of("call_failed")}
    assert kinds == {"001": "provider_error", "002": "unexpected"}


def test_the_price_tier_of_a_call_is_recorded_and_its_cost_uses_that_tier(tmp_path):
    prices = PriceTable.from_dict(
        {
            "date": "2026-11-07",
            "models": {
                "fake-model": {
                    "tiers": {
                        "offpeak": {"input": 1.0, "output": 2.0},
                        "peak": {"input": 2.0, "output": 4.0},
                    }
                }
            },
        }
    )
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    outcomes = {"001": FakeOutcome(output=GOOD, usage=usage, tier="offpeak"), "002": FakeOutcome(output=GOOD, usage=usage, tier="peak")}
    world = World(tmp_path, behavior=lambda p: outcomes[p["line"]], prices=prices)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    world.drive(controller, keep_all)
    returned = {e.payload["call_id"].split(":")[1]: e.payload for e in world.events_of("call_returned")}
    # the route is a batch route and these tiers carry no batch card, so half of the card applies
    assert returned["001"]["tier"] == "offpeak" and returned["001"]["usd"] == pytest.approx(1.5)
    assert returned["002"]["tier"] == "peak" and returned["002"]["usd"] == pytest.approx(3.0)
    assert controller.state.calls[next(c for c in controller.state.calls if c.endswith(":002:1"))].tier == "peak"


# ---------------------------------------------------------------------------- partial results and clean-up


class Piecewise(FakeWriter):
    """Hands over its results one at a time, as a worker pool does, and records its clean-up.

    It says "ended" one poll after the last result, so the controller sees every call in under
    "partial" first. `on_poll` is called at every poll (a test can use it to break something).
    """

    log: list = []
    on_poll = None

    def poll(self, handle):
        if type(self).on_poll is not None:
            type(self).on_poll()
        done = self.provider.poll_counts.get(handle.handle_id, 0) + 1
        self.provider.poll_counts[handle.handle_id] = done
        return PollStatus("ended" if done > len(handle.call_ids) else "partial")

    def results(self, handle):
        done = self.provider.poll_counts.get(handle.handle_id, 0)
        return super().results(handle)[:done]

    def release(self, handle):
        type(self).log.append(("release", handle.handle_id))

    def close(self):
        type(self).log.append(("close", self.route_name))


def test_partial_results_are_recorded_as_they_land_and_the_batch_closes_when_all_are_in(tmp_path):
    Piecewise.log, Piecewise.on_poll = [], None
    world = World(tmp_path, writer_class=Piecewise, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003")))
    controller.step()  # prepares, submits, and polls once: the first result arrives
    assert len(world.events_of("call_returned")) == 1 and not world.events_of("batch_collected")
    assert controller.state.counts()["batches_waiting"] == 1
    controller.step()
    assert len(world.events_of("call_returned")) == 2 and not world.events_of("batch_collected")
    controller.step()  # the third result completes the batch, though the writer has not said "ended"
    assert len(world.events_of("call_returned")) == 3 and len(world.events_of("batch_collected")) == 1
    assert [name for name, _ in Piecewise.log].count("release") == 1
    controller.step()
    assert len(world.events_of("batch_collected")) == 1  # a finished batch is not collected twice


def test_results_are_made_durable_before_the_writer_cleans_up(tmp_path):
    Piecewise.log, Piecewise.on_poll = [], None
    world = World(tmp_path, writer_class=Piecewise, behavior=ok_behavior)
    controller = world.controller()
    real_flush = controller.state_store.flush
    controller.state_store.flush = lambda force=False: (Piecewise.log.append(("flush", force)), real_flush(force=force))[1]
    controller.add_task(task_spec(("001",)))
    for _ in range(3):
        controller.step()
    names = [name for name, _ in Piecewise.log]
    assert "release" in names and names.index("flush") < names.index("release")
    assert ("flush", True) in Piecewise.log


def test_clean_up_waits_for_a_store_that_could_not_flush_and_happens_on_a_later_pass(tmp_path):
    Piecewise.log = []
    store_down = {"flushes_to_fail": 0}
    # the store fails its next two flushes once the batch is first polled: the one after the partial
    # results and the one before the clean-up
    Piecewise.on_poll = lambda: store_down.update(flushes_to_fail=2) if not store_down["flushes_to_fail"] else None
    world = World(tmp_path, writer_class=Piecewise, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    real_flush = controller.state_store.flush

    def flush(force=False):
        if store_down["flushes_to_fail"] > 0:
            store_down["flushes_to_fail"] -= 1
            raise StoreError("the store is down")
        return real_flush(force=force)

    controller.state_store.flush = flush
    controller.step()
    assert len(world.events_of("batch_collected")) == 1
    assert not [entry for entry in Piecewise.log if entry[0] == "release"]  # nothing is deleted while the results may be lost
    assert any("could not flush" in line for line in world.printed)
    Piecewise.on_poll = None
    controller.step()  # the store is back: the clean-up the controller kept waiting for happens now
    assert [entry[0] for entry in Piecewise.log].count("release") == 1


def test_the_run_collects_what_has_finished_and_closes_the_writers_when_it_ends(tmp_path):
    Piecewise.log, Piecewise.on_poll = [], None
    world = World(tmp_path, writer_class=Piecewise, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002")))
    summary = controller.run(max_steps=1)
    assert summary.reason == "max_steps"
    assert ("close", "fake-batch") in Piecewise.log
    assert len(world.events_of("call_returned")) >= 1  # the last collection took in what was ready


def test_a_handle_the_writer_lost_fails_its_calls_and_they_are_sent_again_as_rebounds(tmp_path):
    class Forgetful(FakeWriter):
        def poll(self, handle):
            return PollStatus("failed", "the run that sent these calls stopped; their results are lost")

    world = World(tmp_path, writer_class=Forgetful, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    controller.step()
    failed = world.events_of("call_failed")
    assert len(failed) == 1 and failed[0].payload["kind"] == "provider_error" and failed[0].payload["billed"] is False
    world.writer_class = FakeWriter
    resumed = world.controller()
    world.drive(resumed, keep_all)
    assert [e.payload["purpose"] for e in world.events_of("call_prepared")] == ["production", "rebound"]


def test_every_batch_a_crash_left_unrecorded_is_found_again_before_anything_is_sent(tmp_path):
    from rps.providers import SimulatedCrash

    class TwoLost(FakeWriter):
        def split(self, requests):
            return [list(requests)]

        def submit(self, requests):
            if not self.provider.batches:
                for group in (requests[:2], requests[2:]):
                    super().submit(group)  # both batches reach the provider ...
                raise SimulatedCrash("the process died before either id was recorded")
            return super().submit(requests)

    world = World(tmp_path, writer_class=TwoLost, behavior=ok_behavior)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003", "004")))
    with pytest.raises(SimulatedCrash):
        first.run()
    assert world.provider.submit_count == 2 and not world.events_of("batch_submitted")
    second = world.controller()
    world.drive(second, keep_all)
    recovered = world.events_of("batch_submitted")
    assert [e.payload["recovered"] for e in recovered] == [True, True]
    assert sorted(cid for e in recovered for cid in e.payload["call_ids"]) == sorted(second.state.calls)
    assert world.provider.submit_count == 2  # nothing was sent again
    assert len(world.shipped_rows()) == 4


# ---------------------------------------------------------------------------- remembered caches


def test_a_cache_a_writer_made_is_recorded_and_handed_back_to_the_next_run(tmp_path):
    handed_back = []

    class Caching(FakeWriter):
        def submit(self, requests):
            self.notes = [("cache_created", {"route": self.route_name, "key": "m|v1", "name": "cachedContents/c1", "expires": "2026-11-07T18:00:00Z"})]
            return super().submit(requests)

        def drain_notes(self):
            notes, self.notes = getattr(self, "notes", []), []
            return notes

        def restore(self, notes):
            handed_back.extend(notes)

    world = World(tmp_path, writer_class=Caching, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    [event] = world.events_of("cache_created")
    assert event.payload["name"] == "cachedContents/c1"
    assert controller.state.caches[("fake-batch", "m|v1")]["expires"] == "2026-11-07T18:00:00Z"
    world.controller().start()
    assert [note["name"] for note in handed_back] == ["cachedContents/c1"]


# ---------------------------------------------------------------------------- pictures


def photo_task(image_class=None, route="fake-batch", source="photos"):
    return TaskSpec(
        source=source, batch="1", route=route, purpose="production", variant="",
        lines=(SpecLine("001", "a photo", item_id="img-1", image="p.jpg", image_class=image_class),),
    )


@pytest.mark.parametrize("image_class", ["deepfashion", "hico", "synthetic"])
def test_a_task_whose_pictures_may_not_go_to_the_route_is_refused_before_anything_is_recorded(tmp_path, image_class):
    world = World(tmp_path, behavior=ok_behavior)
    controller = world.controller()
    with pytest.raises(ConfigError, match="may not be sent|synthetic"):
        controller.add_task(photo_task(image_class))
    assert not world.events_of("task_created")


def claude_config():
    route = {"provider": "anthropic", "model": "claude-haiku-5-5", "kind": "batch", "effort": "low", "budget_usd": 10.0}
    return make_config(routes={"haiku-batch": {**route, "purpose_caps": {"production": 10.0}}})


@pytest.mark.parametrize("image_class", ["public_common", "coco", "ffhq"])
def test_the_pictures_of_a_public_dataset_go_to_a_claude_route_as_they_do_to_the_others(tmp_path, image_class):
    seen = []

    class Spy(FakeWriter):
        def prepare(self, calls):
            seen.extend((call.provider, call.image is not None, call.image_class) for call in calls)
            return super().prepare(calls)

    world = World(tmp_path, config=claude_config(), behavior=ok_behavior, writer_class=Spy)
    controller = world.controller()
    controller.add_task(photo_task(image_class, route="haiku-batch"))
    assert world.events_of("task_created")  # taken: nothing refused the task
    controller.step()
    assert seen == [("anthropic", True, image_class)]  # and the picture was loaded for the call


@pytest.mark.parametrize("image_class", ["deepfashion", "hico", "synthetic"])
def test_the_closed_classes_are_refused_for_a_claude_route_before_anything_is_recorded(tmp_path, image_class):
    world = World(tmp_path, config=claude_config(), behavior=ok_behavior)
    with pytest.raises(ConfigError, match="claude routes|synthetic"):
        world.controller().add_task(photo_task(image_class, route="haiku-batch"))
    assert not world.events_of("task_created")


def test_a_picture_with_no_class_anywhere_is_refused(tmp_path):
    config = make_config(sources={"photos": {"license": "CC BY-NC", "dataset": "photos"}})
    world = World(tmp_path, config=config, behavior=ok_behavior)
    with pytest.raises(ConfigError, match="needs a source class"):
        world.controller().add_task(photo_task())


def test_the_class_comes_from_the_line_or_else_the_source_and_reaches_the_writer_with_the_picture(tmp_path):
    seen = []

    class Spy(FakeWriter):
        def prepare(self, calls):
            seen.extend((call.image_class, call.image is not None) for call in calls)
            return super().prepare(calls)

    world = World(tmp_path, writer_class=Spy, behavior=ok_behavior, polls_until_ended=0)
    controller = world.controller()
    controller.add_task(photo_task())  # the source says public_common
    controller.step()
    other = TaskSpec(
        source="photos", batch="2", route="fake-batch", purpose="production", variant="",
        lines=(SpecLine("001", "another photo", item_id="img-2", image="q.jpg", image_class="ffhq"),),
    )
    controller.add_task(other)
    controller.step()
    assert ("public_common", True) in seen and ("ffhq", True) in seen
    [created, created_other] = world.events_of("task_created")
    assert created.payload["lines"][0]["image_class"] == "public_common"
    assert created_other.payload["lines"][0]["image_class"] == "ffhq"


def test_a_line_with_no_picture_records_no_class_and_sends_no_picture(tmp_path):
    seen = []

    class Spy(FakeWriter):
        def prepare(self, calls):
            seen.extend((call.image_class, call.image) for call in calls)
            return super().prepare(calls)

    world = World(tmp_path, writer_class=Spy, behavior=ok_behavior, polls_until_ended=0)
    controller = world.controller()
    controller.add_task(task_spec(("001",)))
    controller.step()
    assert seen == [(None, None)]
    assert world.events_of("task_created")[0].payload["lines"][0].get("image_class") is None


def test_a_task_recorded_before_the_table_existed_cannot_send_a_picture_to_a_route_that_may_not_have_it(tmp_path):
    """A line replayed from old events carries no class; its picture is refused when the call is built."""
    world = World(tmp_path, behavior=ok_behavior)
    controller = world.controller()
    controller.start()
    line = {"line_id": "001", "spec": "x", "item_id": "i", "image": "p.jpg", "license": "CC BY-NC"}
    controller.log.append(
        "task_created",
        {"task_id": ids.task_id("photos", "1", {"001": "x"}), "source": "photos", "batch": "1", "variant": "", "route": "fake-batch",
         "purpose": "production", "effort": "low", "pool_version": "1", "dataset": "photos", "lines": [line]},
    )
    controller.step()
    failed = world.events_of("call_failed")
    assert len(failed) == 1 and "needs a source class" in failed[0].payload["error"] and failed[0].payload["retryable"] is False
    assert failed[0].payload["billed"] is False and world.provider.submit_count == 0


# ---------------------------------------------------------------------------- calls that cannot be built


def test_one_call_the_writer_cannot_build_fails_alone_and_the_others_go_on(tmp_path):
    class Picky(FakeWriter):
        def prepare(self, calls):
            if any(call.line == "002" for call in calls) and len(calls) > 1:
                raise ValueError("cannot build the second one")
            if calls[0].line == "002":
                raise ValueError("cannot build the second one")
            return super().prepare(calls)

    world = World(tmp_path, writer_class=Picky, behavior=ok_behavior)
    controller = world.controller()
    controller.add_task(task_spec(("001", "002", "003")))
    world.drive(controller, keep_all)
    failed = world.events_of("call_failed")
    assert len(failed) == 1 and failed[0].payload["call_id"].split(":")[1] == "002"
    assert failed[0].payload["kind"] == "bad_request" and failed[0].payload["retryable"] is False
    assert "cannot build the second one" in failed[0].payload["error"]
    assert len(world.shipped_rows()) == 2


# ---------------------------------------------------------------------------- checks before a run


def test_a_writer_that_sends_the_rules_to_a_model_refuses_to_start_without_them(tmp_path):
    class Real(FakeWriter):
        needs_rules = True

    world = World(tmp_path, writer_class=Real, behavior=ok_behavior)
    with pytest.raises(ConfigError, match="no rules are set"):
        world.controller().start()
    world.controller(rules=make_rules("sys json", {"type": "object"}, "v1")).start()


def test_the_writers_check_the_rules_once_before_the_run_starts(tmp_path):
    checked = []

    class Checking(FakeWriter):
        def check_rules(self, rules):
            checked.append(rules.version)
            raise ConfigError("this route cannot send these rules")

    world = World(tmp_path, writer_class=Checking, behavior=ok_behavior)
    with pytest.raises(ConfigError, match="cannot send these rules"):
        world.controller(rules=make_rules("sys", {"type": "object"}, "v7")).start()
    assert checked == ["v7"]


def test_a_switched_off_route_is_not_asked_about_the_rules(tmp_path):
    class Checking(FakeWriter):
        needs_rules = True

        def check_rules(self, rules):
            raise ConfigError("never asked")

    config = make_config(routes={"fake-batch": {"provider": "fake", "model": "fake-model", "kind": "batch", "enabled": False, "budget_usd": 1}})
    world = World(tmp_path, config=config, writer_class=Checking, behavior=ok_behavior)
    world.controller().start()


def test_the_rules_normalize_function_is_applied_once_per_answer(tmp_path):
    calls = []

    def normalize(text, spec, model):
        calls.append((text, spec, model))
        return json.dumps([{"id": "from-normalize", "thoughts": ["x"]}])

    rules = make_rules("sys", {"type": "object"}, "v1", normalize=normalize)
    world = World(tmp_path, behavior=lambda p: FakeOutcome(output="raw writer text"))
    controller = world.controller(rules=rules)
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    assert calls == [("raw writer text", "spec for 001", "fake-model")]
    assert [row["id"] for row in world.shipped_rows()] == ["from-normalize"]


def test_a_normalize_function_that_raises_makes_the_answer_unusable_and_not_a_crash(tmp_path):
    def normalize(text, spec, model):
        raise KeyError("rows")

    rules = make_rules("sys", {"type": "object"}, "v1", normalize=normalize)
    world = World(tmp_path, behavior=ok_behavior)
    controller = world.controller(rules=rules)
    controller.add_task(task_spec(("001",)))
    world.drive(controller, keep_all)
    failed = world.events_of("call_failed")
    assert failed and failed[0].payload["kind"] == "invalid_output" and "KeyError" in failed[0].payload["error"]


def test_calls_carry_the_rules_and_the_class_of_their_picture_to_the_writer(tmp_path):
    captured: list[CallSpec] = []

    class Spy(FakeWriter):
        def prepare(self, calls):
            captured.extend(calls)
            return super().prepare(calls)

    rules = make_rules("the system text", {"type": "object"}, "v9")
    world = World(tmp_path, writer_class=Spy, behavior=ok_behavior, polls_until_ended=0)
    controller = world.controller(rules=rules)
    controller.add_task(task_spec(("001",)))
    controller.step()
    [spec] = captured
    assert spec.rules is rules and spec.task_text == "spec for 001"
    assert fake_image_loader is not None
