"""Checks for the faults a careful reviewer finds in rows that already passed the first checks.

Each check is a function from a stored row (and its spec item) to a list of `Problem`s.
An error is only used where a script can decide the case precisely; everything else is a
flag, so the reviewer sees a pointer and judges.

    repetition         thought_repeat, mood_repeat, self_hedge
    self-talk          self_talk
    invented detail    unrecorded_thing, unrecorded_light, unrecorded_detail, unrecorded_shot (the pose checks moved to invention_checks)
    mood               mood_cause_differs, mood_weak_evidence, mood_unhedged
    required records   required_record, story_incomplete, question_key, question_value
    the hint           hint_contradicts_record, position_as_place, hint_paraphrase, single_caption_fact
    step labels        step_count, step_compare, step_relate, step_change
    wordings           tag_color, tag_value, caption_garment, style_on_scene, caption_hint_list
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from .problems import FLAG, Problem

STOP = frozenset(
    """a an the and or but of in on at to from by with without for as is are was were be been it its this that these those there
    their his her them they he she we you i not no so than then too very more most some any each every both all can could would
    should may might will just only also into onto over under up down out off one two three while whose which who what where
    when how why such other another same own set sets like as if""".split()
)

HEDGE = re.compile(
    r"\b(?:suggest\w*|seem\w*|reads? as|likely|perhaps|probably|maybe|may|might|could|would|points? to|as if|hints? (?:at|of)|implies|feels? like)\b",
    re.I,
)


def _rid(row: Mapping[str, Any]) -> str:
    return str(row.get("id", "?"))


def _stem(word: str) -> str:
    w = word.lower().strip("'")
    if w.endswith(("tly", "bly", "dly", "fly", "kly", "ply", "gly")):
        w = w[:-1]
    for suffix in ("ing", "ed", "ly", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    return w


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def _content(text: str) -> set[str]:
    return {_stem(w) for w in _words(text) if w not in STOP and len(w) > 2}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+", text) if s.strip()]


def _thoughts(row: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(str(t.get("step", "")), str(t.get("text", ""))) for t in row.get("thoughts", []) if isinstance(t, Mapping)]


def _wordings(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return row.get("wordings") or {}


def _prose(row: Mapping[str, Any]) -> list[str]:
    w = _wordings(row)
    return [t for _, t in _thoughts(row)] + [str(w.get(k) or "") for k in ("caption", "prompt", "critique")]


COUNTERFACTUAL = re.compile(r"\b(?:would|could|might|if|were|instead|rather|sink|sinks|turn|turns|fade|fades|lose|loses)\b", re.I)


def _asserted(row: Mapping[str, Any]) -> list[str]:
    """The prose sentences that state what the picture shows (not a what-if)."""
    return [s for text in _prose(row) for s in re.split(r"(?<=[.!?;])\s+", text) if not COUNTERFACTUAL.search(s)]


def _spec(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return row.get("spec") or {}


def _spec_values(row: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for system, body in _spec(row).items():
        if system == "relation":
            for rel in body or []:
                out.extend(str(rel.get(k, "")) for k in ("a", "rel", "b"))
        elif isinstance(body, Mapping):
            out.extend(str(v) for v in body.values())
    return out


def _hint(item: Any) -> str:
    return str(item.get("hint") or "") if isinstance(item, Mapping) else ""


def _evidence(row: Mapping[str, Any], item: Any) -> str:
    return (" ".join(_spec_values(row)) + " " + _hint(item)).lower()


def _evidence_stems(row: Mapping[str, Any], item: Any) -> set[str]:
    return {_stem(w) for w in _words(_evidence(row, item))}


# ------------------------------------------------------------------ repetition and self-talk

SELF_TALK = re.compile(
    r"\bthe only recorded\b|\brecorded\b(?! (?:music|history|sound|voice|video|audio|footage|history))"
    r"|\b(?:clearest|only|main|first) named (?:subject|thing|object)\b|\bnamed (?:subject|thing|here)\b|\b(?:style|period|medium|subject|genre) (?:is|are) named\b"
    r"|\b(?:is|are|was|were|not) specified\b|\bspecified by\b"
    r"|\bcount (?:of|is) \d+\b|\b(?:the )?\w+ count (?:is|of) (?:\d+|one|two|three)\b",
    re.I,
)


def self_talk_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """Talk about the records themselves that the first word filter misses ("recorded", "named here", "a count of 1")."""
    for text in _prose(row):
        match = SELF_TALK.search(text)
        if match:
            return [Problem("self_talk", _rid(row), f"the text talks about the records ({match.group(0)!r}); say what the picture shows")]
    return []


SELF_HEDGE = re.compile(
    r"\b(?:is|remains?) (?:an? )?(?:interpretation|possibility|plausible\b)"
    r"|\bnot (?:an? )?(?:claim|event)\b|\bplausible story\b"
    r"|\b(?:does|do) not (?:show|tell|explain|settle|say|clarify)\b|\bwithout (?:clarifying|explaining|guessing|settling)\b"
    r"|\bleaves? (?:\w+ )?unexplained\b|\bno clear (?:source|event|action)\b|\bremains a portrait\b|\bnothing in the (?:scene|picture) (?:shows|tells)\b",
    re.I,
)


def self_hedge_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """Clauses that hedge the row's own claims instead of looking at the picture."""
    found = [m.group(0) for _, text in _thoughts(row) for m in [SELF_HEDGE.search(text)] if m]
    if found:
        return [Problem("self_hedge", _rid(row), f"{len(found)} thought(s) hedge their own claims instead of the picture ({found[0]!r})", FLAG)]
    return []


