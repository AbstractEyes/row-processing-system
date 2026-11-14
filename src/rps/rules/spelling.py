"""American spelling as a step after writing.

British spelling never stops or flags a row. `americanize_row` is a pure function that
turns the prose and tags of a stored row into US spelling; the given records (the spec)
keep their spelling, and so do record questions, which copy the records.

    americanize_text("a grey harbour")   -> "a gray harbor"
    americanize_row(row)                 -> a copy of the row with thoughts, wordings and
                                            non-record question text in US spelling

A row written with the picture attached quotes the text its picture shows, letter for letter
(`rps.assessor.shown_text`). The words inside double quotes of such a row are the picture's own
and keep their spelling: a sign that reads "TOWN CENTRE" is not respelled.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from ..assessor import is_picture_row
from ..assessor.shown_text import outside_quotes

_OUR = r"(?:colo|favo|harbo|neighbo|behavio|armo|hono|flavo|labo|humo|rumo|vapo|odo)"

# (pattern, replacement); replacements keep the word's capitalization.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"\b({_OUR})ur", re.I), r"\1r"),
    (re.compile(r"\bgrey(?!hound)", re.I), "gray"),
    (re.compile(r"\bcentred\b", re.I), "centered"),
    (re.compile(r"\bcentring\b", re.I), "centering"),
    (re.compile(r"\bcentre", re.I), "center"),
    (re.compile(r"\bjewellery\b", re.I), "jewelry"),
    (re.compile(r"\bcatalogue", re.I), "catalog"),
    (re.compile(r"\btheatre", re.I), "theater"),
    (re.compile(r"\bmetre(s?)\b", re.I), r"meter\1"),
    (re.compile(r"\bfibre", re.I), "fiber"),
    (re.compile(r"\bcosy\b", re.I), "cozy"),
    (re.compile(r"\bpyjamas\b", re.I), "pajamas"),
    (re.compile(r"\btravell(er|ers|ed|ing)\b", re.I), r"travel\1"),
    (re.compile(r"\b(organ|real|recogn)is(e|ed|es|ing|ation|ations)\b", re.I), r"\1iz\2"),
)


def _keep_case(original: str, replacement: str) -> str:
    if original.isupper() and len(original) > 1:
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def americanize_text(text: str) -> str:
    for pattern, replacement in _RULES:

        def swap(match: re.Match[str], replacement: str = replacement) -> str:
            return _keep_case(match.group(0), match.expand(replacement))

        text = pattern.sub(swap, text)
    return text


def americanize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the row with US spelling in thoughts, wordings and non-record question text; the spec is left as given.

    On a picture row (its writer label holds a picture size) the words inside double quotes are left as written, and
    a chain value that the thoughts state only among those words keeps its spelling with them.
    """
    out = copy.deepcopy(dict(row))
    picture = is_picture_row(row)

    def respell(text: str) -> str:
        return outside_quotes(text, americanize_text) if picture else americanize_text(text)

    for thought in out.get("thoughts", []):
        if isinstance(thought, dict) and isinstance(thought.get("text"), str):
            thought["text"] = respell(thought["text"])
    wordings = out.get("wordings")
    if isinstance(wordings, dict):
        for kind, text in wordings.items():
            if isinstance(text, str):
                wordings[kind] = respell(text)
    stated = " ".join(str(t.get("text", "")) for t in out.get("thoughts", []) if isinstance(t, dict)).lower() if picture else ""
    for question in out.get("questions", []):
        if isinstance(question, dict) and question.get("needs") == "chain" and isinstance(question.get("value"), str):
            value = americanize_text(question["value"])
            if not picture or value.lower() in stated or question["value"].lower() not in stated:
                question["value"] = value
    return out
