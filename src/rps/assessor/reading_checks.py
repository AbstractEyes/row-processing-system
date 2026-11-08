"""Checks for how a row reads its records: slots, tags, record talk and stock openings.

    tags          tag_color, tag_person, tag_solo         a recorded color stays with its thing; a person record yields a person tag
    slots         story_slot, story_cause, chain_kind     story before/after hold events; a chain answer is a value of its key's kind
    record talk   spec_talk, stock_opening, check_repeat  "the blue-hour scene time", "Check the X:", a check thought that repeats another

Every finding names the words to change. Which codes stop a row is set in `assessor.ERROR_WHEN_RAISED`
and by the severity given here.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .problems import ERROR, FLAG, Problem
from .review_checks import _content, _rid, _sentences, _spec, _stem, _tag_items, _thoughts, _words, _wordings

# ------------------------------------------------------------------ tags

COLOR_WORDS = frozenset(
    """red orange yellow green blue purple pink brown black white gray grey silver gold beige tan cream teal navy olive magenta
    mint wine hazel lilac crimson mustard coral ivory lavender turquoise violet burgundy maroon indigo khaki salmon""".split()
)
SAME_COLOR = {
    "grey": "gray", "navy": "blue", "wine": "red", "burgundy": "red", "maroon": "red", "crimson": "red", "mint": "green", "olive": "green",
    "teal": "blue", "lilac": "purple", "lavender": "purple", "violet": "purple", "indigo": "blue", "hazel": "brown", "tan": "beige",
    "cream": "beige", "ivory": "white", "mustard": "yellow", "coral": "orange", "salmon": "orange", "magenta": "pink", "turquoise": "blue",
    "khaki": "beige", "gold": "yellow",
}


def _family(word: str) -> set[str]:
    return {word, SAME_COLOR.get(word, word)}


MODIFIERS = frozenset("dark light pale bright deep soft muted vivid rich".split())
COLOR_PAIRS = (("thing", "colour"), ("upper", "colour"), ("lower", "colour"), ("outer", "colour"), ("shoes", "colour"), ("wear", "colour"))
PERSON_TAGS = re.compile(r"^(?:\d+(?:girls?|boys?|others?)|solo|multiple (?:girls|boys|others)|(?:girl|boy|man|woman|person|child|people|men|women|boys|girls)s?)$")
PERSON_WORD = re.compile(r"\b(?:girls?|boys?|man|men|woman|women|person|people|child|children|figure)\b", re.I)


def _is_bare_color(tag: str) -> bool:
    words = tag.split()
    return bool(words) and all(w in COLOR_WORDS or w in MODIFIERS for w in words) and any(w in COLOR_WORDS for w in words)


def tag_color_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A recorded color must be in the tags, attached to its thing (not a bare color tag, not left out)."""
    tags = _tag_items(_wordings(row).get("tags"))
    if not tags:
        return []
    spec = _spec(row)
    for system, attr in COLOR_PAIRS:
        body = spec.get(system)
        if not isinstance(body, Mapping) or not body.get(attr):
            continue
        base = [w for w in _words(str(body[attr])) if w in COLOR_WORDS]
        if not base:
            continue
        wanted = set().union(*(_family(w) for w in base))
        holders = [t for t in tags if any(_family(w) & wanted for w in t.split() if w in COLOR_WORDS)]
        attached = [t for t in holders if not _is_bare_color(t)]
        if not holders:
            return [Problem("tag_color_missing", _rid(row), f"the recorded color {body[attr]!r} is not in the tags; add it with its thing (for example '{base[0]} {body.get('name', system)}')")]
        if not attached:
            return [Problem("tag_color_detached", _rid(row), f"the color {body[attr]!r} is a bare tag; write it with its thing in one tag (for example '{base[0]} {body.get('name', system)}')")]
    return []


def person_tag_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    tags = _tag_items(_wordings(row).get("tags"))
    if not tags:
        return []
    spec = _spec(row)
    has_person = bool(spec.get("person"))
    out: list[Problem] = []
    if has_person and not any(PERSON_TAGS.match(t.replace(" ", "") if t[:1].isdigit() else t) for t in tags):
        out.append(Problem("tag_person", _rid(row), "a person is recorded but no tag names the person; add 1girl, 1boy or 1other (or 2girls, multiple boys and the like)"))
    if "solo" in tags and not has_person:
        out.append(Problem("tag_solo", _rid(row), "the tag 'solo' is for a picture of one person; none is recorded, so remove 'solo'"))
    return out


