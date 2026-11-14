"""The assessor: checks a written row before a reviewer sees it.

    assessor = Assessor(index=SimilarityIndex())     # an AssessFn for the controller
    assessor(row, context)                           # one row -> Assessment
    assessor.assess_call(rows, context)              # all rows of one call -> one Assessment each
    report(rows, items)                              # batch statistics and every finding

A finding is an error (the writer must fix it; the error line goes back on the one retry),
a flag (a reviewer should look), or a set-aside (an item that should not be written, such as
a real-picture item whose hint implies a person the records leave out).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..assess import AssessContext, Assessment
from . import checks, invention_checks, reading_checks, review_checks, shown_text, validator
from .seed_checks import screen_item
from .problems import ERROR, FLAG, Problem, errors, flags
from .report import Report, balance_problems, row_stats
from .similarity import SimilarityIndex

__all__ = ["Assessor", "Problem", "Report", "SimilarityIndex", "check_row", "check_series", "report", "screen_item"]


def _item(spec: Any) -> Mapping[str, Any] | None:
    return spec if isinstance(spec, Mapping) else None


# Findings raised as flags by their check that stop a row: each names the word to remove, and each
# had almost no hits on the released rows (see tools/parity.py).
ERROR_WHEN_RAISED = frozenset({"unrecorded_pose", "state_conflict", "unrecorded_detail", "unrecorded_shot", "mood_cause_differs", "caption_hint_list", "relation_side", "position_as_place", "unknown_stated", "pasted_value"})


# Findings that say "the text holds a detail no record and no hint gives". A writer that was shown the picture may state
# what the picture shows, so on a picture row each of these is a flag that points at the picture, never an error.
PICTURE_DETAIL_CODES = frozenset({
    "unrecorded_pose", "unrecorded_detail", "unrecorded_shot", "unrecorded_light", "unrecorded_thing", "unrecorded_size",
    "unrecorded_material", "unrecorded_color", "unrecorded_garment", "state_conflict", "added_record", "relation_placement",
})
PICTURE_NOTE = "; look for it in the picture"

# Findings that say "this sentence says what a hint sentence says". On a picture row the hint describes the very picture
# the writer was shown, so a sentence that is true of the picture will often agree with it: each of these only flags the
# sentence, for a reader to compare with the hint. (Measured on rows written from pictures whose hint was a full caption:
# these checks stopped two rows in three, most of them sentences in the writer's own words.)
PICTURE_HINT_CODES = frozenset({"thought_hint_copy", "caption_hint_copy", "caption_hint_list"})
PICTURE_HINT_NOTE = "; the hint describes this same picture, so compare the sentence with the hint: a fault only when it is taken over from the hint"

# A picture row quotes the text its picture shows (`shown_text`): a thought with the step "say" gives the words, and the
# caption and the prompt quote them. That shown text is the picture's, not the row's prose, so a check reads the row
# without it (`shown_text.own_prose`): what a sign says claims no detail, no mood and no name. Any other quotation is
# read as the row's own words. Sizes, counts, questions and tags are the validator's, read from the row as written.
# Two kinds of check read the shown text too.
ROW_CHECK_GROUPS = (checks.ROW_CHECKS, review_checks.REVIEW_CHECKS, invention_checks.INVENTION_CHECKS, reading_checks.READING_CHECKS)
# The check for fit and shape words about a minor reads every word of the row: nothing is kept from it.
AS_WRITTEN_CHECKS = frozenset({review_checks.minor_fit_check})
# A check that compares the row with its hint counts a shown word as the row's own, except the words the hint quotes
# too: those are left out of the row's shown text. The row is then compared with the hint as written, and with the hint
# without those words in its quotations, and a finding of either counts (`shown_text.beside_hint`): a hint sentence
# is caught whether the row copies it with the quoted words outside quote marks, around them, or inside quote marks.
HINT_COPY_CHECKS = frozenset({
    checks.hint_copy_check, review_checks.hint_paraphrase_check, review_checks.caption_hint_list_check, invention_checks.hint_name_check,
    invention_checks.caption_hint_copy_check, invention_checks.hint_order_check, invention_checks.thought_hint_copy_check,
})


def is_picture_row(row: Mapping[str, Any]) -> bool:
    """True for a row whose writer call carried the picture: its writer label holds the picture's size."""
    writer = row.get("writer")
    size = writer.get("image_size") if isinstance(writer, Mapping) else None
    return isinstance(size, int) and not isinstance(size, bool) and size > 0


def _for_picture(found: list[Problem]) -> list[Problem]:
    out = []
    for p in found:
        if p.code in PICTURE_DETAIL_CODES:
            out.append(Problem(p.code, p.row_id, p.message + PICTURE_NOTE, FLAG))
        elif p.code in PICTURE_HINT_CODES:
            out.append(Problem(p.code, p.row_id, p.message + PICTURE_HINT_NOTE, FLAG))
        else:
            out.append(p)
    return out


