"""The user message of a writing call: one spec item, rendered as plain text.

The system prompt holds the rules; this text holds only what is particular to the
call: the ids to use, the records to keep, the hint of a seeded item, and the choices
the runner made for the batch's balance (`balance.assign`).
"""

from __future__ import annotations

from typing import Any, Mapping

from ..assessor.checks import GENRES
from .balance import Assignment, assign

RANK_BY_DEPTH = {"simple": 1, "medium": 2, "deep": 3}


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
    out.append("Assigned for this call:")
    if plan.relation_part:
        out.append(f"- Relation question (rows that hold a relation): ask 'rel1 {plan.relation_part}'.")
    else:
        out.append("- This item holds no relation: write no relation and ask no relation question.")
    if plan.unknown_keys:
        out.append(
            "- Unknown question key by depth: "
            + "; ".join(f"{depth} = '{key}'" for depth, key in plan.unknown_keys.items())
            + " (the row must not hold these keys: add no record under them)."
        )
    out.append("- Wordings by depth: " + "; ".join(f"{depth} = {', '.join(kinds)}" for depth, kinds in plan.wordings.items()) + ".")
    out.append("")
    out.append('Return the json output object described in the rules: {"skip", "reason", "class", "rows"}.')
    return "\n".join(out)
