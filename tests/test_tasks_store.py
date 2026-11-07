"""The tasks file in the state store: read through the store, a missing file means no tasks, the file can grow."""

from __future__ import annotations

import json

import pytest

from rps.config import ConfigError, tasks_store_path
from rps.controller import Controller
from rps.layout import operator_actor
from rps.providers import FakeOutcome, FakeWriter
from rps.runtime import build_operator_store
from rps.store import ActorStore, LocalStore, OwnershipError
from rps.store.base import Store
from rps.tasks import StorePlanner, parse_task_specs

from harness import World, keep_all
from helpers import make_config

PATH = "control/tasks.json"
GOOD = json.dumps({"id": "ok", "thoughts": ["fine"]})


def ok_behavior(payload):
    return FakeOutcome(output=GOOD)


def task(batch, lines=("001", "002"), route="fake-batch", **extra):
    entry = {
        "source": "photos", "batch": batch, "route": route,
        "lines": [{"line_id": line, "spec": f"spec for {line}", "item_id": f"img-{line}"} for line in lines],
    }
    entry.update(extra)
    return entry


def put(world, tasks, path=PATH):
    """The operator writes the tasks file into the state store, as someone outside the run would."""
    ActorStore(world.state_store, operator_actor()).write_text(path, tasks if isinstance(tasks, str) else json.dumps(tasks))


def store_world(tmp_path, **overrides):
    config = make_config(tasks_file="store:" + PATH, **overrides)
    return World(tmp_path, config=config, behavior=ok_behavior, polls_until_ended=0)


# ---------------------------------------------------------------------------- the configuration


def test_a_tasks_file_can_name_a_file_in_the_state_store():
    config = make_config(tasks_file="store:control/tasks.json")
    assert config.tasks_file == "store:control/tasks.json" and config.tasks_store_path() == "control/tasks.json"
    assert make_config(tasks_file="tasks.json").tasks_store_path() is None and make_config().tasks_store_path() is None
    assert tasks_store_path("store:a/b.json") == "a/b.json" and tasks_store_path(None) is None
    assert tasks_store_path("C:\\work\\tasks.json") is None


@pytest.mark.parametrize("bad", ["store:", "store:/control/tasks.json", "store:../tasks.json", "store:a//b.json", "store:a\\b.json"])
def test_a_tasks_file_in_the_store_needs_a_clean_relative_path(bad):
    with pytest.raises(ConfigError, match="tasks_file"):
        make_config(tasks_file=bad)


# ---------------------------------------------------------------------------- the planner


def test_the_planner_reads_the_file_through_the_store_every_time_it_is_asked(tmp_path):
    store = LocalStore(tmp_path / "state", fsync=False)
    planner = StorePlanner(store, PATH)
    assert planner(None) == []  # no file yet: no tasks
    ActorStore(store, operator_actor()).write_text(PATH, json.dumps([task("1")]))
    assert [t.batch for t in planner(None)] == ["1"]
    ActorStore(store, operator_actor()).write_text(PATH, json.dumps([task("1"), task("2", purpose="pricing")]))
    assert [(t.batch, t.purpose) for t in planner(None)] == [("1", "production"), ("2", "pricing")]
    store.delete(PATH)
    assert planner(None) == []  # a file taken away is no tasks again, not an error


def test_a_file_that_is_not_a_list_of_tasks_is_a_configuration_error_naming_the_file(tmp_path):
    store = LocalStore(tmp_path / "state", fsync=False)
    planner = StorePlanner(store, PATH)
    for text, message in [("{not json", "cannot read tasks file 'control/tasks.json' in the state store"), ("{}", "must hold a list of tasks"),
                          ('["x"]', "task 0 must be an object"), ('[{"source": "photos"}]', "'batch' is required")]:
        ActorStore(store, operator_actor()).write_text(PATH, text)
        with pytest.raises(ConfigError, match=message):
            planner(None)


def test_text_of_a_tasks_file_parses_the_same_everywhere():
    assert [t.batch for t in parse_task_specs(json.dumps([task("7")]), "a file")] == ["7"]
    with pytest.raises(ConfigError, match="cannot read the dropped file"):
        parse_task_specs("[", "the dropped file")


def test_the_planner_reads_at_most_every_interval_and_hands_back_what_it_read_last_in_between(tmp_path):
    reads = []

    class Counting(LocalStore):
        def read_bytes(self, path):
            reads.append(path)
            return super().read_bytes(path)

    store = Counting(tmp_path / "state", fsync=False)
    now = [100.0]
    planner = StorePlanner(store, PATH, min_interval_seconds=10.0, clock=lambda: now[0])
    ActorStore(store, operator_actor()).write_text(PATH, json.dumps([task("1")]))
    assert [t.batch for t in planner(None)] == ["1"] and len(reads) == 1
    ActorStore(store, operator_actor()).write_text(PATH, json.dumps([task("1"), task("2")]))
    now[0] += 5
    assert [t.batch for t in planner(None)] == ["1"] and len(reads) == 1  # too soon: the last answer
    now[0] += 6
    assert [t.batch for t in planner(None)] == ["1", "2"] and len(reads) == 2


