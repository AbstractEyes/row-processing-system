"""The user message of a writing call: one spec item, rendered as plain text.

The system prompt holds the rules; this text holds only what is particular to the
call: the ids to use, the records to keep, the hint of a seeded item, what the rows may
not use (the facts only a deeper row of a drawn series records, and the details the item
records nowhere), and the choices the runner made for the batch's balance (`balance.assign`).
"""

from __future__ import annotations

from typing import Any, Mapping

from ..assessor.checks import GENRES
from .balance import Assignment, assign, spec_keys

RANK_BY_DEPTH = {"simple": 1, "medium": 2, "deep": 3}

# Details writers add by habit: (what it is, the record keys that would give it, which items it concerns: every item,
# items with a person, items with a thing). A detail the item's deepest spec gives by none of its keys goes on the
# item's "not recorded" line.
TRAPS: tuple[tuple[str, frozenset[str], str], ...] = (
    ("a pose or motion (standing, sitting, walking, leaning, looking up, a tilted head)", frozenset({"body pose"}), "person"),
    ("a posture or motion of the thing (standing, perched, moving, still)", frozenset({"thing state"}), "thing"),
    ("a facial expression (smiling, frowning, calm, focused)", frozenset({"look expression"}), "person"),
    ("the light (soft, warm, golden, natural, bright, dim)", frozenset({"shot light"}), "every"),
    ("the weather or the sky (clear, cloudy, rain)", frozenset({"scene weather"}), "every"),
    ("the time of day (morning, noon, dusk, night)", frozenset({"scene time"}), "every"),
    ("materials and surfaces (brick, stone, concrete, wood, smooth, worn)", frozenset({"form texture"}), "every"),
    ("sizes and extents (narrow, wide, tall, massive, towering, vast)", frozenset({"form shape", "form space", "scene layout"}), "every"),
    ("the camera angle (high, low, eye level)", frozenset({"shot angle"}), "every"),
    ("focus and sharpness (sharp, blurred, bokeh)", frozenset({"shot focus"}), "every"),
)

# What each unknown-question key covers, so the row can leave it out entirely (one entry per balance.UNKNOWN_KEYS).
UNKNOWN_COVERS: dict[str, str] = {
    "shot lens": "the lens (wide, telephoto, fisheye, a focal length)",
    "shot focus": "the focus (sharp, soft, blurred, bokeh)",
    "thing state": "the state of the thing (open, broken, moving, still, wet)",
    "body gesture": "any gesture (waving, pointing, hands clasped)",
    "look makeup": "makeup",
    "scene weather": "the weather or the sky",
    "form texture": "textures and surfaces (smooth, rough, grainy, worn)",
    "story before": "what happened before this moment",
    "intent message": "what the picture means to say",
    "person role": "the person's job or role",
    "wear accessory": "accessories (hats, glasses, bags, jewelry)",
    "look eyes": "the eyes, their color or look",
    "scene layout": "how full, empty, crowded or sparse the scene is",
    "thing position": "where the thing sits in the frame",
    "body form": "the build of the body",
    "look skin": "the skin",
    "form rhythm": "rhythm or repetition",
    "form space": "the sense of space (deep, open, enclosed, vast)",
    "shot angle": "the camera angle (eye level, high, low, overhead)",
    "form line": "the picture's lines (vertical, diagonal, curved)",
    "scene time": "the time of day",
    "form balance": "the picture's balance (balanced, left heavy)",
    "mood tone": "the tone (solemn, ironic, sweet, brash and the like)",
    "story cause": "why this moment happens",
}


