"""Per-call balances: the choices the runner makes so that a batch comes out even.

A single call cannot see the rest of the batch, so three choices are made by the
runner from the line's position in the batch and handed to the writer in the task
text, never left to the model:

    relation question   which part of the first relation to ask about ("a", "rel" or
                        "b"), cycling so the second thing ("b") is asked about on
                        a third of the relation questions, never more than half
    unknown question    which key the unknown question asks about, rotating through
                        a fixed list and skipping keys the item's own spec holds;
                        the keys of one series are all different
    wordings            which wordings the row carries, from its depth
"""

from __future__ import annotations

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

WORDINGS_BY_DEPTH: dict[str, tuple[str, ...]] = {
    "simple": ("caption",),
    "medium": ("caption", "prompt", "tags"),
    "deep": ("caption", "prompt", "tags", "critique"),
}

DEPTH_BY_RANK = {1: "simple", 2: "medium", 3: "deep"}


@dataclass(frozen=True)
class Assignment:
    """What the runner decided for one call."""

    relation_part: str
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


def relation_part(ordinal: int) -> str:
    """The part of the first relation this line's relation question asks about."""
    return RELATION_PARTS[ordinal % len(RELATION_PARTS)]


def unknown_keys(item: Mapping[str, Any], ordinal: int) -> dict[str, str]:
    """One unknown-question key per depth above simple, all different, none held by the spec."""
    held = spec_keys(_deepest_spec(item))
    wanted = [depth for depth in item_depths(item) if depth != "simple"]
    start = (ordinal * 5) % len(UNKNOWN_KEYS)
    chosen: dict[str, str] = {}
    taken: set[str] = set()
    offset = 0
    while len(chosen) < len(wanted) and offset < len(UNKNOWN_KEYS):
        key = UNKNOWN_KEYS[(start + offset) % len(UNKNOWN_KEYS)]
        offset += 1
        if key in held or key in taken:
            continue
        taken.add(key)
        chosen[wanted[len(chosen)]] = key
    return chosen


def assign(item: Mapping[str, Any], ordinal: int) -> Assignment:
    """The runner's choices for the spec item at position `ordinal` in its batch."""
    depths = item_depths(item)
    return Assignment(
        relation_part=relation_part(ordinal),
        unknown_keys=unknown_keys(item, ordinal),
        wordings={depth: WORDINGS_BY_DEPTH[depth] for depth in depths},
    )
