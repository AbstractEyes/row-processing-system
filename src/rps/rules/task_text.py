"""The user message of a writing call: one spec item, rendered as plain text.

The system prompt holds the rules; this text holds only what is particular to the
call: the ids to use, the records to keep, the hint of a seeded item, and the choices
the runner made for the batch's balance (`balance.assign`).

`build_task_text` writes the message of the default prompt. A prompt profile (see the
package notes) lays the message out in its own words: its task.tpl.txt names fields in
braces, and `profile_task_text` returns the function that fills them for that profile.

    {series_id}, {row_ids}, {domain}  the item's series id, its row ids (joined by ", "), its domain
    {ranks}                          "1 = simple, 2 = medium, 3 = deep" over the depths a series holds;
                                     "1 = <depth>" for a single row
    {task_kind}, {task_line}         "series" or "single row"; "Write one series." or "Write one single row."
    {genre_line}                     the genre to keep, or the instruction to choose one
    {source_line}                    whether the item is a real picture or a drawn one, and what that allows
    {hint_block}                     the hint of a real picture, in the profile's own words
    {record_mode}                    how the records are laid out
    {records_by_depth}               the records to keep, depth by depth
    {relations_by_depth}             the relations to keep, depth by depth, for a layout that lists them apart
    {relation_line}, {unknown_line}, {wordings_line}
                                     the runner's choices for the call

A real picture is an item with a seed id; a drawn item has none. Where a profile words a
field as the default message does, the text is the default message's own.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..assessor.checks import GENRES
from ..config import ConfigError
from ..prompting import FIELD_PATTERN, fill_fields
from .balance import Assignment, assign

RANK_BY_DEPTH = {"simple": 1, "medium": 2, "deep": 3}
SEEDED_DEEP_NOTE = " (the picture itself; cut the lower ranks from it)"
NOTHING_ASSIGNED = "none."  # a layout's fixed line for a choice the call does not make

Field = Callable[[Mapping[str, Any], Assignment], str]


# ---------------------------------------------------------------------------- the parts of the default message


def relation_lines(spec: Mapping[str, Any]) -> list[str]:
    """One line for each relation of a spec, numbered from 1."""
    return [
        f"  relation {number}: {relation['a']} | {relation['rel']} | {relation['b']}"
        for number, relation in enumerate(spec.get("relation") or [], 1)
    ]


def render_spec(spec: Mapping[str, Any], *, relations: bool = True) -> list[str]:
    """The records of a spec, one line each, then its relations (left out when `relations` is false)."""
    lines: list[str] = []
    for system, body in spec.items():
        if system == "relation":
            continue
        for attribute, value in body.items():
            lines.append(f"  {system} {attribute} = {value}")
    if relations:
        lines.extend(relation_lines(spec))
    return lines or ["  (no records)"]


def series_depths(item: Mapping[str, Any]) -> list[str]:
    """The depths a series item holds specs for, lowest first."""
    return [depth for depth in ("simple", "medium", "deep") if depth in item["specs"]]


def records_lines(item: Mapping[str, Any], *, relations: bool = True) -> list[str]:
    """The records to keep: depth by depth for a series (a real picture's deep records are the picture itself),
    the lines alone for a single row."""
    if item.get("kind") != "series":
        return render_spec(item["spec"], relations=relations)
    seeded = bool(item.get("seed_id"))
    lines: list[str] = []
    for depth in series_depths(item):
        lines.append(f"{depth}" + (SEEDED_DEEP_NOTE if seeded and depth == "deep" else "") + ":")
        lines.extend(render_spec(item["specs"][depth], relations=relations))
    return lines


def ranks_text(item: Mapping[str, Any]) -> str:
    """The ranks and depths of the rows asked for, as "1 = simple, 2 = medium, 3 = deep"."""
    if item.get("kind") == "series":
        return ", ".join(f"{RANK_BY_DEPTH[depth]} = {depth}" for depth in series_depths(item))
    return f"1 = {item['depth']}"


def genre_text(item: Mapping[str, Any]) -> str:
    """What the default message says after "Genre: "."""
    genre = item.get("genre")
    if not genre:
        return "choose the one that fits, from the genre list."
    if genre in GENRES:
        return f"{genre}."
    return f'the drawn genre "{genre}" is not on the genre list (it is a topic or an object name, not a genre); choose the genre that fits from the list.'


def relation_text(plan: Assignment) -> str:
    """The default message's relation choice (without the "- " before it)."""
    if plan.relation_part:
        return f"Relation question (rows that hold a relation): ask 'rel1 {plan.relation_part}'."
    return "This item holds no relation: write no relation and ask no relation question."


def unknown_text(plan: Assignment) -> str | None:
    """What the default message says after "- Unknown question key by depth: " (None when no key is assigned)."""
    if not plan.unknown_keys:
        return None
    return "; ".join(f"{depth} = '{key}'" for depth, key in plan.unknown_keys.items()) + " (the row must not hold these keys: add no record under them)."


def wordings_text(plan: Assignment) -> str:
    """What the default message says after "- Wordings by depth: "."""
    return "; ".join(f"{depth} = {', '.join(kinds)}" for depth, kinds in plan.wordings.items()) + "."


def build_task_text(item: Mapping[str, Any], ordinal: int, assignment: Assignment | None = None) -> str:
    """The user message for one spec item at position `ordinal` of its batch."""
    plan = assignment or assign(item, ordinal)
    is_series = item.get("kind") == "series"
    seeded = bool(item.get("seed_id"))
    out: list[str] = []
    ids = list(item["ids"])
    if is_series:
        out.append(f"Write one series. Series id {item['series']}. Row ids in order: " + ", ".join(ids) + ".")
        out.append("Ranks and depths: " + ranks_text(item) + ".")
    else:
        out.append(f"Write one single row. Series id {item['series']}. Row id {ids[0]}. Rank 1, depth {item['depth']}.")
    out.append(f"Domain: {item['domain']}.")
    out.append("Genre: " + genre_text(item))
    if seeded:
        out.append("")
        out.append(
            "This item comes from a real picture. The records are facts about it; the hint below is "
            "evidence, never text to copy. Keep every record; add facts only where the rules allow."
        )
        out.append(f"Hint: {item.get('hint', '')}")
    out.append("")
    out.append("Records to keep (key = value):")
    out.extend(records_lines(item))
    out.append("")
    out.append("Assigned for this call:")
    out.append("- " + relation_text(plan))
    unknown = unknown_text(plan)
    if unknown is not None:
        out.append("- Unknown question key by depth: " + unknown)
    out.append("- Wordings by depth: " + wordings_text(plan))
    out.append("")
    out.append('Return the json output object described in the rules: {"skip", "reason", "class", "rows"}.')
    return "\n".join(out)


# ---------------------------------------------------------------------------- the prompt profiles' fields


def _real(item: Mapping[str, Any]) -> bool:
    return bool(item.get("seed_id"))


def _hint(item: Mapping[str, Any]) -> str:
    return f"{item.get('hint', '')}"


def _task_kind(item: Mapping[str, Any], plan: Assignment) -> str:
    return "series" if item.get("kind") == "series" else "single row"


def _shared_fields() -> dict[str, Field]:
    """The fields every profile words the same way."""
    return {
        "series_id": lambda item, plan: f"{item['series']}",
        "row_ids": lambda item, plan: ", ".join(item["ids"]),
        "ranks": lambda item, plan: ranks_text(item),
        "domain": lambda item, plan: f"{item['domain']}",
        "records_by_depth": lambda item, plan: "\n".join(records_lines(item)),
        "wordings_line": lambda item, plan: wordings_text(plan),
    }


DEEPSEEK_SOURCE_DRAWN = (
    "drawn item; no real picture and no hint. Use only the given records; add only the mood, intent and story records allowed at each depth."
)
DEEPSEEK_SOURCE_REAL = (
    "real picture; the records are facts about it and the hint below is evidence, never text to copy. Keep every record; "
    "add facts only where the rules allow."
)


def _deepseek_fields() -> dict[str, Field]:
    return {
        **_shared_fields(),
        "task_kind": _task_kind,
        "genre_line": lambda item, plan: genre_text(item),
        "source_line": lambda item, plan: DEEPSEEK_SOURCE_REAL if _real(item) else DEEPSEEK_SOURCE_DRAWN,
        "hint_block": lambda item, plan: (f"Hint: {_hint(item)}" if _real(item) else "Hint: none.") + "\n",
        "relation_line": lambda item, plan: relation_text(plan),
        "unknown_line": lambda item, plan: unknown_text(plan) or NOTHING_ASSIGNED,
    }


OPENAI_GENRE_CHOICE = "choose the fitting genre from the allowed genre choices."
OPENAI_REAL = (
    "Real picture. The records are facts about it; the hint below is evidence, never text to copy. Keep every record; "
    "add facts only where the rules allow."
)


def _openai_genre(item: Mapping[str, Any], plan: Assignment) -> str:
    genre = item.get("genre")
    if not genre:
        return OPENAI_GENRE_CHOICE
    if genre in GENRES:
        return f"{genre}."
    return f'the drawn genre "{genre}" is not an allowed genre (it is a topic or an object name, not a genre); {OPENAI_GENRE_CHOICE}'


def _openai_record_mode(item: Mapping[str, Any], plan: Assignment) -> str:
    if item.get("kind") != "series":
        return "one row's records"
    if _real(item):
        return "deep records only (the picture itself; cut simple and medium from them)"
    return "per-depth records"


def _openai_relations(item: Mapping[str, Any], plan: Assignment) -> str:
    """The relations to keep: for a series, a block for each depth that holds one; for a single row, the lines."""
    if item.get("kind") == "series":
        lines: list[str] = []
        for depth in series_depths(item):
            held = relation_lines(item["specs"][depth])
            if held:
                lines += [f"{depth}:", *held]
    else:
        lines = relation_lines(item["spec"])
    return "\n".join(lines) if lines else "None."


def _openai_relation_line(item: Mapping[str, Any], plan: Assignment) -> str:
    if plan.relation_part:
        return f"rows that hold a relation ask 'rel1 {plan.relation_part}'."
    return "no relations; write none and ask no relation question."


def _openai_unknown(item: Mapping[str, Any], plan: Assignment) -> str:
    if not plan.unknown_keys:
        return NOTHING_ASSIGNED
    return "; ".join(f"{depth} = '{key}'" for depth, key in plan.unknown_keys.items()) + ". Keep each key absent and unanswered in its assigned row."


def _openai_fields() -> dict[str, Field]:
    return {
        **_shared_fields(),
        "task_kind": _task_kind,
        "genre_line": _openai_genre,
        "hint_block": lambda item, plan: f"{OPENAI_REAL}\nHint: {_hint(item)}" if _real(item) else "Drawn item; no real-picture hint.",
        "record_mode": _openai_record_mode,
        "records_by_depth": lambda item, plan: "\n".join(records_lines(item, relations=False)),
        "relations_by_depth": _openai_relations,
        "relation_line": _openai_relation_line,
        "unknown_line": _openai_unknown,
    }


def _gemini_fields(hint_layout: str) -> dict[str, Field]:
    block = hint_layout.rstrip("\n")  # the block stands where the layout's field is, on the lines of its own

    def hint_block(item: Mapping[str, Any], plan: Assignment) -> str:
        return fill_fields(block, {"hint_text": _hint(item)}) if _real(item) else ""

    return {
        **_shared_fields(),
        "task_line": lambda item, plan: "Write one series." if item.get("kind") == "series" else "Write one single row.",
        "genre_line": lambda item, plan: genre_text(item),
        "hint_block": hint_block,
        "relation_line": lambda item, plan: relation_text(plan),
        "unknown_line": lambda item, plan: unknown_text(plan) or NOTHING_ASSIGNED,
    }


def _profile_fields(profile: str, hint_layout: str | None) -> dict[str, Field]:
    if profile == "deepseek":
        return _deepseek_fields()
    if profile == "openai":
        return _openai_fields()
    if profile == "gemini":
        if hint_layout is None:
            raise ConfigError("prompt profile 'gemini': its hint block (hint.tpl.txt) is missing")
        unknown = sorted(set(FIELD_PATTERN.findall(hint_layout)) - {"hint_text"})
        if unknown:
            raise ConfigError(f"prompt profile 'gemini': the hint block names fields that are not filled: {unknown}")
        return _gemini_fields(hint_layout)
    raise ConfigError(f"the rule pool writes no task text for the prompt profile {profile!r}")


def profile_task_text(profile: str, layout: str, hint_layout: str | None = None) -> Callable[..., str]:
    """The function that writes the user message of a call in a prompt profile's layout.

    `layout` is the profile's task.tpl.txt, `hint_layout` its hint.tpl.txt when it has one. The function
    takes the same arguments as `build_task_text`. A layout that names a field this module does not fill
    for the profile raises ConfigError here, before any call is built.
    """
    fields = _profile_fields(profile, hint_layout)
    used = set(FIELD_PATTERN.findall(layout))
    unknown = sorted(used - set(fields))
    if unknown:
        raise ConfigError(f"prompt profile {profile!r}: the task layout names fields that are not filled: {unknown}")

    def build(item: Mapping[str, Any], ordinal: int, assignment: Assignment | None = None) -> str:
        plan = assignment or assign(item, ordinal)
        return fill_fields(layout, {name: fields[name](item, plan) for name in used})

    return build