def render_spec(spec: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for system, body in spec.items():
        if system == "relation":
            continue
        for attribute, value in body.items():
            lines.append(f"  {system} {attribute} = {value}")
    for number, relation in enumerate(spec.get("relation") or [], 1):
        lines.append(f"  relation {number}: {relation['a']} | {relation['rel']} | {relation['b']}")
    return lines or ["  (no records)"]


def spec_facts(spec: Mapping[str, Any]) -> list[str]:
    """A spec's records and relations as short lines ("scene time = dusk", "girl beside table"); a count of 1 is left out."""
    facts: list[str] = []
    for system, body in spec.items():
        if system == "relation" or not isinstance(body, Mapping):
            continue
        for attribute, value in body.items():
            if attribute == "count" and str(value) == "1":
                continue  # one person or thing is in the singular already
            facts.append(f"{system} {attribute} = {value}")
    facts.extend(f"{relation['a']} {relation['rel']} {relation['b']}" for relation in spec.get("relation") or [])
    return facts


def deepest_spec(item: Mapping[str, Any]) -> Mapping[str, Any]:
    if item.get("kind") == "series":
        specs = item.get("specs") or {}
        return next((specs[depth] for depth in ("deep", "medium", "simple") if depth in specs), {})
    return item.get("spec") or {}


def rank_scope(item: Mapping[str, Any]) -> list[str]:
    """For a drawn series, one line per shallower row: the facts it must not use because only a deeper row records them."""
    specs = item.get("specs") or {}
    if item.get("kind") != "series" or item.get("seed_id"):
        return []
    depths = [depth for depth in ("simple", "medium", "deep") if depth in specs]
    lines = []
    for index, depth in enumerate(depths[:-1]):
        own = set(spec_facts(specs[depth]))
        deeper: list[str] = []
        for later in depths[index + 1 :]:
            deeper += [fact for fact in spec_facts(specs[later]) if fact not in own and fact not in deeper]
        if deeper:
            lines.append(f"- {depth} (rank {RANK_BY_DEPTH[depth]}) must not use: " + "; ".join(deeper) + ".")
    return lines


def unrecorded(item: Mapping[str, Any]) -> str:
    """The details the item records nowhere, which no row writes (a seeded row: only where its hint states one plainly)."""
    deepest = deepest_spec(item)
    held = spec_keys(deepest)
    present = {"every", *(system for system in ("person", "thing") if system in deepest)}
    details = [text for text, keys, concerns in TRAPS if concerns in present and not held & keys]
    if not any(relation.get("rel") == "holding" for relation in deepest.get("relation") or []):
        details.append("anything held or carried")
    if item.get("seed_id"):
        return (
            "Not in the records, so write one only where the hint states it plainly: " + "; ".join(details) + ". "
            "Add no person, animal or object that neither the records nor the hint gives, and no place or relation between "
            "things that the hint does not state."
        )
    return "Recorded nowhere in this item, so no row writes it: " + "; ".join(details + ["a person, animal or object beyond the records", "sound"]) + "."


def build_task_text(item: Mapping[str, Any], ordinal: int, assignment: Assignment | None = None) -> str:
    """The user message for one spec item at position `ordinal` of its batch."""
    plan = assignment or assign(item, ordinal)
    is_series = item.get("kind") == "series"
    seeded = bool(item.get("seed_id"))
    out: list[str] = []
    ids = list(item["ids"])
    if is_series:
        specs = item["specs"]
        depths = [depth for depth in ("simple", "medium", "deep") if depth in specs]
        out.append(f"Write one series. Series id {item['series']}. Row ids in order: " + ", ".join(ids) + ".")
        out.append("Ranks and depths: " + ", ".join(f"{RANK_BY_DEPTH[d]} = {d}" for d in depths) + ".")
    else:
        depth = item["depth"]
        depths = [depth]
        out.append(f"Write one single row. Series id {item['series']}. Row id {ids[0]}. Rank 1, depth {depth}.")
    out.append(f"Domain: {item['domain']}.")
    genre = item.get("genre")
    if not genre:
        out.append("Genre: choose the one that fits, from the genre list.")
    elif genre in GENRES:
        out.append(f"Genre: {genre}.")
    else:
        out.append(f'Genre: the drawn genre "{genre}" is not on the genre list (it is a topic or an object name, not a genre); choose the genre that fits from the list.')
    if seeded:
        out.append("")
        out.append(
            "This item comes from a real picture. The records are facts about it; the hint below is "
            "evidence, never text to copy. Keep every record; add facts only where the rules allow."
        )
        out.append(f"Hint: {item.get('hint', '')}")
    out.append("")
    out.append("Records to keep (key = value):")
    if is_series:
        for depth_name in depths:
            out.append(f"{depth_name}" + (" (the picture itself; cut the lower ranks from it)" if seeded and depth_name == "deep" else "") + ":")
            out.extend(render_spec(item["specs"][depth_name]))
    else:
        out.extend(render_spec(item["spec"]))
    out.append("")
    scope = rank_scope(item)
    if scope:
        out.append("Each row writes only from its own records:")
        out.extend(scope)
    elif is_series:
        out.append(
            "Each row writes only from its own records: cut the simple and medium rows from the deep records, "
            "then let each row's text use only the records that row keeps."
        )
    out.append(unrecorded(item))
    out.append("")
    out.append("Assigned for this call:")
    if plan.relation_part:
        out.append(f"- Relation question (rows that hold a relation): ask 'rel1 {plan.relation_part}'.")
    else:
        out.append("- This item holds no relation: write no relation and ask no relation question.")
    if plan.unknown_keys:
        out.append(
            "- Unknown question key by depth: "
            + "; ".join(f"{depth} = '{key}' (that row says nothing about {UNKNOWN_COVERS.get(key, 'it')})" for depth, key in plan.unknown_keys.items())
            + ". The row must not hold these keys: add no record under them, and no thought, wording or tag answers them."
        )
    out.append("- Wordings by depth: " + "; ".join(f"{depth} = {', '.join(kinds)}" for depth, kinds in plan.wordings.items()) + ".")
    out.append("")
    out.append('Return the json output object described in the rules: {"skip", "reason", "class", "rows"}.')
    return "\n".join(out)
