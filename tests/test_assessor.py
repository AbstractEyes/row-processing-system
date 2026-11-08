"""The assessor: each check fires on a deliberately broken copy of a clean row, and stays quiet on the clean one."""

from __future__ import annotations

import copy

import pytest

from rps import assessor, rules
from rps.assess import AssessContext
from rps.assessor import SimilarityIndex, checks
from rps.rules import form

MODEL = "test-model"


def _example(name):
    example = next(e for e in rules.examples() if e["name"] == name)
    return example["item"], [form.to_row(r, example["item"], MODEL) for r in example["output"]["rows"]]


@pytest.fixture
def series():
    return _example("people_series")


@pytest.fixture
def seeded():
    item, rows = _example("seeded_single")
    return item, rows[0]


def _context(item, attempt=1, assignment=None):
    return AssessContext(
        task_id="t0123456789abcdef",
        source="s",
        batch="b",
        line="l1",
        attempt=attempt,
        route="r",
        pool_version="v",
        spec=item,
        item_id=None,
        assignment=assignment,
    )


ASSIGNED = {
    "relation_part": "rel",
    "unknown_keys": {"medium": "shot lens", "deep": "shot focus"},
    "wordings": {"simple": ["caption"], "medium": ["caption", "prompt", "tags"], "deep": ["caption", "prompt", "tags", "critique"]},
}


def codes(problems, severity=None):
    return {p.code for p in problems if severity is None or p.severity == severity}


def _deep(series):
    item, rows = series
    return item, copy.deepcopy(rows[2])


def test_clean_examples_have_no_errors():
    for example in rules.examples():
        item = example["item"]
        rows = [form.to_row(r, item, MODEL) for r in example["output"]["rows"]]
        for row in rows:
            assert not codes(assessor.check_row(row, item), "error"), example["name"]
        assert not assessor.check_series(rows, item)


def test_validator_rules_are_reported_by_number(series):
    item, row = _deep(series)
    row["thoughts"] = row["thoughts"] + row["thoughts"][:3]  # 13 thoughts
    found = codes(assessor.check_row(row, item))
    assert "rule_3" in found


def test_item_checks(series):
    item, row = _deep(series)
    row["id"] = "art-999999"
    assert "ids" in codes(assessor.check_row(row, item))
    item, row = _deep(series)
    row["source"]["license"] = "CC BY 4.0"
    assert "license" in codes(assessor.check_row(row, item))
    item, row = _deep(series)
    del row["spec"]["shoes"]
    assert "spec_kept" in codes(assessor.check_row(row, item))


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda r: r.update(genre="harbor portrait"), "genre_off_list"),
        (lambda r: r["thoughts"][0].update(text="The spec names a gray sweater."), "spec_language"),
        (lambda r: r["thoughts"][1].update(text="A painter would crop this tighter."), "advice"),
        (lambda r: r["thoughts"][2].update(text="This is an inference from the sweater."), "formula_word"),
        (lambda r: r["wordings"].pop("critique"), "wording_missing"),
        (lambda r: r["questions"][0].update(key="rel1 second"), "relation_key"),
        (lambda r: r["thoughts"][3].update(text="A baroque feel in the soft window light."), "style_lowercase"),
        (lambda r: r["wordings"].update(tags="1girl, solo, yellow sweater, striped sweater, blue skirt, cafe"), "garment_missing_tags"),
        (lambda r: r["wordings"].update(caption="A woman in a dress in a cafe."), "dress_caption"),
        (lambda r: r["wordings"].update(tags=r["wordings"]["tags"] + ", jeans"), "jeans_tag"),
        (lambda r: r["thoughts"][4].update(text="Count one: a woman at a table."), "label_opener"),
        (lambda r: r["spec"]["relation"][0].update(b="right side"), "relation_direction"),
    ],
)
def test_each_error_check_fires(series, mutate, code):
    item, row = _deep(series)
    mutate(row)
    assert code in codes(assessor.check_row(row, item), "error")


def test_garment_missing_from_the_text(series):
    item, row = _deep(series)
    for t in row["thoughts"]:
        t["text"] = t["text"].replace("pencil skirt", "garment")
    row["wordings"]["caption"] = row["wordings"]["caption"].replace("pencil skirt", "garment")
    assert "garment_missing_text" in codes(assessor.check_row(row, item), "error")


