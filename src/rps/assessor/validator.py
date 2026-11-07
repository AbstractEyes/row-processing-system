"""The row validator, called as a library.

The dataset's own validator (the `thought_seeds` package, pinned in requirements) checks one
row against the rules and a series for nesting. This module adds what a curator checks on
top, the same way the package's batch checker does: the id belongs to the item, the license
and source follow the item, every record of the given spec is kept, and capitalized words
that might be real names are listed for a reader.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from thought_seeds import rows as _rows
from thought_seeds.core import HELD, tp

from .problems import ERROR, FLAG, Problem, item_ids

PERMISSIVE_ROW = _rows.PERMISSIVE_ROW


def _base_spec(row: Mapping[str, Any], item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The spec the row must keep: the item's single spec, the seed's deep spec, or the drawn spec of its depth."""
    if item.get("kind") == "single":
        return item.get("spec")
    if item.get("kind") != "series":
        return None
    specs = item.get("specs") or {}
    if item.get("seed_id"):
        return specs.get("deep") if row.get("depth") == "deep" else None
    return specs.get(row.get("depth"))


def validate_row(row: Mapping[str, Any]) -> list[Problem]:
    """The validator's rules on one row; each problem's code is `rule_N`."""
    row_id = str(row.get("id", "?"))
    return [Problem(f"rule_{rule}", row_id, message) for rule, message in tp.validate(dict(row), HELD)]


def check_against_item(row: Mapping[str, Any], item: Mapping[str, Any] | None) -> list[Problem]:
    """What the batch checker adds for a row whose spec item is known."""
    row_id = str(row.get("id", "?"))
    out: list[Problem] = []
    if not item:
        return out
    allotted = item_ids(item)
    if allotted and row_id not in allotted:
        out.append(Problem("ids", row_id, f"the id is not one of the ids allotted to this item: {list(allotted)}"))
    if allotted and item.get("series") and row.get("series") not in (None, item["series"]):
        out.append(Problem("series", row_id, f"the series id should be {item['series']!r}"))
    want = item.get("license", PERMISSIVE_ROW)
    source = row.get("source") or {}
    if source.get("license") != want:
        out.append(Problem("license", row_id, f"the row's license should be {want!r}"))
    base = _base_spec(row, item)
    if base is not None:
        have = set(tp.records(row.get("spec") or {}))
        lost = [kv for kv in tp.records(base) if kv not in have]
        if lost:
            out.append(Problem("spec_kept", row_id, f"records of the given spec missing or changed: {lost[:4]}"))
    if item.get("seed_id") and source.get("seed_id") != item["seed_id"]:
        out.append(Problem("source", row_id, f"a seeded row names its seed: seed_id {item['seed_id']!r}, kind 'seeded'"))
    return out


def name_warnings(row: Mapping[str, Any]) -> list[Problem]:
    """Capitalized words inside sentences that might be a real person's name, for a reader to confirm."""
    names = _rows.possible_names(dict(row))
    if not names:
        return []
    return [Problem("names", str(row.get("id", "?")), f"capitalized words that might be names: {names}", FLAG)]


def check_series(rows: Sequence[Mapping[str, Any]], item: Mapping[str, Any] | None = None) -> list[Problem]:
    """Ranks 1-3 are simple, medium, deep and each spec nests in the next."""
    if len(rows) < 2:
        return []
    sid = str(rows[0].get("series", "?"))
    out: list[Problem] = []
    depths = sorted((int(r.get("rank", 0)), r.get("depth")) for r in rows)
    if depths != [(1, "simple"), (2, "medium"), (3, "deep")]:
        out.append(Problem("series", sid, f"a series holds ranks 1-3 = simple, medium, deep; it holds {depths}"))
    out.extend(Problem("rule_7", sid, message) for _, message in tp.validate_series([dict(r) for r in rows]))
    return out


def missing_ids(rows: Sequence[Mapping[str, Any]], items: Sequence[Mapping[str, Any]]) -> list[Problem]:
    """Allotted ids that no row carries (an expected result for an item a writer skipped)."""
    present = {r.get("id") for r in rows}
    allotted = [i for item in items for i in item_ids(item)]
    gone = [i for i in allotted if i not in present]
    if not gone:
        return []
    return [Problem("missing", "batch", f"{len(gone)} allotted ids have no row: {gone[:6]}", ERROR)]