def thought_repeat_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A sentence that restates a sentence of another thought, or a feeling named in more thoughts than its place needs."""
    thoughts = _thoughts(row)
    sentences = [(i, s, _content(s)) for i, (_, text) in enumerate(thoughts) for s in _sentences(text)]
    for x in range(len(sentences)):
        for y in range(x + 1, len(sentences)):
            (i, _, ca), (j, b, cb) = sentences[x], sentences[y]
            if i == j or min(len(ca), len(cb)) < 5:
                continue
            share = len(ca & cb) / min(len(ca), len(cb))
            if share >= 0.75:
                return [Problem("thought_repeat", _rid(row), f"thought {j + 1} restates thought {i + 1} ({share:.0%} of the content words): {b[:50]!r}", FLAG)]
    feeling = (_spec(row).get("mood") or {}).get("feeling")
    if feeling:
        stem = _stem(str(feeling))
        holders = [i for i, (_, text) in enumerate(thoughts) if stem in {_stem(w) for w in _words(text)}]
        limit = 2 if row.get("depth") == "deep" else 1
        if len(holders) > limit:
            return [Problem("mood_repeat", _rid(row), f"the mood {feeling!r} is claimed in thoughts {[h + 1 for h in holders]}; claim it once", FLAG)]
    return []


# ------------------------------------------------------------------ details no record or hint holds

THINGS = {
    "table": "table", "counter": "counter", "cushion": "pillow", "pillow": "pillow", "sofa": "sofa", "couch": "sofa", "rug": "rug",
    "curtain": "curtain", "shelf": "shelf", "shelve": "shelf", "lamp": "lamp", "window": "window", "door": "door", "fence": "fence",
    "bench": "bench", "desk": "desk", "scrub": "scrub", "bush": "bush", "path": "path", "road": "road",
    "rock": "rock", "fountain": "fountain", "bridge": "bridge", "boat": "boat",
}


def unrecorded_thing_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """Furniture or scenery named in the text that neither the records nor the hint hold (seeded items only)."""
    if not _hint(item):
        return []
    held = {THINGS.get(_stem(w), _stem(w)) for w in _words(_evidence(row, item))}
    seen: dict[str, str] = {}
    for text in _asserted(row):
        for w in _words(text):
            key = THINGS.get(_stem(w))
            if key and key not in held:
                seen.setdefault(key, w)
    if seen:
        return [Problem("unrecorded_thing", _rid(row), f"things in the text that no record or hint holds: {sorted(seen.values())}; remove {sorted(seen.values())[0]!r}", FLAG)]
    return []


LIGHT = re.compile(
    r"\b(?:shadows?|shadowed|glow\w*|backlit|dappled|rim light|golden hour|window light|"
    r"(?:soft|hard|harsh|warm|cool|gentle|diffuse\w*|even) (?:natural |window )?light)\b",
    re.I,
)
LIGHT_ROOTS = ("shadow", "glow", "backlit", "dappled", "golden", "window", "light", "sun", "lamp", "lantern", "shade", "shine")


def unrecorded_light_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """Light the text invents: shadow, glow or a quality of light when neither a light record nor the hint speaks of it."""
    spec = _spec(row)
    if (spec.get("shot") or {}).get("light"):
        return []
    evidence = _evidence(row, item)
    for text in _asserted(row):
        for match in LIGHT.finditer(text):
            word = match.group(0).lower()
            root = next((r for r in LIGHT_ROOTS if r in word), word)
            if word.endswith("light") and root == "light":
                root = "light"
            if root not in evidence:
                return [Problem("unrecorded_light", _rid(row), f"light that no record or hint holds ({match.group(0)!r}); leave the light step out when nothing is known", FLAG)]
    return []


DETAIL = re.compile(r"\b(small|large|big|tiny|huge|low|chipped|weathered|rusty|worn|faded|peeling|dusty)\s+([a-z]+)", re.I)


def unrecorded_detail_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A size or wear adjective on a thing when no record or hint gives it."""
    stems = _evidence_stems(row, item)
    for text in _asserted(row):
        for match in DETAIL.finditer(text):
            if _stem(match.group(2)) in THINGS and _stem(match.group(1)) not in stems:
                return [Problem("unrecorded_detail", _rid(row), f"{match.group(0)!r}: no record or hint gives that detail; remove {match.group(1).lower()!r}", FLAG)]
    return []