def test_flags(series, seeded):
    item, row = _deep(series)
    row["thoughts"][0]["text"] = "Robe: a long outer garment."
    row["thoughts"][5]["text"] = "Teal sits behind the cup in an orange glow."
    row["thoughts"][1]["text"] = row["thoughts"][1]["text"] + " A hat tops it."
    problems = assessor.check_row(row, item)
    assert {"gloss", "unrecorded_color", "unrecorded_garment"} <= codes(problems, "flag")
    assert not codes(problems, "error") & {"gloss", "unrecorded_color", "unrecorded_garment"}
    item, row = seeded
    row["thoughts"][0]["text"] = "Someone stops to look at the grasshopper on the glass."
    row["spec"]["form"]["medium"] = "oil painting"
    row["thoughts"][1]["text"] = " ".join(item["hint"].split()[:12])
    found = codes(assessor.check_row(row, item), "flag")
    assert {"passerby_present", "domain_medium", "hint_copy"} <= found


def test_conditional_passerby_is_quiet(seeded):
    item, row = seeded
    row["thoughts"][0]["text"] = "Someone might stop to look at the grasshopper."
    assert "passerby_present" not in codes(assessor.check_row(row, item))


def test_a_hint_that_implies_a_person_sets_the_item_aside(seeded):
    item, row = seeded
    item = dict(item, hint="A marble bust of a king on a plinth. " + item["hint"])
    result = assessor.Assessor()(row, _context(item))
    assert not result.passed and result.set_aside["class"] == "person"
    item = dict(item, hint="A green grasshopper near a face of the clock.")
    result = assessor.Assessor()(row, _context(item))
    assert result.passed is True or "person_in_hint" in result.flags


def test_assessor_gives_error_lines_and_flag_codes(series):
    item, row = _deep(series)
    row["wordings"]["caption"] = "A woman in a dress in a colour cafe."
    row["thoughts"][5]["text"] = "Teal is the only note."
    result = assessor.Assessor()(row, _context(item))
    assert result.passed is False
    assert any(line.startswith("dress_caption: ") for line in result.errors)
    assert not any("spelling" in line for line in result.errors)
    assert "unrecorded_color" in result.flags


def test_assess_call_gives_series_errors_to_every_row(series):
    item, rows = series
    rows = copy.deepcopy(rows)
    rows[1]["spec"]["look"]["hair"] = "long"  # the medium rank no longer nests in the deep rank
    results = assessor.Assessor().assess_call(rows, _context(item))
    assert len(results) == 3
    assert all(not r.passed for r in results)
    assert all(any(line.startswith("rule_7: ") for line in r.errors) for r in results)


def test_assess_call_passes_a_clean_series(series):
    item, rows = series
    results = assessor.Assessor().assess_call(copy.deepcopy(rows), _context(item))
    assert [r.passed for r in results] == [True, True, True]


def test_series_genre(series):
    item, rows = series
    rows = copy.deepcopy(rows)
    rows[1]["genre"] = "fashion"
    assert "series_genre" in codes(assessor.check_series(rows, item))


def test_near_duplicates(series):
    item, rows = series
    index = SimilarityIndex()
    index.add(rows[2])
    twin = copy.deepcopy(rows[2])
    twin["id"] = "art-900099"
    twin["thoughts"][0]["text"] += " Yes."
    assert codes(index.problems(twin)) == {"near_duplicate"}
    assert not index.problems(rows[1])
    assessor_with_index = assessor.Assessor(index=SimilarityIndex())
    first = assessor_with_index(rows[2], _context(item))
    again = assessor_with_index(twin, _context(item))
    assert "near_duplicate" not in first.flags and "near_duplicate" in again.flags