def check_row(row: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> list[Problem]:
    """Every finding about one row: the validator, the item checks, the row checks.

    On a picture row (see `is_picture_row`) the checks for a detail no record gives, and the checks for a sentence
    that says what a hint sentence says, only flag it; and the text the row quotes from its picture is not read as its
    prose (see `ROW_CHECK_GROUPS`). A text row is read whole by every check, quoted words and all.
    """
    picture = is_picture_row(row)
    prose = shown_text.own_prose(row) if picture else row
    hint_row, hint_item = shown_text.beside_hint(row, item) if picture else (row, item)

    def run(check: Any) -> list[Problem]:
        if check not in HINT_COPY_CHECKS:
            return check(row if check in AS_WRITTEN_CHECKS else prose, item)
        found = check(hint_row, item)
        if hint_item is not item:  # the hint quotes words the row shows: look for a run copied around them as well
            seen = {p.code for p in found}
            found = [*found, *(p for p in check(hint_row, hint_item) if p.code not in seen)]
        return found

    found = [
        *validator.validate_row(row, prose),
        *validator.check_against_item(row, item),
        *validator.name_warnings(prose),
        *[p for group in ROW_CHECK_GROUPS for check in group for p in run(check)],
    ]
    if picture:
        found = _for_picture(found)
    flag_only = PICTURE_DETAIL_CODES | PICTURE_HINT_CODES if picture else frozenset()
    return [Problem(p.code, p.row_id, p.message, ERROR) if p.code in ERROR_WHEN_RAISED and p.code not in flag_only else p for p in found]


def check_series(rows: Sequence[Mapping[str, Any]], item: Mapping[str, Any] | None = None) -> list[Problem]:
    """Findings that need all the ranks of a series at once."""
    return [*validator.check_series(rows, item), *checks.series_genre_check(rows)]


# Errors that belong to the item, not the writer: the item is set aside under this skip class.
SET_ASIDE_CLASS = {"person_in_hint": "person", "hint_contradicts_record": "contradiction"}


def _assessment(problems: Sequence[Problem]) -> Assessment:
    aside = [p for p in problems if p.code in SET_ASIDE_CLASS and p.severity == ERROR]
    if aside:
        return Assessment(passed=False, set_aside={"class": SET_ASIDE_CLASS[aside[0].code], "reason": aside[0].message})
    errs = errors(problems)
    codes = tuple(dict.fromkeys(p.code for p in flags(problems)))
    return Assessment(passed=not errs, flags=codes, errors=tuple(p.line() for p in errs))


class Assessor:
    """An `AssessFn` for the controller, with an optional near-duplicate index."""

    def __init__(self, index: SimilarityIndex | None = None, remember: bool = True) -> None:
        self.index = index
        self.remember = remember

    def _row_problems(self, row: Mapping[str, Any], item: Mapping[str, Any] | None, assignment: Mapping[str, Any] | None = None) -> list[Problem]:
        problems = [*check_row(row, item), *checks.assignment_check(row, assignment)]
        if self.index is not None:
            problems.extend(self.index.problems(row))
        return problems

    def _remember(self, row: Mapping[str, Any], assessment: Assessment) -> None:
        if self.index is not None and self.remember and assessment.passed:
            self.index.add(row)

    def __call__(self, row: Mapping[str, Any], context: AssessContext) -> Assessment:
        assessment = _assessment(self._row_problems(row, _item(context.spec), context.assignment))
        self._remember(row, assessment)
        return assessment

    def assess_call(self, rows: Sequence[Mapping[str, Any]], context: AssessContext) -> list[Assessment]:
        """Assess all rows of one call.

        A series-level error is given to every row. The controller accepts a line when any
        of its rows is accepted, so when one rank of a multi-row call fails, every other
        row gets an error naming it and the whole series is rewritten in its one rebound.
        A row set aside for a reason that belongs to the item sets aside every rank.
        """
        item = _item(context.spec)
        per_row = [self._row_problems(row, item, context.assignment) for row in rows]
        series = check_series(rows, item) if len(rows) > 1 else []
        results = [_assessment([*problems, *series]) for problems in per_row]
        if len(rows) > 1:
            results = _tie_ranks(rows, per_row, results)
        if all(a.passed for a in results):
            for row, assessment in zip(rows, results):
                self._remember(row, assessment)
        return results


RANKS = {"simple": 1, "medium": 2, "deep": 3}


def _rank(row: Mapping[str, Any]) -> Any:
    return row.get("rank") or RANKS.get(str(row.get("depth")), "?")


def _tie_ranks(rows: Sequence[Mapping[str, Any]], per_row: Sequence[Sequence[Problem]], results: list[Assessment]) -> list[Assessment]:
    aside = next((r.set_aside for r in results if r.set_aside), None)
    if aside:
        return [Assessment(passed=False, set_aside=dict(aside)) for _ in results]
    failed = [i for i, p in enumerate(per_row) if errors(p)]
    if not failed:
        return results
    tied = list(results)
    for i in range(len(rows)):
        others = [j for j in failed if j != i]
        if not others:
            continue
        lines = tuple(
            f"series_sibling: rank {_rank(rows[j])} (row {rows[j].get('id', '?')}) failed: "
            + ", ".join(dict.fromkeys(p.code for p in errors(per_row[j])))
            for j in others
        )
        base = results[i]
        tied[i] = Assessment(passed=False, flags=base.flags, errors=(*base.errors, *lines))
    return tied


def report(
    rows: Sequence[Mapping[str, Any]],
    items: Sequence[Mapping[str, Any]] = (),
    index: SimilarityIndex | None = None,
) -> Report:
    """Every finding for a batch of rows and the batch statistics.

    `items` are the batch's spec items (the lines of its specs file); rows are matched to
    their item by id. `index` may hold the accepted set; the batch's own rows are compared
    among themselves either way.
    """
    by_id = {i: item for item in items for i in item.get("ids", ())}
    own = index if index is not None else SimilarityIndex()
    problems: list[Problem] = []
    for row in rows:
        item = by_id.get(row.get("id"))
        problems.extend(check_row(row, item))
        problems.extend(own.problems(row))
        own.add(row)
    series: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        series.setdefault(str(row.get("series")), []).append(row)
    for members in series.values():
        item = by_id.get(members[0].get("id"))
        if len(members) > 1 and (item is None or item.get("kind") == "series"):
            problems.extend(check_series(members, item))
    if items:
        problems.extend(validator.missing_ids(rows, items))
    stats = row_stats(rows)
    problems.extend(balance_problems(stats))
    return Report(rows=len(rows), problems=problems, stats=stats)