SHOT = re.compile(
    r"\b(close portrait|close-up|extreme close|tight crop|tight framing|wide shot|wide view|wide framing|medium shot|"
    r"bird's-eye|aerial view|high angle|low angle|worm's-eye|over-the-shoulder)\b",
    re.I,
)


def unrecorded_shot_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    shot = _spec(row).get("shot") or {}
    if shot.get("framing") and shot.get("angle"):
        return []
    evidence = _evidence(row, item).replace("-", " ").replace("'", "")
    for text in _asserted(row):
        for match in SHOT.finditer(text):
            phrase = match.group(0).lower().replace("-", " ").replace("'", "")
            key = phrase.split()[0]
            if key not in evidence and not (shot.get("framing") and key in ("close", "extreme", "tight", "wide", "medium")):
                return [Problem("unrecorded_shot", _rid(row), f"the shot {match.group(0)!r} is not recorded and the hint does not give it; remove {match.group(0)!r}", FLAG)]
    return []


# ------------------------------------------------------------------ mood

WEAK_FROM_ERROR = frozenset({"thing count", "thing position", "shot framing"})
WEAK_FROM_PATTERN = re.compile(r"^(shot angle|rel\d+ (a|rel|b))$")  # an angle or any relation key cannot carry a feeling
WEAK_FROM_FLAG = frozenset({"thing colour", "thing name"})
SYNONYMS = {"moving": ("motion", "movement", "move", "moves"), "standing": ("stands", "stand", "upright"), "still": ("stillness", "calm")}
# Words that show a sentence is reading the mood from this kind of record.
COLOR_CUES = ("color", "colour", "paint", "hue", "palette", "beige", "brown", "green", "blue", "red", "white", "black", "gray", "yellow")
OTHER_EVIDENCE = {
    "weather": ("sky", "cloud", "rain", "fog", "storm", "sunshine", "weather"),
    "time": ("morning", "evening", "night", "dusk", "dawn", "noon"),
    "light": ("light", "shadow", "glow", "sunlight"),
    "colour": COLOR_CUES,
}
CUES = {
    "scene weather": ("weather", "condition", "sky", "sun", "bright", "cloud", "rain", "fog", "storm", "air"),
    "scene time": ("time", "hour", "morning", "evening", "night", "dusk", "dawn", "daylight"),
    "shot light": ("light", "lit", "shadow", "glow", "lamp", "lantern"),
    "scene setting": ("setting", "place", "surround"),
}


def _mood_sentences(row: Mapping[str, Any]) -> list[str]:
    feeling = (_spec(row).get("mood") or {}).get("feeling")
    if not feeling:
        return []
    stem = _stem(str(feeling))
    return [s for _, text in _thoughts(row) for s in _sentences(text) if stem in {_stem(w) for w in _words(s)} or "mood" in s.lower()]


