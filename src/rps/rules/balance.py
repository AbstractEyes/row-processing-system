"""Per-call balances: the choices the runner makes so that a batch comes out even.

A single call cannot see the rest of the batch, so three choices are made by the
runner from the line's position in the batch and handed to the writer in the task
text, never left to the model:

    relation question   which part of the first relation to ask about ("a", "rel" or
                        "b"), cycling so the second thing ("b") is asked about on
                        a third of the relation questions, never more than half
    unknown question    which key the unknown question asks about, rotating through
                        a fixed list and skipping keys the item's own spec holds and keys that do not fit it (a person key for a personless item);
                        the keys of one series are all different
    wordings            which wordings the row carries, from its depth
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

RELATION_PARTS: tuple[str, ...] = ("a", "rel", "b")

# Keys an unknown question may ask about (picture details a spec may leave out).
UNKNOWN_KEYS: tuple[str, ...] = (
    "shot lens",
    "shot focus",
    "thing state",
    "body gesture",
    "look makeup",
    "scene weather",
    "form texture",
    "story before",
    "intent message",
    "person role",
    "wear accessory",
    "look eyes",
    "scene layout",
    "thing position",
    "body form",
    "look skin",
    "form rhythm",
    "form space",
    "shot angle",
    "form line",
    "scene time",
    "form balance",
    "mood tone",
    "story cause",
)

# Keys that only fit an item that records a person, or one that records a thing.
PERSON_KEYS: frozenset[str] = frozenset(
    {"body gesture", "look makeup", "person role", "wear accessory", "look eyes", "body form", "look skin"}
)
THING_KEYS: frozenset[str] = frozenset({"thing state", "thing position"})
# Keys left out of the rotation: every medium and deep row records a mood, so an unknown tone would sit beside one.
NEVER_ASKED: frozenset[str] = frozenset({"mood tone"})
# A recorded attribute that already answers a key: (system, attribute) -> keys it rules out.
ANSWERED_BY: dict[tuple[str, str], frozenset[str]] = {
    ("body", "pose"): frozenset({"body gesture"}),
}

WORDINGS_BY_DEPTH: dict[str, tuple[str, ...]] = {
    "simple": ("caption",),
    "medium": ("caption", "prompt", "tags"),
    "deep": ("caption", "prompt", "tags", "critique"),
}

DEPTH_BY_RANK = {1: "simple", 2: "medium", 3: "deep"}


@dataclass(frozen=True)
class Assignment:
    """What the runner decided for one call."""

    relation_part: str | None  # None: the item holds no relation, so no relation question is asked
    unknown_keys: Mapping[str, str]  # depth -> key ("simple" never has one)
    wordings: Mapping[str, tuple[str, ...]]  # depth -> wording kinds

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_part": self.relation_part,
            "unknown_keys": dict(self.unknown_keys),
            "wordings": {depth: list(kinds) for depth, kinds in self.wordings.items()},
        }


def spec_keys(spec: Mapping[str, Any]) -> set[str]:
    """The record keys ("system attribute") a spec holds; a relation counts as none."""
    keys: set[str] = set()
    for system, body in spec.items():
        if system == "relation" or not isinstance(body, Mapping):
            continue
        keys.update(f"{system} {attribute}" for attribute in body)
    return keys


def item_depths(item: Mapping[str, Any]) -> tuple[str, ...]:
    """The depths a spec item asks for, lowest first."""
    if item.get("kind") == "series":
        return tuple(depth for depth in ("simple", "medium", "deep") if depth in (item.get("specs") or {})) or (
            "simple",
            "medium",
            "deep",
        )
    return (str(item.get("depth", "medium")),)


def _deepest_spec(item: Mapping[str, Any]) -> Mapping[str, Any]:
    if item.get("kind") == "series":
        specs = item.get("specs") or {}
        for depth in ("deep", "medium", "simple"):
            if depth in specs:
                return specs[depth]
        return {}
    return item.get("spec") or {}


def fits_item(key: str, spec: Mapping[str, Any], hinted: bool = False) -> bool:
    """True when an unknown question about `key` makes sense for an item whose deepest spec is `spec`.

    A hinted item's hint describes where its thing is and what state it is in, so the writer records those; they are not asked.
    """
    if key in NEVER_ASKED:
        return False
    if key in PERSON_KEYS and not spec.get("person"):
        return False
    if key in THING_KEYS and (hinted or not spec.get("thing")):
        return False
    return not any(key in ruled_out for (system, attribute), ruled_out in ANSWERED_BY.items() if isinstance(spec.get(system), Mapping) and spec[system].get(attribute) is not None)


def relation_part(ordinal: int) -> str:
    """The part of the first relation this line's relation question asks about."""
    return RELATION_PARTS[ordinal % len(RELATION_PARTS)]


def _stride(size: int) -> int:
    """The first of 5, 7, 3, 1 that shares no factor with `size`, so the rotation visits every start."""
    return next(step for step in (5, 7, 3, 1) if math.gcd(step, size) == 1)


def unknown_keys(item: Mapping[str, Any], ordinal: int) -> dict[str, str]:
    """One unknown-question key per depth above simple, all different, none held by the spec, none that does not fit it."""
    deepest = _deepest_spec(item)
    held = spec_keys(deepest)
    wanted = [depth for depth in item_depths(item) if depth != "simple"]
    pool = [key for key in UNKNOWN_KEYS if key not in held and fits_item(key, deepest, bool(item.get("hint")))]
    start = (ordinal * _stride(len(pool))) % len(pool) if pool else 0
    chosen: dict[str, str] = {}
    for offset in range(min(len(wanted), len(pool))):
        chosen[wanted[offset]] = pool[(start + offset) % len(pool)]
    return chosen


def holds_relation(item: Mapping[str, Any]) -> bool:
    """True when the item's deepest spec holds at least one relation."""
    return bool(_deepest_spec(item).get("relation"))


def assign(item: Mapping[str, Any], ordinal: int) -> Assignment:
    """The runner's choices for the spec item at position `ordinal` in its batch."""
    depths = item_depths(item)
    return Assignment(
        relation_part=relation_part(ordinal) if holds_relation(item) else None,
        unknown_keys=unknown_keys(item, ordinal),
        wordings={depth: WORDINGS_BY_DEPTH[depth] for depth in depths},
    )
