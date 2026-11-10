"""closed_tasks: the controller prepares no new call for a closed task, and everything already under way goes on."""

from __future__ import annotations

from harness import World, task_spec
from helpers import RecordingWriter, make_config


def lines_called(world, controller):
    return {controller.state.calls[c].line for c in world.provider.submitted_calls}


def test_a_closed_tasks_lines_get_no_calls_while_another_tasks_lines_do_and_the_run_goes_idle(tmp_path):
    closed = task_spec(("001", "002", "003"), batch="closed")
    world = World(tmp_path, config=make_config(closed_tasks=[closed.task_id]), writer_class=RecordingWriter)
    controller = world.controller()
    assert controller.add_task(closed) == closed.task_id
    controller.add_task(task_spec(("101", "102"), batch="open"))
    for _ in range(4):  # several passes
        controller.step()
        assert {call.line for call in controller.writers["fake-batch"].calls} <= {"101", "102"}
    world.drive(controller)
    assert controller.is_idle()  # the closed lines are not work to do
    assert lines_called(world, controller) == {"101", "102"}
    assert {row["row_id"].split("_")[1] for row in world.shipped_rows()} == {"101", "102"}
    progress = controller.status()["progress"]
    assert progress["lines_closed"] == 3 and progress["lines_new"] == 3
    assert "closed 3 lines" in controller.format_status()
    assert "closed 3 lines" in controller._progress_line(1.0, None)
    assert not any(event.kind == "closed" for event in world.events())  # nothing about it is recorded as an event


def test_a_call_in_flight_when_the_task_is_closed_is_collected_and_its_rows_go_to_review(tmp_path):
    spec = task_spec(("001", "002"), batch="late")
    world = World(tmp_path, polls_until_ended=3)
    first = world.controller()
    first.add_task(spec)
    first.step()  # the calls are prepared and sent
    assert first.state.submitted_calls()
    world.config = make_config(closed_tasks=[spec.task_id])
    second = world.controller()
    world.drive(second)
    assert second.is_idle()
    assert len(world.shipped_rows()) == 2  # collected, reviewed, shipped as always


def test_removing_the_id_reopens_the_task_and_its_lines_are_prepared_again(tmp_path):
    spec = task_spec(("001", "002"), batch="again")
    world = World(tmp_path, config=make_config(closed_tasks=[spec.task_id]), writer_class=RecordingWriter)
    closed = world.controller()
    closed.add_task(spec)
    for _ in range(3):
        closed.step()
    assert closed.writers["fake-batch"].calls == [] and closed.is_idle()
    closed.set_closed_tasks(())  # the same controller, the id taken out of the list
    assert not closed.is_idle()
    world.drive(closed)
    assert len(world.shipped_rows()) == 2
    # a later run built from a configuration without the id does the same
    other = World(tmp_path / "other", config=make_config(), writer_class=RecordingWriter)
    reopened = other.controller()
    reopened.add_task(spec)
    other.drive(reopened)
    assert len(other.shipped_rows()) == 2


def test_an_id_that_names_no_known_task_is_reported_once_and_otherwise_ignored(tmp_path):
    world = World(tmp_path, config=make_config(closed_tasks=["t-nothing"]), writer_class=RecordingWriter)
    controller = world.controller()
    controller.add_task(task_spec(("001",), batch="x"))
    world.drive(controller)
    for _ in range(3):
        controller.step()
    text = "closed_tasks: 't-nothing' names no known task"
    assert controller.problems.count(text) == 1
    assert sum(text in line for line in world.printed) == 1
    assert len(world.shipped_rows()) == 1  # the other task went through