def mood_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    mood = _spec(row).get("mood") or {}
    source = str(mood.get("from") or "")
    if not mood or not source:
        return []
    out: list[Problem] = []
    rid = _rid(row)
    if source in WEAK_FROM_ERROR or WEAK_FROM_PATTERN.match(source):
        out.append(Problem("mood_weak_evidence", rid, f"a mood cannot be read from {source!r}; read it from a record that can carry a feeling (look expression, body pose, thing state, shot light, scene time, scene weather, scene setting, form palette), adding a fact the hint states plainly when the records hold none"))
    sentences = _mood_sentences(row)
    if not sentences:
        return out
    if source in WEAK_FROM_ERROR | WEAK_FROM_FLAG | {"thing state"} and not any(HEDGE.search(s) for s in sentences):
        out.append(Problem("mood_unhedged", rid, "the mood rests on thin evidence and is stated flatly; hedge it (suggests, reads as, likely)", FLAG))
    value = _lookup(row, source)
    if value:
        wanted = _content(value) | {_stem(w) for v in [value.lower()] for k, alts in SYNONYMS.items() if k == v for w in alts}
        mentioned = set().union(*(_content(s) for s in sentences))
        own = {"thing colour": "colour", "form palette": "colour", "scene weather": "weather", "scene time": "time", "shot light": "light"}.get(source)
        if own == "colour":
            wanted |= {_stem(w) for w in COLOR_CUES}
        wanted |= {_stem(w) for w in CUES.get(source, ())}
        other = sorted(name for name, cues in OTHER_EVIDENCE.items() if name != own and mentioned & {_stem(w) for w in cues})
        if own == "time" and other == ["light"]:
            other = []  # the hour of day and its light are one cause
        if wanted and not wanted & mentioned and other:
            out.append(Problem("mood_cause_differs", rid, f"the mood record is read from {source!r} ({value}), but the mood thoughts credit {other}", FLAG))
    return out


def _lookup(row: Mapping[str, Any], key: str) -> str:
    match = re.fullmatch(r"rel(\d+) (a|rel|b)", key)
    if match:
        relations = _spec(row).get("relation") or []
        index = int(match.group(1)) - 1
        return str(relations[index].get(match.group(2), "")) if index < len(relations) else ""
    system, _, attr = key.partition(" ")
    body = _spec(row).get(system)
    return str(body.get(attr, "")) if isinstance(body, Mapping) else ""


# ------------------------------------------------------------------ required records and question keys


def required_record_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    depth, spec, rid = row.get("depth"), _spec(row), _rid(row)
    out: list[Problem] = []
    if depth in ("medium", "deep") and not spec.get("mood"):
        out.append(Problem("required_record", rid, f"a {depth} row adds a mood record; none is there"))
    if depth == "deep":
        for system in ("intent", "story"):
            if not spec.get(system):
                out.append(Problem("required_record", rid, f"a deep row adds {system} records; none is there"))
        story = spec.get("story") or {}
        if story and not any(story.get(k) for k in ("before", "after", "cause")):
            out.append(Problem("story_incomplete", rid, "a story record holds a moment and one of before, after or cause; this one has only a moment"))
    return out


def question_key_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    from .validator import tp

    valid = set(tp.keys())
    out: list[Problem] = []
    for q in row.get("questions", []):
        key, need = str(q.get("key", "")), q.get("needs")
        if need in ("chain", "unknown") and key not in valid:
            out.append(Problem("question_key", _rid(row), f"{need} question key {key!r} is not a record key; use one of the spec keys (system attribute)"))
        if need == "chain" and (len(str(q.get("value", "")).encode("utf-8")) > 20 or len(str(q.get("value", "")).split()) > 3):
            out.append(Problem("question_value", _rid(row), f"chain question {key!r}: the value is a short phrase of at most 20 bytes, not a sentence"))
    return out


# ------------------------------------------------------------------ the hint

PERIOD_YEARS = {
    "ancient": (-3000, 600), "medieval": (450, 1500), "renaissance": (1300, 1650), "baroque": (1570, 1790),
    "19th century": (1780, 1915), "early 20th": (1890, 1950), "mid century": (1930, 1980), "1980s": (1975, 1995),
    "1990s": (1985, 2005), "contemporary": (1960, 2100),
}
DATED = re.compile(r"\bdated (?:c\. ?|circa )?(\d{4})\b", re.I)


def hint_contradiction_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """The hint dates the work outside the period record: the item should have been skipped."""
    period = str((_spec(row).get("form") or {}).get("period") or "")
    match = DATED.search(_hint(item))
    if not period or period not in PERIOD_YEARS or not match:
        return []
    year, (low, high) = int(match.group(1)), PERIOD_YEARS[period]
    if not low <= year <= high:
        return [Problem("hint_contradicts_record", _rid(row), f"the hint dates the work {year}, outside the {period!r} period record; skip the item as a contradiction")]
    return []


PLACE_OF_FRAME = re.compile(r"\b(?:middle|center|centre) of (?:a|an|the) (?!frame\b|image\b|picture\b|canvas\b|composition\b|photograph\b|painting\b|view\b|shot\b|scene\b|row\b)([a-z]+)", re.I)


