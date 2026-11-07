"""Batch-level statistics and the report of a set of rows."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .problems import ERROR, FLAG, Problem

RELATION_B_SHARE_MAX = 0.5  # the second thing may be asked about on at most half of a batch's relation questions
UNKNOWN_SHARE_MAX = 0.25  # no unknown key on more than about a fifth of a batch's unknown questions (some slack)
HEDGES = ("suggests", "reads as", "likely", "points to", "would mean", "as if", "perhaps", "probably", "fits", "implies", "must be")


@dataclass
class Report:
    rows: int
    problems: list[Problem] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def by_code(self) -> dict[str, int]:
        return dict(Counter(p.code for p in self.problems))

    @property
    def rows_with_errors(self) -> int:
        return len({p.row_id for p in self.problems if p.severity == ERROR and p.row_id.startswith("art-")})

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "rows_with_errors": self.rows_with_errors,
            "by_code": self.by_code,
            "stats": self.stats,
            "problems": [p.to_dict() for p in self.problems],
        }

    def summary(self) -> str:
        codes = ", ".join(f"{code} {n}" for code, n in sorted(self.by_code.items())) or "none"
        return f"{self.rows} rows, {self.rows_with_errors} with errors; by code: {codes}"


def row_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unknown, mood_from, steps, relation_keys = Counter(), Counter(), Counter(), Counter()
    grams: Counter[str] = Counter()
    hedges: Counter[str] = Counter()
    bytes_by_depth: dict[str, list[int]] = {}
    for row in rows:
        for q in row.get("questions", []):
            if q.get("needs") == "unknown":
                unknown[q.get("key")] += 1
            match = re.match(r"rel\d+ (a|rel|b)$", str(q.get("key", "")))
            if match:
                relation_keys[match.group(1)] += 1
        mood = (row.get("spec") or {}).get("mood")
        if mood:
            mood_from[mood.get("from")] += 1
        text = " ".join(str(t.get("text", "")) for t in row.get("thoughts", []))
        for t in row.get("thoughts", []):
            steps[t.get("step")] += 1
        bytes_by_depth.setdefault(str(row.get("depth")), []).append(len(text.encode("utf-8")))
        words = re.findall(r"[a-z']+", text.lower())
        for i in range(len(words) - 3):
            grams[" ".join(words[i : i + 4])] += 1
        for hedge in HEDGES:
            hedges[hedge] += text.lower().count(hedge)
    relation_total = sum(relation_keys.values())
    return {
        "by_depth": dict(Counter(r.get("depth") for r in rows)),
        "by_domain": dict(Counter(r.get("domain") for r in rows)),
        "unknown_keys": dict(unknown.most_common()),
        "mood_from": dict(mood_from.most_common()),
        "step_kinds": dict(steps.most_common()),
        "relation_keys": dict(relation_keys),
        "relation_b_share": round(relation_keys["b"] / relation_total, 3) if relation_total else 0.0,
        "repeated_4grams": [(g, n) for g, n in grams.most_common(14) if n >= 3],
        "hedges": dict(hedges),
        "thought_bytes": {d: (min(v), max(v)) for d, v in bytes_by_depth.items()},
    }


def balance_problems(stats: Mapping[str, Any]) -> list[Problem]:
    """Batch-wide spreads that no single row can show."""
    out: list[Problem] = []
    if stats["relation_b_share"] > RELATION_B_SHARE_MAX:
        out.append(Problem("relation_key", "batch", f"the second thing is asked on {stats['relation_b_share']:.0%} of the relation questions (at most half)", FLAG))
    unknown = stats["unknown_keys"]
    total = sum(unknown.values())
    if total >= 8:
        key, n = next(iter(unknown.items()))
        if n / total > UNKNOWN_SHARE_MAX:
            out.append(Problem("unknown_spread", "batch", f"{key!r} is {n} of {total} unknown questions; rotate the keys", FLAG))
    return out
