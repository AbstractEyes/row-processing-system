"""A screen for seeded items whose records do not agree with their own hint.

Some seed items arrive garbled: a record carries the hint's own typo ("start" for "star"), or
a relation names two things the hint never mentions. Writing them wastes a call and the rows
are rejected, so the builder sets such an item aside before any call is made.

    garbled = screen_item(item)          # None, or a reason string
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .review_checks import _stem, _words


def _edit_distance_one(a: str, b: str) -> bool:
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


# Other words a hint uses for a recorded thing name; a name outside the table must itself appear in the hint.
SYNONYMS: dict[str, frozenset[str]] = {
    key: frozenset(value.split())
    for key, value in {
        "couch": "sofa sofas loveseat settee",
        "sofa": "couch couches loveseat settee",
        "oven": "stove stoves range cooker kitchen",
        "stove": "oven ovens range cooker kitchen",
        "refrigerator": "fridge fridges freezer",
        "fridge": "refrigerator freezer",
        "airplane": "plane planes aeroplane aeroplanes jet jets aircraft airliner",
        "plane": "airplane aeroplane jet aircraft airliner",
        "boat": "ship ships boats yacht yachts vessel vessels ferry sailboat",
        "ship": "boat boats vessel vessels yacht",
        "cow": "bull bulls cattle cows ox calf calves",
        "bull": "cow cows cattle ox",
        "medal": "medallion medallions seal seals coin coins disk disc",
        "coin": "medal medallion medallions disk disc",
        "plaquette": "plaque relief medallion panel",
        "statuette": "statue figure figurine sculpture toad",
        "train": "locomotive engine railway railroad",
        "phone": "telephone cellphone smartphone mobile",
        "pizza": "food meal dish",
        "sandwich": "food meal sandwiches bread toast burger",
        "orange": "oranges fruit",
        "carrot": "carrots vegetables vegetable",
        "broccoli": "vegetables vegetable",
        "cup": "mug glass",
        "suitcase": "luggage bag baggage trolley",
        "retriever": "dog dogs puppy",
        "dog": "retriever puppy",
        "cat": "kitten kitty",
        "car": "vehicle vehicles automobile sedan jeep",
        "truck": "lorry vehicle",
        "bicycle": "bike bikes cycle",
        "motorcycle": "motorbike bike scooter",
        "television": "tv",
        "laptop": "computer notebook",
        "tie": "necktie",
        "handbag": "purse bag",
        "bag": "purse handbag",
        "cellphone": "phone smartphone",
    }.items()
}
# A hint that is only museum metadata ("medium: silver. subjects: portrait.") does not name its object, so it is left alone.
# Names of a picture's own form or frame; the hint describes the subject in other words, so these are not screened.
FORM_NAMES = frozenset("painting drawing photograph photo print sketch image picture illustration display skyline logo".split())
METADATA_KEY = re.compile(r"^(?:medium|subjects?|school|title|period|culture|artist|date|classification|style)\s*:", re.I)


def _metadata_only(hint: str) -> bool:
    prose = [part for part in re.split(r"(?<=[.])\s+", hint.strip()) if part and not METADATA_KEY.match(part)]
    return len(" ".join(prose).split()) < 6


def _deepest(item: Mapping[str, Any]) -> Mapping[str, Any]:
    specs = item.get("specs")
    if isinstance(specs, Mapping) and specs:
        return specs.get("deep") or list(specs.values())[-1] or {}
    return item.get("spec") or {}


def _names_thing(name: str, words: set[str], hint: str) -> bool:
    """True when the hint holds the thing's name, its plural, its last word or a listed synonym."""
    head = name.split()[-1]
    forms = {name, head, name + "s", head + "s", name + "es", head + "es"}
    if head.endswith("y"):
        forms |= {name[:-1] + "ies", head[:-1] + "ies"}
    forms |= SYNONYMS.get(name, frozenset()) | SYNONYMS.get(head, frozenset())
    return bool(forms & words) or (" " in name and name in hint)


def screen_item(item: Mapping[str, Any]) -> str | None:
    """A reason to set the item aside, or None. Only items with a hint are screened."""
    hint = str(item.get("hint") or "").lower()
    if not hint:
        return None
    words = _words(hint)
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    spec = _deepest(item)
    thing = str((spec.get("thing") or {}).get("name") or "").lower().strip()
    for system, body in spec.items():
        if system == "relation":
            continue  # object-pair relations are often not in a caption; the name check below decides
        elif isinstance(body, Mapping) and body.get("name"):
            value = str(body["name"]).lower()
            if " " in value or len(value) < 4:
                continue
            if SYNONYMS.get(value, frozenset()) & counts.keys():
                continue
            for w, n in counts.items():
                if 4 <= len(w) <= len(value) and w[0] == value[0] and _edit_distance_one(value, w) and not (w + "s" == value or value + "s" == w) and n >= counts.get(value, 0):
                    return f"the record {system} name {value!r} looks like a misspelling of {w!r}, which the hint also uses"
    if thing and not _names_thing(thing, set(words), hint) and thing.split()[-1] not in FORM_NAMES and not _metadata_only(hint):
        return f"the record thing name {thing!r} never appears in the hint; the item records a thing the hint does not show"
    return None