def position_as_place_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A frame-position record (centre, left third) read as a place in the scene."""
    spec = _spec(row)
    positions = {str((spec.get("thing") or {}).get("position") or "").lower(), str((spec.get("form") or {}).get("compose") or "").lower()} - {""}
    if not positions:
        return []
    for text in _prose(row):
        match = PLACE_OF_FRAME.search(text)
        if match and _stem(match.group(1)) != _stem(str((spec.get("scene") or {}).get("setting") or "")):
            return [Problem("position_as_place", _rid(row), f"{match.group(0)!r}: a position record places the thing in the frame, not in the scene", FLAG)]
    return []


def hint_paraphrase_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """Sentences that follow one hint sentence closely: at least 70% of their content words come from it."""
    hint = _sentences(_hint(item))
    if len(hint) < 2:
        return []
    hint_sets = [_content(s) for s in hint]
    close = []
    for i, (_, text) in enumerate(_thoughts(row)):
        for s in _sentences(text):
            words = _content(s)
            if len(words) < 6 or _thoughts(row)[i][0] == "see":
                continue
            best = max(range(len(hint)), key=lambda k: len(words & hint_sets[k]))
            if len(words & hint_sets[best]) / len(words) >= 0.75:
                close.append((i + 1, best))
    if close:
        return [Problem("hint_paraphrase", _rid(row), f"{len(close)} sentence(s) restate a hint sentence closely (thought {close[0][0]}); say what a critic sees in them", FLAG)]
    return []


CAPTIONS_AGREE = 0.28  # mean overlap of the hint's sentences: above it they are captions of one scene, below it one narration


def _agreement(sentences: list[str]) -> float:
    sets = [_content(s) for s in sentences]
    pairs = [len(a & b) / min(len(a), len(b)) for i, a in enumerate(sets) for b in sets[i + 1 :] if min(len(a), len(b)) >= 2]
    return sum(pairs) / len(pairs) if pairs else 0.0


def single_caption_fact_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A thing only one of several captions in the hint mentions, used as if the captions agreed."""
    hint = _sentences(_hint(item))
    if len(hint) < 4 or _agreement(hint) < CAPTIONS_AGREE:
        return []
    held = {THINGS.get(_stem(w), _stem(w)) for v in _spec_values(row) for w in _words(v)}
    for key in sorted(set(THINGS.values())):
        mentions = sum(1 for s in hint if key in {THINGS.get(_stem(w), _stem(w)) for w in _words(s)})
        if mentions == 1 and key not in held and any(key in {THINGS.get(_stem(w), _stem(w)) for w in _words(t)} for t in _prose(row)):
            return [Problem("single_caption_fact", _rid(row), f"{key!r} comes from one caption of several; use only what the captions agree on", FLAG)]
    return []


# ------------------------------------------------------------------ step labels

NUMBER = re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|single|lone|solitary|alone|pair|couple|both|several|many|few|dozen|trio|twin|no|none|each|every)\b", re.I)
COMPARE = re.compile(r"\b(?:than|whereas|while|but|unlike|versus|against|rather than|compared|contrast\w*|both|beside|between|more|less|larger|smaller|closer|brighter|darker)\b", re.I)
RELATE = re.compile(
    r"\b(?:beside|besides|behind|in front of|left of|right of|above|below|under|beneath|over|between|next to|along|around|beyond|near|atop|on top|inside|within|toward|towards|across|alongside|among)\b|\bto the (?:left|right)\b",
    re.I,
)
CHANGE = re.compile(r"\b(?:if|would|instead|rather|were|than|alter\w*|shift\w*|swap\w*|turn\w*|replace\w*|without|change\w*)\b", re.I)


COMPARE_OR_CHANGE = re.compile(COMPARE.pattern + "|" + CHANGE.pattern, re.I)


def _names_two_things(row: Mapping[str, Any], text: str) -> bool:
    things = set()
    for system, body in _spec(row).items():
        if system == "relation":
            ends = [str(r.get(k, "")) for r in body or [] for k in ("a", "b")]
        else:
            ends = [str(body.get("name", ""))] if isinstance(body, Mapping) else []
        things |= {_stem(w) for e in ends for w in _words(e) if w not in STOP and len(w) > 2}
    return len(things & {_stem(w) for w in _words(text)}) >= 2


def step_label_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    out: list[Problem] = []
    for index, (step, text) in enumerate(_thoughts(row), 1):
        if step == "count" and not NUMBER.search(text):
            out.append(Problem("step_count", _rid(row), f"thought {index} is a count step with no number in it"))
        elif step == "compare" and not COMPARE_OR_CHANGE.search(text):
            out.append(Problem("step_compare", _rid(row), f"thought {index} is a compare step with no second thing to compare", FLAG))
        elif step == "relate" and not RELATE.search(text) and not _names_two_things(row, text):
            out.append(Problem("step_relate", _rid(row), f"thought {index} is a relate step that relates no two things", FLAG))
        elif step == "change" and not CHANGE.search(text):
            out.append(Problem("step_change", _rid(row), f"thought {index} is a change step with no change in it", FLAG))
    return out


