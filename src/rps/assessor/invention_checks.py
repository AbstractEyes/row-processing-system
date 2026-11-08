"""Checks for facts a row states that its own records and hint do not give.

A row may only say what its records (and the item's hint) hold. These checks find the
commonest inventions a reviewer sends rows back for:

    poses          unrecorded_pose, state_conflict      stands, sits, perched; a look up under an overhead shot
    rank scope     rank_scope                           a shallower rank using a fact only a deeper rank records
    light          unrecorded_light                     soft lighting, warm low light, cool tones
    size/material  unrecorded_size, unrecorded_material narrow, towering; brick, concrete, stone
    placement      relation_placement                   "beside him" from a relation that only matches colors
    added records  added_record                         a record the writer adds at depth that the hint does not support

Each finding names the word to remove. Errors are reserved for cases a script decides precisely;
see `assessor.ERROR_WHEN_RAISED` for the codes raised to errors.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .problems import ERROR, FLAG, Problem
from .review_checks import STOP as STOPWORDS, _asserted, _content, _sentences, _thoughts, _hint, _prose, _rid, _spec, _spec_values, _stem, _words, _wordings

# ------------------------------------------------------------------ the text a row states

def _all_text(row: Mapping[str, Any]) -> list[str]:
    """Every field that states what the picture shows: thoughts, caption, prompt, critique and tags."""
    tags = _wordings(row).get("tags")
    if isinstance(tags, (list, tuple)):
        tags = ", ".join(map(str, tags))
    return [*_prose(row), str(tags or "")]


def _statements(row: Mapping[str, Any]) -> list[str]:
    """The sentences of every field that state what the picture shows (what-ifs left out)."""
    tags = _wordings(row).get("tags")
    if isinstance(tags, (list, tuple)):
        tags = ", ".join(map(str, tags))
    return [*_asserted(row), str(tags or "")]


def _rank_specs(row: Mapping[str, Any], item: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(the specs this row answers to, the specs of the other ranks of its series)."""
    own = [_spec(row)]
    others: list[Mapping[str, Any]] = []
    if isinstance(item, Mapping) and isinstance(item.get("specs"), Mapping):
        depth = row.get("depth")
        for name, spec in item["specs"].items():
            (own if name == depth else others).append(spec or {})
    elif isinstance(item, Mapping) and isinstance(item.get("spec"), Mapping):
        own.append(item["spec"])
    return own, others