# ------------------------------------------------------------------ story and chain slots

MOMENT_WORDS = re.compile(r"\b(?:instant|moment|second|seconds|before|after|later|earlier|pause|beat|just|then|a while)\b", re.I)
TIME_ONLY = re.compile(
    r"^(?:(?:the|a|an|late|early|mid|bright|dark|quiet|plain|same) )*(?:morning|afternoon|evening|night|dusk|dawn|noon|midday|midnight|daytime|day|twilight|blue hour|golden hour|sunset|sunrise)$",
    re.I,
)


def story_slot_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    story = _spec(row).get("story") or {}
    out: list[Problem] = []
    for slot in ("before", "after"):
        value = str(story.get(slot) or "")
        if value and MOMENT_WORDS.search(value):
            out.append(Problem("story_slot", _rid(row), f"story {slot} {value!r} is a moment word, not an event; name what happened {slot} (a person, a thing and an action)"))
    cause = str(story.get("cause") or "")
    if cause and TIME_ONLY.match(cause.strip()):
        out.append(Problem("story_cause", _rid(row), f"story cause {cause!r} is only a time; name what brought the scene about"))
    return out


KIND = {
    "look eyes": (re.compile(r"\b(?:" + "|".join(sorted(COLOR_WORDS)) + r"|dark|light|closed|wide|narrow|bright)\b", re.I), "an eye color or look"),
    "shot lens": (re.compile(r"\b(?:wide|tele\w*|macro|fisheye|prime|zoom|portrait|normal|standard|\d+ ?mm)\b", re.I), "a kind of lens"),
    "shot framing": (re.compile(r"\b(?:close|wide|medium|full|portrait|two shot|waist|extreme|long|cropped|tight|establishing|bust|head)\b", re.I), "a framing"),
    "shot angle": (re.compile(r"\b(?:front|frontal|profile|overhead|high|low|eye|tilt|dutch|side|back|three|aerial|bird|worm)\w*\b", re.I), "an angle"),
    "scene weather": (re.compile(r"\b(?:clear|cloud\w*|overcast|rain\w*|fog\w*|snow\w*|storm\w*|wind\w*|sun\w*|mist\w*|dry|humid|hazy|haze)\b", re.I), "weather"),
    "scene time": (re.compile(r"\b(?:morning|afternoon|evening|night|dusk|dawn|noon|midday|midnight|day\w*|twilight|hour|sunset|sunrise)\b", re.I), "a time of day"),
}


def chain_kind_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    for q in row.get("questions", []):
        if q.get("needs") != "chain":
            continue
        kind = KIND.get(str(q.get("key", "")))
        value = str(q.get("value", ""))
        if kind and not kind[0].search(value):
            return [Problem("chain_kind", _rid(row), f"chain answer {value!r} is not {kind[1]}, which {q.get('key')!r} asks for; answer with a value of that key's kind")]
    return []


# ------------------------------------------------------------------ record talk and stock openings

SYSTEMS = "scene|shot|form|look|thing|body|mood|story|intent|wear|upper|lower|outer|shoes|person"
ATTRIBUTES = (
    "time|weather|setting|layout|light|angle|lens|focus|framing|compose|space|palette|contrast|balance|rhythm|line|shape|texture|style|medium|"
    "period|colour|position|state|pose|expression|hair|eyes|feeling|tone|moment|cause|before|after|purpose|gesture|makeup"
)
SPEC_TALK = re.compile(rf"\b(?:{SYSTEMS}) (?:{ATTRIBUTES})\b|\b(?:that|this|these|those|the) relations?\b(?! (?:between|of))|\bthe (?:record|records)\b", re.I)


def spec_talk_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    w = _wordings(row)
    for text in [t for _, t in _thoughts(row)] + [str(w.get(k) or "") for k in ("caption", "critique")]:
        match = SPEC_TALK.search(text)
        if match:
            return [Problem("spec_talk", _rid(row), f"{match.group(0)!r} talks about the records; say what the picture shows instead")]
    return []


STOCK = re.compile(r"^(?:check the [^:]{1,40}:|compare [^:]{1,70}:|every element from |check:|compare:)", re.I)


def stock_opening_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    for index, (_, text) in enumerate(_thoughts(row), 1):
        match = STOCK.match(text.strip())
        if match:
            return [Problem("stock_opening", _rid(row), f"thought {index} opens with a stock phrase ({match.group(0)!r}); start with what you see")]
    return []


