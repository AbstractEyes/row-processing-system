"""American spelling as a step after writing: British spelling is corrected, never refused."""

from __future__ import annotations

import copy

import pytest

from rps import assessor
from rps.assess import AssessContext
from rps.assessor.checks import british_words
from rps.rules import spelling

CASES = {
    "A grey harbour at the centre of the frame.": "A gray harbor at the center of the frame.",
    "Colourful, coloured and Colour patches.": "Colorful, colored and Color patches.",
    "The favourite jewellery of a neighbour.": "The favorite jewelry of a neighbor.",
    "The theatre catalogue lists metres of fibre.": "The theater catalog lists meters of fiber.",
    "A cosy pyjamas scene, centred and travelling.": "A cozy pajamas scene, centered and traveling.",
    "They organise, realise and recognise it; a greyhound waits.": "They organize, realize and recognize it; a greyhound waits.",
    "GREY armour": "GRAY armor",
    "Realism and a four-hour hour stay as they are.": "Realism and a four-hour hour stay as they are.",
}


@pytest.mark.parametrize("before, after", CASES.items())
def test_americanize_text(before, after):
    assert spelling.americanize_text(before) == after


def _row():
    return {
        "id": "art-900010",
        "depth": "medium",
        "spec": {"wear": {"colour": "grey"}, "scene": {"setting": "harbour"}},
        "thoughts": [{"step": "see", "text": "A grey coat by the harbour."}],
        "wordings": {"caption": "A woman in a grey coat.", "tags": "grey coat, harbour"},
        "questions": [
            {"key": "wear colour", "value": "grey", "needs": "record"},
            {"key": "form palette", "value": "grey and blue", "needs": "chain"},
            {"key": "shot lens", "value": "unknown", "needs": "unknown"},
        ],
    }


def test_americanize_row_leaves_the_records_as_given():
    row = _row()
    before = copy.deepcopy(row)
    out = spelling.americanize_row(row)
    assert row == before  # a pure function: the input is untouched
    assert out["spec"] == before["spec"]
    assert out["thoughts"][0]["text"] == "A gray coat by the harbor."
    assert out["wordings"] == {"caption": "A woman in a gray coat.", "tags": "gray coat, harbor"}
    assert out["questions"][0]["value"] == "grey"  # a record question copies the record
    assert out["questions"][1]["value"] == "gray and blue"
    assert not british_words(out)


def test_british_spelling_never_rejects_or_flags_a_row():
    row = _row()
    assert british_words(row) == ["grey", "harbour"]
    assert not [p for p in assessor.check_row(row) if "spelling" in p.code]
    context = AssessContext(task_id="t0123456789abcdef", source="s", batch="b", line="l1", attempt=1, route="r", pool_version="v", spec=None, item_id=None)
    result = assessor.Assessor()(row, context)
    assert not any("spelling" in line for line in result.errors) and "us_spelling" not in result.flags


def test_the_report_counts_british_rows_as_a_statistic():
    result = assessor.report([_row(), spelling.americanize_row(_row())])
    assert result.stats["british_spelling_rows"] == 1
    assert "us_spelling" not in result.by_code
