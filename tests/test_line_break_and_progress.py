"""A line break inside a field is refused (`line_break`), and the progress line's estimate counts the lines done in this
run (0.6.3)."""

from __future__ import annotations

import copy

import pytest

from rps import assessor, rules
from rps.assessor.problems import ERROR
from rps.controller import format_progress

from harness import World, task_spec
from test_shown_text import LINE_BREAK_RULE, PROMPTS, TEMPLATES, _medium, _pictured

CR, LF = chr(13), chr(10)


def line_breaks(row, item):
    return [p for p in assessor.check_row(row, item) if p.code == "line_break"]


@pytest.fixture(scope="module")
def plain():
    item, row = _medium()
    return row, item


def broken(row, *, thought=None, wording=None, text=None):
    row = copy.deepcopy(row)
    if thought is not None:
        row["thoughts"][thought] = {**row["thoughts"][thought], "text": text}
    if wording is not None:
        row["wordings"][wording] = text
    return row


# ---------------------------------------------------------------------------- the writer's rule


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_every_writer_prompt_and_template_asks_for_one_line_in_its_general_rules(name):
    for path in (PROMPTS[name], TEMPLATES[name]):
        text = path.read_text(encoding="utf-8")
        assert text.count(LINE_BREAK_RULE) == 1, path
        picture = text[text.index("Text is among the things you read from the picture") :]
        picture = picture[: picture.index(chr(10))]
        assert LINE_BREAK_RULE not in picture  # the sentence is in the rules every writer reads, not only the picture's


def test_the_pool_lists_the_rule():
    (line,) = [line for line in rules.pool().splitlines() if line.startswith("| 119 |")]
    assert "| WRITER |" in line and "line_break" in line and '" / "' in line


# ---------------------------------------------------------------------------- the checker


def test_a_row_without_line_breaks_is_not_refused(plain):
    row, item = plain
    assert line_breaks(row, item) == []
    assert line_breaks(_pictured(row), item) == []


@pytest.mark.parametrize("mark", [LF, CR, CR + LF])
@pytest.mark.parametrize("picture", [False, True], ids=["text row", "picture row"])
def test_a_line_break_inside_a_thought_is_an_error_on_every_row(plain, mark, picture):
    row, item = plain
    bad = broken(row, thought=0, text="a pole" + mark + "stands left")
    bad = _pictured(bad) if picture else bad
    (problem,) = line_breaks(bad, item)
    assert problem.severity == ERROR and problem.line().startswith("line_break: ")
    assert "one line" in problem.message and '" / "' in problem.message and "sign" in problem.message


@pytest.mark.parametrize("kind", ["caption", "prompt", "tags", "critique"])
@pytest.mark.parametrize("picture", [False, True], ids=["text row", "picture row"])
def test_a_line_break_inside_any_wording_is_an_error_on_every_row(plain, kind, picture):
    row, item = plain
    assert kind in row["wordings"] or kind == "critique"
    bad = broken(row, wording=kind, text="first" + LF + "second")
    bad = _pictured(bad) if picture else bad
    (problem,) = line_breaks(bad, item)
    assert problem.severity == ERROR


def test_a_line_break_inside_a_wording_that_holds_a_list_is_found(plain):
    row, item = plain
    bad = copy.deepcopy(row)
    bad["wordings"]["tags"] = ["a", "b" + LF + "c"]
    assert len(line_breaks(bad, item)) == 1


def test_the_error_stops_the_row_like_any_error(plain):
    row, item = plain
    bad = broken(row, thought=0, text="one" + LF + "two")
    errors = [p for p in assessor.check_row(bad, item) if p.severity == ERROR]
    assert "line_break" in {p.code for p in errors}


def test_a_picture_row_that_writes_the_lines_of_a_sign_with_slashes_is_not_refused(plain):
    row, item = plain
    good = copy.deepcopy(row)
    good["thoughts"].append({"step": "say", "text": 'The sign reads "20 / MPH".'})
    assert line_breaks(_pictured(good), item) == []


# ---------------------------------------------------------------------------- the progress line's estimate


def estimate(**kw):
    line = format_progress(rows=0, budget=7200, **kw)
    return [part for part in line.split(" | ") if part.startswith("est.")]


def test_the_estimate_counts_the_lines_done_in_this_run_only():
    # 48 of 120 lines are done and 40 of them were done before this run started: this run did 8 in 730 seconds, so the
    # 72 lines left take 730 * 72 / 8 = 6570 seconds (counting all 48 would give 1095)
    assert estimate(lines_total=120, lines_done=48, elapsed=730, lines_done_at_start=40) == ["est. 01:49:30 to finish"]
    assert estimate(lines_total=120, lines_done=48, elapsed=730) == ["est. 00:18:15 to finish"]  # nothing done before: as it was


def test_the_estimate_is_left_out_when_this_run_has_finished_no_line():
    for lines_done_at_start in (0, 40):
        assert estimate(lines_total=120, lines_done=lines_done_at_start, elapsed=730, lines_done_at_start=lines_done_at_start) == []
    line = format_progress(lines_total=120, lines_done=40, rows=3, elapsed=60, budget=None, lines_done_at_start=40)
    assert "done 40 (33%)" in line and "time spent 00:01:00" in line and "est." not in line


def test_the_estimate_is_left_out_when_every_line_is_done_and_a_status_print_has_no_clock():
    assert estimate(lines_total=120, lines_done=120, elapsed=900, lines_done_at_start=40) == []
    assert estimate(lines_total=120, lines_done=120, elapsed=900) == []
    status = format_progress(lines_total=4, lines_done=2, rows=3, elapsed=None, budget=None, lines_done_at_start=1)
    assert status == "job 4 lines | done 2 (50%) | rows 3"


def test_the_controller_counts_from_the_lines_done_when_it_started(tmp_path):
    world = World(tmp_path)
    first = world.controller()
    first.add_task(task_spec(("001", "002", "003")))
    world.drive(first)
    done = first.state.job_progress(first.config.max_attempts)["lines_done"]
    assert done == 3
    second = world.controller()
    second.start()
    assert first._lines_done_at_start == 0 and second._lines_done_at_start == 3
    second.add_task(task_spec(("004", "005"), batch="2"))
    line = second._progress_line(30.0, None)
    assert "done 3 (60%)" in line and "est." not in line  # this run has done none yet
    world.drive(second)
    assert "est." not in second._progress_line(30.0, None)  # all done
