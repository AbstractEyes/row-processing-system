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
from . import checks, validator
from .problems import ERROR, Problem, errors, flags
from .report import Report, balance_problems, row_stats
from .similarity import SimilarityIndex

__all__ = ["Assessor", "Problem", "Report", "SimilarityIndex", "check_row", "check_series", "report"]


def _item(spec: Any) -> Mapping[str, Any] | None:
    return spec if isinstance(spec, Mapping) else None


def check_row(row: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> list[Problem]:
    """Every finding about one row: the validator, the item checks, the row checks."""
    return [
        *validator.validate_row(row),
        *validator.check_against_item(row, item),
        *validator.name_warnings(row),
        *checks.run_row_checks(row, item),
    ]


def check_series(rows: Sequence[Mapping[str, Any]], item: Mapping[str, Any] | None = None) -> list[Problem]:
    """Findings that need all the ranks of a series at once."""
    return [*validator.check_series(rows, item), *checks.series_genre_check(rows)]


def _assessment(problems: Sequence[Problem]) -> Assessment:
    aside = [p for p in problems if p.code == "person_in_hint" and p.severity == ERROR]
    if aside:
        return Assessment(passed=False, set_aside={"class": "person", "reason": aside[0].message})
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
