"""The text a picture shows, as a row quotes it.

A writer that was sent the picture reads the text in it (rule pool, rules 113 to 116): one thought with the step
"say" gives the words in double quotes, exactly as written, and the caption and the prompt quote them. Those words
are the picture's own, in its spelling and its letter case. They are not the row's prose, so a check that reads the
prose for what the row claims must not read them: a sign that says "OPEN" does not answer a question about the
layout, "french fries" on a menu is not a style name in lower case, and "Fresh Bread Daily" holds no person's name.

Not every quotation is shown text. A quotation of a row counts as shown text only when both of these hold:

    it stands in a thought whose step is "say", or its words are quoted in such a thought of the same row
    (the same words, or a run of them);
    it is no longer than SHOWN_TEXT_MAX bytes.

Every other quotation is the row's own words and is read as such, so prose put between quote marks hides nothing.

    quoted(text)               the texts inside double quotes, in order
    shown(row)                 the quotations of the row that count as shown text, in order
    own_prose(row)             the row with its shown text emptied, in its thoughts, caption, prompt and critique
    beside_hint(row, item)     the row, and its item, without the shown words that the hint quotes too
    outside_quotes(text, f)    the text with `f` applied to everything but its quoted texts

A quoted text opens with a double quote that follows a space, a bracket or the start of the text and closes with one
that a letter or a digit does not follow, so the inch mark of 12" opens nothing; curly double quotes count too. A hint
is written by other hands and may also quote in single quotes. The tags are a list of tags and quote nothing, so they
are never touched. Each function hands back what it was given when it has nothing to change, and none changes its input.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

QUOTED = re.compile(r'(?<![\w"])"([^"\n]+)"(?![\w"])|“([^”\n]+)”')
HINT_QUOTED = re.compile(QUOTED.pattern + r"|(?<![\w'])'([^'\n]+)'(?![\w'])|‘([^’\n]+)’")
WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
PROSE_WORDINGS = ("caption", "prompt", "critique")
SAY = "say"  # the step of the thought that gives the words a picture shows
# The longest quotation that counts as shown text, in bytes of UTF-8. The thoughts of a row have a hard length limit,
# and the smallest row is the simple one: "1 or 2 thoughts of at most 100 bytes" (the writer prompt). Every row of a
# picture quotes the same words, the simple one among them, so a text that is longer is quoted by its main words, as
# the writer is told, and a longer quotation is read as prose.
SHOWN_TEXT_MAX = 100


def _inner(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group is not None)


def _key(word: str) -> str:
    return word.lower().replace("’", "'")


def _words(texts: Iterable[str]) -> set[str]:
    return {_key(word) for text in texts for word in WORD.findall(text)}


def quoted(text: str) -> list[str]:
    """The texts inside double quotes, in order, without the quote marks."""
    return [_inner(match) for match in QUOTED.finditer(text)]


def outside_quotes(text: str, change: Callable[[str], str]) -> str:
    """The text with `change` applied to everything but its quoted texts, which stay as they are, marks and all."""
    parts: list[str] = []
    at = 0
    for match in QUOTED.finditer(text):
        parts += [change(text[at : match.start()]), match.group(0)]
        at = match.end()
    return "".join([*parts, change(text[at:])])


def _inside(pattern: re.Pattern[str], text: str, change: Callable[[str], str]) -> str:
    """The text with `change` applied to the inside of each quoted text; the quote marks stay."""
    return pattern.sub(lambda match: match.group(0)[0] + change(_inner(match)) + match.group(0)[-1], text)


def _texts(row: Mapping[str, Any]) -> list[tuple[str, bool]]:
    """The texts of a row that may quote (its thoughts, caption, prompt and critique), each with whether it is the
    text of a thought whose step is "say"."""
    thoughts, wordings = row.get("thoughts"), row.get("wordings")
    texts: list[tuple[str, bool]] = []
    if isinstance(thoughts, list):
        texts += [(t["text"], t.get("step") == SAY) for t in thoughts if isinstance(t, Mapping) and isinstance(t.get("text"), str)]
    if isinstance(wordings, Mapping):
        texts += [(wordings[kind], False) for kind in PROSE_WORDINGS if isinstance(wordings.get(kind), str)]
    return texts


def _shown_test(row: Mapping[str, Any]) -> Callable[[str, bool], bool]:
    """The test of the module notes for one row: called with a quotation and whether a "say" thought holds it."""
    said = [words for text, in_say in _texts(row) if in_say for quotation in quoted(text) if (words := WORD.findall(quotation))]

    def counts(quotation: str, in_say: bool) -> bool:
        if len(quotation.encode("utf-8")) > SHOWN_TEXT_MAX:
            return False
        if in_say:
            return True
        words = WORD.findall(quotation)
        return bool(words) and any(words == run[at : at + len(words)] for run in said for at in range(len(run) - len(words) + 1))

    return counts


def _reworded(row: Mapping[str, Any], change: Callable[[str, bool], str]) -> Mapping[str, Any]:
    """The row with `change` applied to each of the texts `_texts` reads (the text, and whether a "say" thought holds
    it); the row itself when none of them changes."""
    changed: dict[str, Any] = {}
    thoughts, wordings = row.get("thoughts"), row.get("wordings")
    if isinstance(thoughts, list):
        new = [
            {**t, "text": change(t["text"], t.get("step") == SAY)} if isinstance(t, Mapping) and isinstance(t.get("text"), str) else t
            for t in thoughts
        ]
        if new != thoughts:
            changed["thoughts"] = new
    if isinstance(wordings, Mapping):
        new_wordings = {kind: change(text, False) if kind in PROSE_WORDINGS and isinstance(text, str) else text for kind, text in wordings.items()}
        if new_wordings != wordings:
            changed["wordings"] = new_wordings
    return {**row, **changed} if changed else row


def _in_shown(row: Mapping[str, Any], change: Callable[[str], str]) -> Mapping[str, Any]:
    """The row with `change` applied to the inside of each quotation that counts as shown text; every other quotation,
    and everything outside the quote marks, stays as written."""
    counts = _shown_test(row)
    return _reworded(row, lambda text, in_say: _inside(QUOTED, text, lambda inner: change(inner) if counts(inner, in_say) else inner))


def shown(row: Mapping[str, Any]) -> list[str]:
    """The quotations of the row that count as shown text, in order, without the quote marks."""
    counts = _shown_test(row)
    return [quotation for text, in_say in _texts(row) for quotation in quoted(text) if counts(quotation, in_say)]


def own_prose(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The row as its writer's own prose: each shown text is emptied (the marks stay, so the place still shows)."""
    return _in_shown(row, lambda inner: "")


def beside_hint(row: Mapping[str, Any], item: Any) -> tuple[Mapping[str, Any], Any]:
    """The row and its item as the checks for hint copying compare them: (the row's view, the item's view).

    A word of the row's shown text that the hint quotes too is read off the picture by both, so it is not copied from
    the hint: the row's view leaves it out of the shown text. The item's view leaves the same words out of the hint's
    quotations, so that a run of words copied around a quotation is still a run. A check compares the row's view with
    the hint as written and with the item's view, and a finding of either counts: the hint as written still catches a
    sentence that the row copies with such a word outside quote marks. Every other quoted word of the row stays, as
    the row's own, so a sentence of the hint that is put in quotes is still seen.
    """
    hint = item.get("hint") if isinstance(item, Mapping) else None
    if not isinstance(hint, str) or not hint:
        return row, item
    shared = _words(_inner(match) for match in HINT_QUOTED.finditer(hint)) & _words(shown(row))
    if not shared:
        return row, item

    def drop(inner: str) -> str:
        return WORD.sub(lambda match: "" if _key(match.group(0)) in shared else match.group(0), inner)

    return _in_shown(row, drop), {**item, "hint": _inside(HINT_QUOTED, hint, drop)}