def _values(spec: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for system, body in (spec or {}).items():
        if system == "relation":
            for rel in body or []:
                out.extend(str(rel.get(k, "")) for k in ("a", "rel", "b"))
        elif isinstance(body, Mapping):
            out.extend(str(v) for v in body.values())
    return out


def _own_evidence(row: Mapping[str, Any], item: Any) -> str:
    own, _ = _rank_specs(row, item)
    return (" ".join(v for spec in own for v in _values(spec)) + " " + _hint(item)).lower()


def _other_evidence(row: Mapping[str, Any], item: Any) -> str:
    _, others = _rank_specs(row, item)
    return " ".join(v for spec in others for v in _values(spec)).lower()


def _stems(text: str) -> set[str]:
    return {_stem(w) for w in _words(text)}


# ------------------------------------------------------------------ poses

NEGATION = re.compile(r"\b(?:no|not|nor|neither|never|without|nothing|isn't|aren't|doesn't|don't)\b", re.I)
IDIOM_AFTER = r"(?! (?:out|for|apart|against|in for|in contrast|as))"
POSES: dict[str, re.Pattern[str]] = {
    "stand": re.compile(rf"\b(?:stands?{IDIOM_AFTER}|standing{IDIOM_AFTER}|stood|upright)\b", re.I),
    "sit": re.compile(r"\b(?:sits?|sitting|seated|sat|perch(?:es|ed|ing)?|squat\w*)\b(?! (?:in front|behind|beside|next|to the|at the|on the|above|below|between|near|along|against|beyond|just|a little|slightly|low|high|inside))", re.I),
    "kneel": re.compile(r"\bkneel\w*\b", re.I),
    "crouch": re.compile(r"\bcrouch\w*\b", re.I),
    "lie": re.compile(r"\b(?:lies|lying|lay|reclin\w+|sprawl\w*)\b", re.I),
    "lean": re.compile(r"\b(?:leans?|leaning|leaned)\b", re.I),
    "walk": re.compile(r"\b(?:walks?|walking|strolls?|strolling)\b", re.I),
    "run": re.compile(r"\b(?:runs?|running|sprint\w*)\b", re.I),
    "climb": re.compile(r"\b(?:climbs?|climbing|ascend\w*)\b", re.I),
    "jump": re.compile(r"\b(?:jumps?|jumping|leaps?|leaping|mid-?air)\b", re.I),
    "bow": re.compile(r"\b(?:bows?|bowing|bowed)\b", re.I),
}
POSE_WORDS = {  # words in a record that name a pose family (the record may say "stand" or "standing")
    "stand": ("stand", "upright"), "sit": ("sit", "seat", "perch", "squat"), "kneel": ("kneel",), "crouch": ("crouch",),
    "lie": ("lie", "lying", "lay", "reclin", "sprawl"), "lean": ("lean",), "walk": ("walk", "stroll"), "run": ("run", "sprint"),
    "climb": ("climb", "ascend"), "jump": ("jump", "leap"), "bow": ("bow",),
}
# Things that stand, sit or lean without being posed: a pose word right after one of them is about the thing.
INANIMATE = frozenset(
    """lamp lamps pole poles tree trees wall walls fence fences gate gates door doors window windows building buildings tower towers
    sign signs post posts column columns statue statues car cars tire pot pots table tables bench benches chair chairs sofa couch
    shelf shelves bottle bottles basket baskets rope umbrella bridge boat rock rocks mountain mountains house houses church lantern
    streetlight light lights kite kites fountain ladder bike bicycle stool vase cup cups book books sculpture monument skyscraper
    skyscrapers star stars flag flags cabinet frame frames television plant plants flower flowers sunflower sunflowers bush""".split()
)
# People and animals: the ones a pose word is about.
ANIMATE = frozenset(
    """he she they boy girl man woman child person figure people boys girls men women kid kids youth teen baby elephant
    pigeon bird birds cat cats dog dogs horse horses cow cows deer sheep bear lion tiger fox rabbit duck ducks goose swan owl heron
    crane monkey monkeys ape wolf gull gulls nonbinary someone sitter elephants giraffe zebra camel goat pig hen rooster
    chicken eagle hawk crow parrot seagull squirrel mouse rat frog turtle lizard snake butterfly lamb calf pony donkey ox dragon
    figures youths children persons""".split()
)
LOOK_UP = re.compile(r"\b(?:looks?|looking|gazes?|gazing|stares?|staring|glances?|glancing|eyes?)\s+(?:up|upward|upwards|overhead|skyward)\b|\bascend\w* upward\b|\blooking to the sky\b", re.I)
OVERHEAD_WORDS = ("overhead", "top-down", "top down", "bird's-eye", "birds eye", "bird's eye", "from above", "aerial", "high angle")


def _family_held(family: str, evidence: str) -> bool:
    return any(w in evidence for w in POSE_WORDS[family])


CLAUSE_BREAK = frozenset({"which", "that", "where", "while", "whose", "who", "and", "but", "so", "because", "as"})


def _subject_ok(sentence: str, start: int, end: int, word: str) -> bool:
    """True when a person or an animal is the one posed ("a boy with a bun stands", "a standing figure")."""
    window: list[str] = []
    for w in reversed(_words(sentence[:start])[-8:]):
        if w in CLAUSE_BREAK:
            break
        window.append(w)
    window.reverse()
    animate = [i for i, w in enumerate(window) if w in ANIMATE]
    inanimate = [i for i, w in enumerate(window) if w in INANIMATE]
    if animate and (not inanimate or animate[-1] > inanimate[-1]):
        return True
    after = _words(sentence[end:])[:2]
    return word.lower().endswith("ing") and any(w in ANIMATE for w in after) and bool(window) and window[-1] in ("a", "an", "the")


def pose_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A pose word no record or hint gives, in any text of the row, about a person or an animal."""
    own = _own_evidence(row, item)
    other = _other_evidence(row, item)
    everything = own + " " + other
    out: list[Problem] = []
    for text in _statements(row):
        for sentence in re.split(r"(?<=[.!?;])\s+", text):
            for family, pattern in POSES.items():
                for match in pattern.finditer(sentence):
                    if _family_held(family, own):
                        continue
                    if NEGATION.search(sentence[max(0, match.start() - 28) : match.start()]):
                        continue
                    if not _subject_ok(sentence, match.start(), match.end(), match.group(0)):
                        continue
                    word = match.group(0)
                    if _family_held(family, other):
                        return [Problem("unrecorded_pose", _rid(row), f"{word!r}: this rank records no such pose (a deeper rank of the series does); remove {word!r}")]
                    recorded = _recorded_pose(row, item)
                    if recorded:
                        return [Problem("state_conflict", _rid(row), f"{word!r} conflicts with the recorded pose {recorded!r}; remove {word!r}")]
                    return [Problem("unrecorded_pose", _rid(row), f"{word!r}: no pose is recorded; remove {word!r}")]
    up = next((m for t in _statements(row) for m in [LOOK_UP.search(t)] if m), None)
    if up and any(w in everything for w in OVERHEAD_WORDS):
        return [Problem("state_conflict", _rid(row), f"{up.group(0)!r} contradicts the overhead shot; remove {up.group(0)!r}")]
    return out


def _recorded_pose(row: Mapping[str, Any], item: Any) -> str:
    own, others = _rank_specs(row, item)
    for spec in [*own, *others]:
        pose = str((spec.get("body") or {}).get("pose") or (spec.get("thing") or {}).get("state") or "")
        if pose:
            return pose
    return ""


# ------------------------------------------------------------------ rank scope

GENERIC = frozenset("""person people medium name count scene close light time frame view shot photograph painting illustration picture image wide full
    low high left right center centre front back side body top bottom upper lower small large long short late early young old one""".split())


def rank_scope_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A rank that uses a fact only a deeper (or shallower) rank of its series records."""
    if not (isinstance(item, Mapping) and isinstance(item.get("specs"), Mapping) and len(item["specs"]) > 1):
        return []
    own = _stems(_own_evidence(row, item))
    _, others = _rank_specs(row, item)
    candidates: dict[str, str] = {}
    for spec in others:
        skip = {_stem(w) for r in spec.get("relation") or [] for w in _words(str(r.get("rel", "")))}
        for value in _values(spec):
            for w in _words(value):
                if _stem(w) in skip:
                    continue
                st = _stem(w)
                if len(w) >= 5 and w not in STOPWORDS and w not in GENERIC and st not in own:
                    candidates.setdefault(st, w)
    if not candidates:
        return []
    for text in _statements(row):
        for w in _words(text):
            st = _stem(w)
            if st in candidates and st not in own:
                return [Problem("rank_scope", _rid(row), f"{w!r} is recorded only at another rank of this series; this rank may not use it; remove {w!r}", ERROR)]
    return []


# ------------------------------------------------------------------ light, size, material

LIGHTING = re.compile(
    r"\b(?:(?:soft|hard|harsh|warm|cool|gentle|diffuse\w*|even|natural|ambient|dim|bright|low|strong|golden)(?: (?:natural|outdoor|window|indoor|room|afternoon|evening))*,? (?:light|lighting)"
    r"|(?:natural|outdoor|indoor|room|ambient|soft|window) light(?:ing)?|(?:warm|cool) tones|(?:warm|cool|soft) glow)\b",
    re.I,
)
LIGHT_RECORDS = ("light", "lit", "sun", "lamp", "lantern", "candle", "glow", "shadow", "golden", "dusk", "dawn", "night", "evening", "morning", "midnight", "candlelight", "spotlight", "daylight", "overcast", "bright", "hour", "sunset", "sunrise", "twilight", "noon", "afternoon")


def light_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A quality of light ("soft lighting", "warm low light", "cool tones") that no light record or hint gives."""
    evidence = _own_evidence(row, item)
    if (_spec(row).get("shot") or {}).get("light") or any(w in evidence for w in LIGHT_RECORDS):
        return []
    for text in _statements(row):
        match = LIGHTING.search(text)
        if match:
            return [Problem("unrecorded_light", _rid(row), f"light that no record or hint holds ({match.group(0)!r}); remove {match.group(0)!r}", ERROR)]
    return []


SIZE_WORDS = re.compile(r"\b(?:narrow|massive|towering|enormous|giant|vast|sprawling|tiny|huge|cramped|spacious)\b", re.I)
MATERIAL_WORDS = re.compile(r"\b(?:brick|bricks|concrete|cement|stone|marble|steel|iron|glass|plastic|wooden|wood|metal|metallic|leather|ceramic|tiled|cobblestone|asphalt|gravel|sandstone)\b", re.I)


def _word_check(row: Mapping[str, Any], item: Any, pattern: re.Pattern[str], code: str, noun: str, severity: str) -> list[Problem]:
    evidence = _stems(_own_evidence(row, item))
    for text in _statements(row):
        for match in pattern.finditer(text):
            if _stem(match.group(0)) not in evidence:
                return [Problem(code, _rid(row), f"{noun} {match.group(0)!r} is not in a record or the hint; remove {match.group(0)!r}", severity)]
    return []


def size_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    return _word_check(row, item, SIZE_WORDS, "unrecorded_size", "the size word", FLAG)


def material_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    return _word_check(row, item, MATERIAL_WORDS, "unrecorded_material", "the material", FLAG)


# ------------------------------------------------------------------ placement from a relation that places nothing

NON_SPATIAL = ("same colour as", "same color as", "smaller than", "larger than", "bigger than", "taller than", "shorter than", "matches", "looking at", "holding")
SPATIAL = re.compile(r"\b(?:beside|adjacent(?: to)?|next to|behind|in front of|alongside|at (?:his|her|their) (?:side|feet))\b", re.I)


def placement_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A place ("beside him", "the fence behind") written for two things whose only relation places neither."""
    if _hint(item):
        return []
    relations = _spec(row).get("relation") or []
    spatial_things: set[str] = set()
    plain: set[str] = set()
    for r in relations:
        ends = {_stem(str(r.get("a", ""))), _stem(str(r.get("b", "")))}
        (plain if str(r.get("rel", "")).lower() in NON_SPATIAL else spatial_things).update(ends)
    plain -= spatial_things
    if not plain:
        return []
    for text in _statements(row):
        for sentence in re.split(r"(?<=[.!?;])\s+|, |; | and | but ", text):
            match = SPATIAL.search(sentence)
            if not match:
                continue
            words = {_stem(w) for w in _words(sentence)}
            named = [t for t in plain if t in words]
            if named and not words & spatial_things:
                return [Problem("relation_placement", _rid(row), f"{match.group(0)!r}: the relation on {named[0]!r} does not place it; remove {match.group(0)!r}", ERROR)]
    return []


# ------------------------------------------------------------------ records the writer adds

WRITER_SYSTEMS = frozenset({"mood", "intent", "story"})  # records every medium or deep row adds by design
# The records a hint states plainly or not at all; the others (medium, position, colors) are read off the picture.
HINT_BOUND = frozenset({"scene setting", "scene weather", "scene time", "form space", "form contrast", "shot sharpness"})
SKY_CLOUDY = ("cloud", "overcast", "rain", "fog", "storm", "gray sky", "grey sky", "mist", "snow")


def _given_spec(row: Mapping[str, Any], item: Any) -> Mapping[str, Any]:
    if not isinstance(item, Mapping):
        return {}
    if isinstance(item.get("specs"), Mapping):
        return item["specs"].get(row.get("depth")) or item["specs"].get("deep") or {}
    return item.get("spec") or {}


def added_record_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """On an item with a hint, a record the writer adds that the hint does not support (or contradicts)."""
    hint = _hint(item).lower()
    if not hint:
        return []
    given = _given_spec(row, item)
    hint_stems = _stems(hint)
    for system, body in _spec(row).items():
        if system in WRITER_SYSTEMS or system == "relation" or not isinstance(body, Mapping):
            continue
        for attr, value in body.items():
            if f"{system} {attr}" not in HINT_BOUND or (isinstance(given.get(system), Mapping) and attr in given[system]):
                continue
            words = {_stem(w) for w in _words(str(value)) if w not in STOPWORDS and len(w) > 2}
            if not words:
                continue
            if system == "scene" and attr == "weather" and str(value).lower() in ("clear", "sunny") and any(w in hint for w in SKY_CLOUDY):
                return [Problem("added_record", _rid(row), f"the record {system} {attr}: {value!r} contradicts the hint (clouds or rain); drop the record")]
            if not words & hint_stems and not (system == "scene" and attr == "weather"):
                return [Problem("added_record", _rid(row), f"the added record {system} {attr}: {value!r} is not in the hint; drop the record and what rests on it")]
    return []


# ------------------------------------------------------------------ names taken from the hint

PLAIN_ADJECTIVES = frozenset(
    """adult behind landscape mortar through large small big tiny little old new young tall long short wide narrow dark light bright pale deep heavy thin thick round flat
    open closed single lone solid plain fresh dry wet clean dirty empty full half whole front back left right top bottom upper lower
    first last second third white black red blue green yellow orange purple pink brown gray grey silver gold golden beige tan cream
    wooden metal metallic plastic stone glass iron steel paper cloth leather rusty worn broken cracked white-and-black many few
    several multiple modern ancient classic simple fancy colorful colourful shiny glossy matte smooth rough soft hard sharp blurry
    clear cloudy sunny rainy snowy foggy misty distant nearby far close tight loose upright standing sitting hanging lit unlit""".split()
)


def hint_name_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A species or place name taken from the hint ("pajama cardinal fish") when no record holds it."""
    hint = _hint(item).lower()
    thing = str((_spec(row).get("thing") or {}).get("name") or "").lower()
    if not hint or not thing or " " in thing or (thing not in ANIMATE and thing not in ANIMALS_EXTRA):
        return []
    held = _stems(" ".join(_values(_spec(row))))
    for match in re.finditer(r"((?:[a-z]+ ){1,2})" + re.escape(thing) + r"s?"+chr(92)+"b", hint):
        for w in match.group(1).split():
            if w in PLAIN_ADJECTIVES or w in STOPWORDS or len(w) < 5 or _stem(w) in held or w in COLOR_NAMES:
                continue
            if any(re.search(rf"{chr(92)}b{re.escape(w)}{chr(92)}b", t, re.I) for t in _all_text(row)):
                return [Problem("hint_name", _rid(row), f"{w!r} is a name the hint gives for the {thing}; no record holds it, so say {thing!r} and describe it; remove {w!r}")]
    return []


ANIMALS_EXTRA = frozenset("fish shark whale dolphin kitten puppy cub insect bee bug beetle spider ant seal otter".split())
COLOR_NAMES = frozenset("""maroon burgundy crimson scarlet teal navy turquoise indigo violet lavender magenta olive mustard khaki ivory coral salmon""".split())

# ------------------------------------------------------------------ leaning on the hint

def caption_hint_copy_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A caption that restates the hint's first sentence."""
    sentences = _sentences(_hint(item))
    caption = _content(str(_wordings(row).get("caption") or ""))
    if len(sentences) < 2 or len(caption) < 6:
        return []
    first = _content(sentences[0])
    if len(caption & first) / len(caption) >= 0.7:
        return [Problem("caption_hint_copy", _rid(row), "the caption restates the hint's first sentence; describe the picture in your own words")]
    return []


def hint_order_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """Thoughts that take the hint's sentences one after the other (three or more in a row)."""
    hint = _sentences(_hint(item))
    if len(hint) < 4:
        return []
    hint_sets = [_content(s) for s in hint]
    order: list[int] = []
    for step, text in _thoughts(row):
        if step in ("see", "mood", "check", "story", "intent"):
            continue
        for sentence in _sentences(text):
            words = _content(sentence)
            if len(words) < 4:
                continue
            best = max(range(len(hint)), key=lambda k: len(words & hint_sets[k]))
            if len(words & hint_sets[best]) >= 3:
                order.append(best)
    run = 1
    for a, b in zip(order, order[1:]):
        run = run + 1 if b > a else 1
        if run >= 3:
            return [Problem("hint_order", _rid(row), "the thoughts take the hint's sentences in the hint's order; read the picture in your own order", FLAG)]
    return []


def thought_hint_copy_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    """A thought sentence that is mostly one hint sentence's content words (error), or a caption that compresses a one-sentence hint (flag)."""
    hint = _sentences(_hint(item))
    if not hint:
        return []
    hint_sets = [_content(s) for s in hint]
    out: list[Problem] = []
    for index, (_, text) in enumerate(_thoughts(row), 1):
        for sentence in _sentences(text):
            words = _content(sentence)
            if len(words) >= 4 and max(len(words & h) for h in hint_sets) / len(words) >= 0.7:
                out.append(Problem("thought_hint_copy", _rid(row), f"thought {index} repeats one of the hint's sentences; say what the picture shows in your own words, or what it means"))
                break
        if out:
            break
    caption = _content(str(_wordings(row).get("caption") or ""))
    if len(hint) == 1 and len(caption) >= 6 and len(caption & hint_sets[0]) / len(caption) >= 0.8:
        out.append(Problem("caption_compresses_hint", _rid(row), "the caption compresses the hint's one sentence; describe the picture in your own words", FLAG))
    return out


INVENTION_CHECKS = (
    pose_check, rank_scope_check, light_check, size_check, material_check, placement_check, added_record_check, hint_name_check,
    caption_hint_copy_check, hint_order_check, thought_hint_copy_check,
)