def test_report_counts_and_balance(series, seeded):
    item, rows = series
    rows = copy.deepcopy(rows)
    for row in rows:
        row["questions"] = [q for q in row["questions"] if not q["key"].startswith("rel")]
    rows[2]["questions"].append({"key": "rel1 b", "value": "table", "needs": "record"})
    rows[1]["thoughts"][0]["text"] = "The spec names a sweater."
    result = assessor.report(rows[:2], [item])
    assert result.stats["by_depth"] == {"simple": 1, "medium": 1}
    assert result.by_code["spec_language"] == 1
    assert result.by_code["missing"] == 1
    full = assessor.report(copy.deepcopy(series[1]), [item])
    assert full.stats["relation_b_share"] == 0.0
    forced = copy.deepcopy(series[1])
    forced[2]["questions"] = [{"key": "rel1 b", "value": "table", "needs": "record"}] + [
        q for q in forced[2]["questions"] if not q["key"].startswith("rel")
    ]
    assert assessor.report(forced, [item]).stats["relation_b_share"] == 1.0
    assert "relation_key" in assessor.report(forced, [item]).by_code
    data = result.to_dict()
    assert data["rows"] == 2 and data["by_code"] == result.by_code
    assert "2 rows" in result.summary()


def test_a_clean_series_meets_its_assignment(series):
    item, rows = series
    results = assessor.Assessor().assess_call(copy.deepcopy(rows), _context(item, assignment=ASSIGNED))
    assert [r.passed for r in results] == [True, True, True]


def test_no_assignment_makes_no_assignment_checks(series):
    item, row = _deep(series)
    row["questions"][0]["key"] = "rel1 b"
    assert not any(c.startswith("assigned_") for c in codes(checks.assignment_check(row, None)))
    result = assessor.Assessor()(row, _context(item, assignment=None))
    assert not any(line.startswith("assigned_") for line in result.errors)


def test_assigned_relation_part_is_an_error(series):
    item, row = _deep(series)
    row["questions"][0]["key"] = "rel1 b"
    result = assessor.Assessor()(row, _context(item, assignment=ASSIGNED))
    assert not result.passed and any(line.startswith("assigned_relation_part: ") for line in result.errors)


def test_assigned_unknown_key_checks(series):
    item, row = _deep(series)
    row["questions"][-1]["key"] = "shot angle"
    found = codes(checks.assignment_check(row, ASSIGNED), "error")
    assert found == {"assigned_unknown_key"}
    item, row = _deep(series)
    row["questions"] = [q for q in row["questions"] if q["needs"] != "unknown"]
    assert codes(checks.assignment_check(row, ASSIGNED), "flag") == {"assigned_unknown_missing"}
    item, row = _deep(series)
    row["spec"]["shot"]["focus"] = "soft"
    assert "assigned_unknown_held" in codes(checks.assignment_check(row, ASSIGNED), "error")


def test_assigned_wordings(series):
    item, row = _deep(series)
    del row["wordings"]["critique"]
    assert codes(checks.assignment_check(row, ASSIGNED), "error") == {"assigned_wordings"}
    item, simple = _example("people_series")[0], copy.deepcopy(_example("people_series")[1][0])
    simple["wordings"]["prompt"] = "a woman at a table"
    assert codes(checks.assignment_check(simple, ASSIGNED), "flag") == {"assigned_wordings_extra"}


def test_a_failed_rank_fails_its_siblings(series):
    item, rows = series
    rows = copy.deepcopy(rows)
    rows[1]["wordings"]["caption"] = "A woman in a dress in a cafe."
    results = assessor.Assessor().assess_call(rows, _context(item))
    assert [r.passed for r in results] == [False, False, False]
    for i in (0, 2):
        assert any(line.startswith(f"series_sibling: rank 2 (row {rows[1]['id']}) failed: dress_caption") for line in results[i].errors)
    assert not any(line.startswith("series_sibling") for line in results[1].errors)


def test_a_single_row_call_has_no_sibling_errors(series):
    item, rows = series
    row = copy.deepcopy(rows[1])
    row["wordings"]["caption"] = "A woman in a dress in a cafe."
    result = assessor.Assessor().assess_call([row], _context(item))
    assert not result.passed if not isinstance(result, list) else not result[0].passed
    assert not any(line.startswith("series_sibling") for line in (result[0] if isinstance(result, list) else result).errors)


def test_an_item_set_aside_sets_aside_every_rank(seeded):
    item, row = seeded
    item = dict(item, hint="A marble bust of a king on a plinth. " + item["hint"])
    twin = copy.deepcopy(row)
    twin["id"] = "art-900098"
    results = assessor.Assessor().assess_call([row, twin], _context(item))
    assert len(results) == 2
    assert all(r.set_aside and r.set_aside["class"] == "person" for r in results)