# ------------------------------------------------------------------ wordings

NON_BOORU_COLORS = frozenset("maroon burgundy crimson scarlet vermilion peach golden".split())  # the words the prompt rules out
LAYOUT_KEYS = ("form compose", "form space", "form line", "form shape", "form balance", "form rhythm", "scene layout", "thing position")
GARMENT_KEYS = ("wear clothing", "upper name", "lower name", "outer name", "shoes name")


def _tag_items(tags: Any) -> list[str]:
    text = tags if isinstance(tags, str) else ", ".join(map(str, tags or []))
    return [t.strip().lower() for t in text.split(",") if t.strip()]


def tag_color_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    items = _tag_items(_wordings(row).get("tags"))
    bad = sorted({w for t in items for w in _words(t) if w in NON_BOORU_COLORS and not re.search(r"golden (?:retriever|hour|gate|ratio)", t)})
    out = []
    if bad:
        out.append(Problem("tag_color", _rid(row), f"tags use color words that are not booru colors: {bad} (peach is orange, golden is yellow, maroon is red)"))
    return out


def tag_value_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    items = set(_tag_items(_wordings(row).get("tags")))
    pasted = sorted({v for k in LAYOUT_KEYS if (v := _lookup(row, k).lower()) and v in items})
    if pasted:
        return [Problem("tag_value", _rid(row), f"record values pasted as tags: {pasted}; tags name what the picture shows", FLAG)]
    return []


def caption_garment_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    caption = str(_wordings(row).get("caption") or "").lower()
    if not caption:
        return []
    missing = [v for k in GARMENT_KEYS if (v := _lookup(row, k).lower()) and v not in caption]
    if missing:
        return [Problem("caption_garment", _rid(row), f"the caption leaves out the recorded garment(s): {missing}")]
    return []


STYLE_NAMES = r"(?:baroque|renaissance|rococo|gothic|impressionist|cubist|italian|dutch|flemish|french|spanish|medieval)"
SCENE_NOUNS = r"(?:garden|scene|room|kitchen|street|village|forest|field|city|harbor|beach|sky|house|building|church|meadow|cave|interior|cafe|market|shop|park|river|lake|sea|mountain)"
STYLE_ON_SCENE = re.compile(rf"\b{STYLE_NAMES} {SCENE_NOUNS}\b", re.I)


def style_on_scene_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    w = _wordings(row)
    for text in [str(w.get(k) or "") for k in ("caption", "prompt", "critique")] + [t for _, t in _thoughts(row)]:
        match = STYLE_ON_SCENE.search(text)
        if match:
            return [Problem("style_on_scene", _rid(row), f"{match.group(0)!r}: a style or period describes the painting, not the thing it shows", FLAG)]
    return []


DEPICTS = re.compile(r"\bdepicts? ([^.]+)\.", re.I)


def caption_hint_list_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A caption that is the hint's list of depicted things turned into one sentence."""
    match = DEPICTS.search(_hint(item))
    caption = str(_wordings(row).get("caption") or "")
    if not match or not caption:
        return []
    listed = [re.sub(r"^(?:a|an|the)\s+", "", part.strip()) for part in re.split(r",|\band\b", match.group(1)) if part.strip()]
    hits = [x for x in listed if x and re.search(rf"\b{re.escape(x)}s?\b", caption, re.I)]
    if len(hits) >= 3 and "," in caption:
        return [Problem("caption_hint_list", _rid(row), f"the caption lists the hint's depicted things ({hits[:3]}); describe the picture in one natural sentence", FLAG)]
    return []


# ------------------------------------------------------------------ what the second blind review found

MINOR_AGES = frozenset({"child", "teen", "baby"})
FIT_WORDS = re.compile(
    r"\b(?:fitted|sleek|tight(?=[ -](?:fit|fitting|jeans|pants|dress|top|shirt|clothes|clothing|suit|bodysuit|leggings|sweater|trousers|skirt))|tightly fitted|form-fitting|skin-tight|clingy|clinging|figure-hugging|body-hugging|curvy|shapely|snug|slim-fitting|revealing|loose(?! (?:hair|strands?|curls?|braids?|locks?|ends?|waves?|bun|ponytail))|delicate|vulnerab\w+|intimac\w+|intimate|fragile|alluring)\b",
    re.I,
)