# ---------------------------------------------------------------------------- in the controller


def test_with_no_file_there_are_no_tasks_and_the_run_ends_idle(tmp_path):
    world = store_world(tmp_path)
    controller = world.controller()
    summary = controller.run()
    assert summary.reason == "idle" and not summary.problems and not controller.state.tasks


def test_tasks_added_to_the_store_while_a_run_goes_on_are_picked_up_on_the_next_pass(tmp_path):
    world = store_world(tmp_path)
    controller = world.controller()
    controller.step()
    assert not controller.state.tasks
    put(world, [task("1")])
    controller.step()
    assert len(controller.state.tasks) == 1 and len(world.events_of("task_created")) == 1
    put(world, [task("1"), task("2", lines=("003", "004"))])  # the file grows; the first task is not recorded again
    controller.step()
    assert len(controller.state.tasks) == 2 and len(world.events_of("task_created")) == 2
    world.drive(controller, keep_all)
    assert len(world.shipped_rows()) == 4


def test_a_later_run_reads_the_same_file_and_ignores_the_tasks_it_already_knows(tmp_path):
    world = store_world(tmp_path)
    put(world, [task("1")])
    first = world.controller()
    world.drive(first, keep_all)
    assert len(world.shipped_rows()) == 2
    put(world, [task("1"), task("2", lines=("005",))])
    second = world.controller()
    world.drive(second, keep_all)
    assert len(world.events_of("task_created")) == 2 and len(world.shipped_rows()) == 3


def test_a_tasks_file_that_cannot_be_read_is_reported_once_and_the_run_goes_on(tmp_path):
    world = store_world(tmp_path)
    controller = world.controller()
    put(world, [task("1")])
    controller.step()
    put(world, "{this is not json")
    for _ in range(3):
        controller.step()  # the run is not stopped by a bad edit
    trouble = [line for line in world.printed if "the tasks could not be read" in line]
    assert len(trouble) == 1 and "cannot read tasks file 'control/tasks.json' in the state store" in trouble[0]
    assert any(problem.startswith("the tasks could not be read") for problem in controller.problems)
    assert len(controller.state.tasks) == 1  # what was recorded stays
    put(world, [task("1"), task("2", lines=("003",))])  # the file is fixed
    controller.step()
    assert len(controller.state.tasks) == 2


def test_a_task_that_cannot_be_added_does_not_keep_the_others_out(tmp_path):
    world = store_world(tmp_path)
    controller = world.controller()
    put(world, [task("1", route="no-such-route"), task("2")])
    controller.step()
    assert [t.batch for t in controller.state.tasks.values()] == ["2"]
    assert any("a task was not added" in problem and "unknown route 'no-such-route'" in problem for problem in controller.problems)


def test_a_missing_local_tasks_file_is_reported_and_does_not_stop_the_run(tmp_path):
    config = make_config(tasks_file=str(tmp_path / "missing" / "tasks.json"))
    world = World(tmp_path, config=config, behavior=ok_behavior)
    controller = world.controller()
    summary = controller.run()
    assert summary.reason == "idle" and any("the tasks could not be read" in problem for problem in summary.problems)


def test_the_controller_may_not_write_the_tasks_file_but_the_operator_may(tmp_path):
    world = store_world(tmp_path)
    controller = world.controller()
    with pytest.raises(OwnershipError):
        controller.state_store.write_text(PATH, "[]")
    operator = build_operator_store(make_config(work_dir=str(tmp_path / "work")))
    operator.write_text(PATH, json.dumps([task("1")]))
    operator.flush(force=True)
    assert json.loads(LocalStore(tmp_path / "work" / "state").read_text(PATH))[0]["batch"] == "1"


def test_the_file_is_read_on_every_pass_from_a_local_folder_and_at_most_every_fifteen_seconds_from_another_store(tmp_path):
    world = store_world(tmp_path)
    assert world.controller().planner.min_interval_seconds == 0.0

    class Remote(Store):
        """Anything that is not a local folder, standing for a store behind a network."""

        def __init__(self):
            self.inner = LocalStore(tmp_path / "remote", fsync=False)

        def read_bytes(self, path):
            return self.inner.read_bytes(path)

        def write_bytes(self, path, data):
            self.inner.write_bytes(path, data)

        def append_lines(self, path, lines):
            self.inner.append_lines(path, lines)

        def list(self, prefix=""):
            return self.inner.list(prefix)

        def delete(self, path):
            self.inner.delete(path)

        def exists(self, path):
            return self.inner.exists(path)

    config = world.config
    writers = {name: FakeWriter(name, route) for name, route in config.routes.items()}
    far = Controller(config, Remote(), writers, instance="20261107T091000Z-test")
    assert far.planner.min_interval_seconds == 15.0
    quick = Controller(config, Remote(), writers, instance="20261107T091100Z-test", tasks_check_seconds=2.5)
    assert quick.planner.min_interval_seconds == 2.5
    assert Controller(make_config(), Remote(), writers, instance="20261107T091200Z-test").planner is None
