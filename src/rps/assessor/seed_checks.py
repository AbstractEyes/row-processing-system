"""A screen for seeded items whose records do not agree with their own hint.

Some seed items arrive garbled: a record carries the hint's own typo ("start" for "star"), or
a relation names two things the hint never mentions. Writing them wastes a call and the rows
are rejected, so the builder sets such an item aside before any call is made.

    garbled = screen_item(item)          # None, or a reason string
"""

from __future__ import annotations

from typing import Any, Mapping

from .review_checks import _stem, _words


def _edit_distance_one(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


def _deepest(item: Mapping[str, Any]) -> Mapping[str, Any]:
    specs = item.get("specs")
    if isinstance(specs, Mapping) and specs:
        return specs.get("deep") or list(specs.values())[-1] or {}
    return item.get("spec") or {}


def screen_item(item: Mapping[str, Any]) -> str | None:
    """A reason to set the item aside, or None. Only items with a hint are screened."""
    hint = str(item.get("hint") or "").lower()
    if not hint:
        return None
    words = _words(hint)
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    stems = {_stem(w) for w in words}
    spec = _deepest(item)
    for system, body in spec.items():
        if system == "relation":
            continue  # object-pair relations are often not in a caption; the name check below decides
        elif isinstance(body, Mapping) and body.get("name"):
            value = str(body["name"]).lower()
            if " " in value or len(value) < 4:
                continue
            for w, n in counts.items():
                if 4 <= len(w) <= len(value) and w[0] == value[0] and _edit_distance_one(value, w) and not (w + "s" == value or value + "s" == w) and n >= counts.get(value, 0):
                    return f"the record {system} name {value!r} looks like a misspelling of {w!r}, which the hint also uses"
    return None