def minor_fit_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """Fit or shape words about the clothes or body of a minor: describe them plainly."""
    person = _spec(row).get("person") or {}
    if str(person.get("age") or "").lower() not in MINOR_AGES and not (not person.get("age") and str(person.get("gender") or "").lower() in ("boy", "girl")):
        return []
    for text in _prose(row):
        match = FIT_WORDS.search(text)
        if match:
            return [Problem("minor_fit", _rid(row), f"{match.group(0)!r} describes the clothes or body of a minor; describe the clothes plainly (color, kind, pattern)")]
    return []


def deep_steps_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A deep row holds a thought labeled compare or change."""
    if row.get("depth") == "deep" and not any(step in ("compare", "change") for step, _ in _thoughts(row)):
        return [Problem("step_missing", _rid(row), "a deep row needs a thought labeled compare or change (two things set against each other, or what would change)")]
    return []


def _item_relations(item: Any, depth: Any) -> list[tuple[str, str, str]]:
    if not isinstance(item, Mapping):
        return []
    spec = (item.get("specs") or {}).get(depth) if item.get("kind") == "series" else item.get("spec")
    return [(str(r.get("a")), str(r.get("rel")), str(r.get("b"))) for r in (spec or {}).get("relation") or []]


def invented_relation_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A relation the row holds that the item did not give: writers made one up to answer a relation question."""
    if not isinstance(item, Mapping):
        return []
    given = set(_item_relations(item, row.get("depth")))
    extra = [(r.get("a"), r.get("rel"), r.get("b")) for r in _spec(row).get("relation") or [] if (str(r.get("a")), str(r.get("rel")), str(r.get("b"))) not in given]
    if extra and not given:
        a, rel, b = extra[0]
        return [Problem("invented_relation", _rid(row), f"the item holds no relation, but the row adds {a} {rel} {b}; write no relation the records do not give", FLAG)]
    return []


FLAT_MOOD = re.compile(r"\b(?:gives?|lends?|creates?|evokes?|makes it)\b", re.I)


def mood_flat_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A mood stated with a flat verb and no hedge in its sentence."""
    for sentence in _mood_sentences(row):
        match = FLAT_MOOD.search(sentence)
        if match and not HEDGE.search(sentence):
            return [Problem("mood_flat", _rid(row), f"the mood is stated flatly ({match.group(0)!r}); hedge it: suggests, seems, reads as, may, feels")]
    return []


def chain_held_key_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A chain or unknown question must ask for a key the row holds no record for."""
    held = {f"{system} {attribute}" for system, body in _spec(row).items() if system != "relation" and isinstance(body, Mapping) for attribute in body}
    for q in row.get("questions", []):
        if q.get("needs") in ("chain", "unknown") and str(q.get("key", "")) in held:
            return [Problem("question_held_key", _rid(row), f"{q.get('needs')} question {q.get('key')!r} asks for a key the row already holds as a record; ask for a key it lacks")]
    return []


UNKNOWN_CUES = {
    "thing position": re.compile(r"\b(?:in the )?(?:middle|center|centre)\b|\b(?:left|right|top|bottom|upper|lower) (?:third|side|edge|of the frame)\b", re.I),
    "scene weather": re.compile(r"\b(?:overcast|cloudy|rainy|foggy|sunny|stormy|snowy|windy|clear sky|clear weather)\b", re.I),
    "scene time": re.compile(r"\b(?:morning|evening|noon|dusk|dawn|midday|afternoon|night|sunset|sunrise)\b", re.I),
    "shot angle": re.compile(r"\b(?:high angle|low angle|eye level|eye-level|bird's-eye|worm's-eye|from above|from below)\b", re.I),
    "shot lens": re.compile(r"\b(?:wide-angle|telephoto|macro lens|fisheye|wide angle)\b", re.I),
    "scene layout": re.compile(r"\b(?:empty|sparse|crowded|cluttered|open|deserted|busy)\b", re.I),
    "form space": re.compile(r"\b(?:vast|cramped|open|spacious|enclosed|deep|shallow)\b", re.I),
    "body pose": re.compile(r"\b(?:standing|sitting|seated|kneeling|leaning|lying|crouching)\b", re.I),
}


def unknown_stated_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """An unknown question whose answer the row's own text states."""
    for q in row.get("questions", []):
        pattern = UNKNOWN_CUES.get(str(q.get("key", ""))) if q.get("needs") == "unknown" else None
        if pattern is None:
            continue
        for text in _prose(row):
            match = pattern.search(text)
            if match:
                return [Problem("unknown_stated", _rid(row), f"the unknown question {q.get('key')!r} is answered by the text ({match.group(0)!r}); ask about something the row leaves out", FLAG)]
    return []