def check_repeat_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A check thought whose content repeats an earlier thought."""
    seen: list[tuple[int, set[str]]] = []
    for index, (step, text) in enumerate(_thoughts(row), 1):
        sets = [(_content(s)) for s in _sentences(text)]
        if step == "check":
            for words in sets:
                if len(words) < 4:
                    continue
                for j, earlier in seen:
                    if len(words & earlier) / len(words) >= 0.7:
                        return [Problem("check_repeat", _rid(row), f"the check (thought {index}) repeats thought {j}; check something the earlier thoughts did not")]
        seen.extend((index, words) for words in sets)
    return []


# ------------------------------------------------------------------ caption form and pasted values

AUXILIARIES = frozenset("is are was were be been being has have had does do did will would can could may might shall should".split())
# Verbs a caption uses as its main verb: the bare plural form and the -s form both count; participles ("seen", "blurred") do not.
VERB_ROOTS = frozenset(
    """rest frame stand sit lean stretch fill hold wear carry look appear show occupy dominate sweep cross lie hang float rise reach surround
    gather cut center centre catch bear feature depict portray capture reveal hover drift perch lounge kneel crouch climb jump
    bow smile raise lift hide sprawl curl twist bend fade glint gleam loom fringe wrap drape trail tilt sink emerge present find
    contain include offer give take make let keep put bring meet join mix blend divide split fold sparkle shimmer reflect lay
    run walk move pass fly swim sail ride drive cling grow bloom sprout throw touch press push pull dip arch bridge guard watch
    overlook anchor support share repeat flank follow trace shape pick highlight emphasize draw tie pin link lend add leave belong
    remain stay wait pause linger sleep eat drink read write play dance sing stare glance laugh seem become sound feel place isolate
    project""".split()
)
IRREGULAR_PAST = frozenset("stood sat lay held wore sat rose fell ran swam flew hung drew took gave made met left lent kept grew threw".split())
CAPTION_SPLIT = re.compile(r"[.!?]\s+")


def _verb_forms() -> frozenset[str]:
    forms = set(VERB_ROOTS) | IRREGULAR_PAST
    for root in VERB_ROOTS:
        forms.add(root + "s")
        forms.add(root + "es")
        if root.endswith("y") and len(root) > 2:
            forms.add(root[:-1] + "ies")
        forms.add(root + "ed" if not root.endswith("e") else root + "d")
    return frozenset(forms)


VERB_FORMS = _verb_forms()


def _has_verb(sentence: str) -> bool:
    """True when the sentence holds an auxiliary or a main verb; a word after 'a/an/the/of/with/in' is read as a noun."""
    words = re.findall(r"[a-z']+", sentence.lower())
    for index, word in enumerate(words):
        if word in AUXILIARIES:
            return True
        if word in VERB_FORMS and not (word.endswith("ed") and word not in IRREGULAR_PAST and (index == 0 or words[index - 1] not in ("and", "that", "which", "who"))):
            if index > 0 and words[index - 1] in ("a", "an", "the", "of", "with", "in", "on", "its", "his", "her", "their", "some", "small", "large", "single", "close", "wide", "bright", "soft", "dark"):
                continue
            return True
    return False


def caption_form_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A caption of a single sentence with no verb is a noun phrase (a flag)."""
    caption = str(_wordings(row).get("caption") or "").strip()
    if not caption or len(caption.split()) < 5:
        return []
    sentences = [s for s in CAPTION_SPLIT.split(caption) if s.strip()]
    if sentences and not any(_has_verb(s) for s in sentences):
        return [Problem("caption_no_verb", _rid(row), "the caption has no verb; write one sentence that says something about the picture", FLAG)]
    return []


PASTED_VALUE = re.compile(
    r"\b(?:exactly|precisely) \d+\b|\bchild (?:boy|girl)\b|\bcount of \d+\b",
    re.I,
)


def pasted_value_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A record value pasted into prose: 'Exactly 1 girl', 'child boy' (a flag)."""
    w = _wordings(row)
    for text in [t for _, t in _thoughts(row)] + [str(w.get(k) or "") for k in ("caption", "critique")]:
        match = PASTED_VALUE.search(text)
        if match:
            return [Problem("pasted_value", _rid(row), f"{match.group(0)!r} reads like a record pasted into prose; write it as a person would say it", FLAG)]
    return []


READING_CHECKS = (
    tag_color_check, person_tag_check, story_slot_check, chain_kind_check, spec_talk_check, stock_opening_check, check_repeat_check, caption_form_check, pasted_value_check,
)
