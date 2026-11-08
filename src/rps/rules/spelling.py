"""American spelling as a step after writing.

British spelling never stops or flags a row. `americanize_row` is a pure function that
turns the prose and tags of a stored row into US spelling; the given records (the spec)
keep their spelling, and so do record questions, which copy the records.

    americanize_text("a grey harbour")   -> "a gray harbor"
    americanize_row(row)                 -> a copy of the row with thoughts, wordings and
                                            non-record question text in US spelling
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

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
    """A copy of the row with US spelling in thoughts, wordings and non-record question text; the spec is left as given."""
    out = copy.deepcopy(dict(row))
    for thought in out.get("thoughts", []):
        if isinstance(thought, dict) and isinstance(thought.get("text"), str):
            thought["text"] = americanize_text(thought["text"])
    wordings = out.get("wordings")
    if isinstance(wordings, dict):
        for kind, text in wordings.items():
            if isinstance(text, str):
                wordings[kind] = americanize_text(text)
    for question in out.get("questions", []):
        if isinstance(question, dict) and question.get("needs") == "chain" and isinstance(question.get("value"), str):
            question["value"] = americanize_text(question["value"])
    return out