OWN_SIDE = re.compile(r"\bto (?:his|her|their|its|(?:the|a|an) \w+'s) (?:left|right)\b", re.I)
DIRECTIONAL = ("left of", "right of", "above", "below", "behind", "in front of", "under", "over")  # beside and next to read the same both ways


def relation_reading_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A relation read backwards in prose, or a side given from a person's own point of view."""
    texts = _prose(row)
    for text in texts:
        match = OWN_SIDE.search(text)
        if match:
            return [Problem("relation_side", _rid(row), f"{match.group(0)!r} can flip the relation; say left or right of the frame or of the named thing", FLAG)]
    for relation in _spec(row).get("relation") or []:
        a, rel, b = (str(relation.get(k, "")).lower() for k in ("a", "rel", "b"))
        if rel not in DIRECTIONAL or not a or not b:
            continue
        reversed_ = re.compile(rf"\b{re.escape(b)}\b[^.;]{{0,24}}\b{re.escape(rel)}\b[^.;]{{0,16}}\b{re.escape(a)}\b", re.I)
        for text in texts:
            match = reversed_.search(text)
            if match:
                return [Problem("relation_reversed", _rid(row), f"{match.group(0)!r} reads the relation {a} {rel} {b} backwards", FLAG)]
    return []


BOORU_COUNT = re.compile(r"^\d+(?:man|men|woman|women|person|people|male|female)$")
TAG_PHRASE = re.compile(r"\b(?:mood|palette|tones?|contrast|composition|atmosphere|feeling|leading lines)\b", re.I)


def tag_booru_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    items = _tag_items(_wordings(row).get("tags"))
    bad = [t for t in items if BOORU_COUNT.match(t.replace(" ", ""))]
    out: list[Problem] = []
    if bad:
        out.append(Problem("tag_booru", _rid(row), f"tags {bad} are not booru tags; use 1girl, 1boy, 1other, solo, 2girls, 2boys, multiple girls, multiple boys"))
    phrases = [t for t in items if TAG_PHRASE.search(t)]
    if phrases:
        out.append(Problem("tag_phrase", _rid(row), f"tags {phrases[:3]} name a mood, palette or composition; tags name what the picture shows", FLAG))
    return out


MOOD_WORDS = frozenset(
    """calm gentle wistful nostalgic lonely eerie tense anxious joyful cozy awed grim stern serene tranquil melancholy somber sombre uneasy
    ominous hopeful peaceful playful solemn forlorn haunting menacing foreboding cheerful gloomy dreamy restless hushed reverent
    triumphant sad happy bleak mournful desolate threatening haunted spooky suspenseful dramatic""".split()
)
CLAIM = re.compile(r"\b(?:feels?|reads?|suggests?|seems?|mood|atmosphere|sense of|tone)\b", re.I)
CONTRAST = re.compile(r"\b(?:rather than|instead of|not|than|never|neither|nor)\b", re.I)


def second_mood_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A second mood claimed in a later sentence of the row, beyond the recorded feeling."""
    feeling = str((_spec(row).get("mood") or {}).get("feeling") or "")
    if not feeling:
        return []
    own = _stem(feeling)
    for _, text in _thoughts(row):
        for sentence in _sentences(text):
            if not CLAIM.search(sentence) or CONTRAST.search(sentence):
                continue
            words = {_stem(w) for w in _words(sentence) if w in MOOD_WORDS}
            other = sorted(w for w in words if w != own and not own.startswith(w) and not w.startswith(own))
            if other and own not in {_stem(w) for w in _words(sentence)}:
                return [Problem("second_mood", _rid(row), f"the row claims {other[0]!r} beside the recorded mood {feeling!r}; one mood only", FLAG)]
    return []


REVIEW_CHECKS: tuple[Callable[..., list[Problem]], ...] = (
    self_talk_check,
    self_hedge_check,
    thought_repeat_check,
    unrecorded_thing_check,
    unrecorded_light_check,
    unrecorded_detail_check,
    unrecorded_shot_check,
    mood_check,
    required_record_check,
    question_key_check,
    hint_contradiction_check,
    position_as_place_check,
    hint_paraphrase_check,
    single_caption_fact_check,
    step_label_check,
    tag_color_check,
    tag_value_check,
    caption_garment_check,
    style_on_scene_check,
    caption_hint_list_check,
    minor_fit_check,
    deep_steps_check,
    invented_relation_check,
    mood_flat_check,
    chain_held_key_check,
    unknown_stated_check,
    relation_reading_check,
    tag_booru_check,
    second_mood_check,
)
