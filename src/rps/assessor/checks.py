"""Row checks the validator cannot make.

Each check is a function from a stored row (and sometimes its spec item) to a list of
`Problem`s. Codes are short lower-case names; the rule pool says which rule each guards.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from .problems import ERROR, FLAG, Problem

GENRES = frozenset(
    {
        "portrait", "landscape", "still life", "street", "documentary", "fashion", "product", "architecture", "wildlife",
        "abstract", "sport", "interior", "aerial", "macro", "war", "history piece", "genre scene", "self portrait", "cityscape",
        "seascape", "nude study", "botanical", "scientific", "environmental portrait", "figure study", "street portrait",
    }
)
PHOTO_MEDIA = frozenset({"photograph", "film", "polaroid", "tintype", "daguerreotype", "silver print", "cyanotype"})

SPEC_LANGUAGE = re.compile(
    r"\b(?:the )?(?:named|given|specified|listed|stated) (?:source|light|colou?r|action|record|pose|setting|value|key|angle|lens|framing|hour)\b"
    r"|\bthe spec\b|\bthe records?\b|\bgiven records?\b",
    re.I,
)
ADVICE = re.compile(
    r"\b(?:A|The) (?:painter|photographer|illustrator|artist|sculptor|designer) (?:would|could|might|should)\b"
    r"|\bwould (?:crop|tighten|move|raise|lower)\b"
)
FORMULA_HARD = re.compile(r"\b(?:inference|inferred|read from)\b", re.I)
FORMULA_SOFT = re.compile(r"\b(?:record|spec)\b", re.I)
BRITISH = re.compile(
    r"\b(colour\w*|grey\w*|centre\w*|centred|favourite\w*|jewellery|harbour\w*|neighbour\w*|behaviour\w*|armour\w*|catalogue\w*"
    r"|theatre\w*|metres?|fibre\w*|cosy|pyjamas|travell(?:er|ers|ed|ing)|(?:organis|realis|recognis)(?:e|ed|es|ing|ation|ations))\b",
    re.I,
)
STYLE_WORDS = re.compile(
    r"\b(renaissance|baroque|romantic(?:ism)?|symbolis\w+|impressionis\w+|realis[mt]|rococo|gothic|cubis\w+|italian|french|dutch|german"
    r"|flemish|japanese|chinese|greek|roman|victorian|edwardian|medieval|neoclassical\w*|expressionis\w+|surrealis\w+|art nouveau"
    r"|art deco|mannerism|mannerist)\b"
)
# A thought that opens with a step name or a counting label and a colon ("Count one:", "Colour:", "Light:").
LABEL_OPENER = re.compile(
    r"^\s*(?:count\b[\w' -]{0,12}|(?:see|name|place|relate|form|light|colou?r|mood|mean|intend|story|compare|change|check|say|record)):\s",
    re.I,
)
# A short thought that defines a term ("Robe: a long outer garment.", "Turquoise is a blue-green.") and points at nothing in the picture.
GLOSS = re.compile(
    r"^\s*(?:[A-Z][\w'’-]*(?: [\w'’-]+){0,2}:\s+(?:an?|the)\s"
    r"|(?!(?:The|She|He|Her|His|They|It|There|This|That|These|Those|Both|Each|One|Two|Three|Four|Five|Six)\b)[A-Z][a-z'’-]+(?: [a-z'’-]+)? (?:is|are|means|refers to) (?:an?|the|any|one) )"
)
GLOSS_POINTS_AT_PICTURE = re.compile(
    r"\b(?:this|these|here|picture|frame|scene|view|shot|image|she|he|they|them|her|his|their|its|it|him|someone|person|man|woman|hers|behind|beside|across|above|below)\b", re.I
)
GLOSS_MAX_LENGTH = 90
DIRECTION_END = re.compile(
    r"^(?:the )?(?:(?:top|bottom|upper|lower|far|near)[ -])?"
    r"(?:left|right|center|centre|middle|foreground|background|side|edge|corner|top|bottom|frame|front|back|distance|horizon)"
    r"(?:[ -](?:side|edge|corner|half|third|of the frame|of frame))*$"
)
COLOR_WORDS = (
    "red orange yellow green blue purple pink brown black white gray grey gold golden silver beige tan navy teal turquoise crimson "
    "scarlet maroon violet lavender lilac magenta cream ivory olive mustard peach mint rust plum amber indigo charcoal emerald "
    "auburn blonde ginger"
).split()
COLOR_FAMILY = {"gold": "golden", "golden": "golden", "grey": "gray", "gray": "gray", "blonde": "blonde"}
COLOR_RE = re.compile(r"\b(" + "|".join(COLOR_WORDS) + r")\b")
GARMENT_WORDS = {
    "dress": "dress", "gown": "gown", "skirt": "skirt", "shirt": "shirt", "t-shirt": "shirt", "blouse": "blouse", "sweater": "sweater",
    "hoodie": "hoodie", "jacket": "jacket", "coat": "coat", "trousers": "trousers", "pants": "trousers", "jeans": "jeans",
    "denim": "jeans", "shorts": "shorts", "leggings": "leggings", "boots": "boots", "sneakers": "sneakers", "sandals": "sandals",
    "heels": "heels", "hat": "hat", "scarf": "scarf", "gloves": "gloves", "suit": "suit", "vest": "vest", "cardigan": "cardigan",
    "blazer": "blazer", "parka": "parka", "robe": "robe", "kimono": "kimono", "uniform": "uniform",
}
GARMENT_RE = re.compile(r"\b(" + "|".join(sorted(GARMENT_WORDS, key=len, reverse=True)) + r")\b")
PASSERBY = re.compile(r"\b(someone|somebody|a passer-?by|passers-?by|a visitor|a viewer|a walker|a pedestrian)\b", re.I)
CONDITIONAL = re.compile(r"\b(might|may|could|would|perhaps|maybe|if|imagine\w*|possibly)\b", re.I)
PERSON_STRONG = re.compile(
    r"\b(humans?|busts?|likenesses|likeness|sitters?|shrouds?|satyrs?|nymphs?|soldier statues?|statues? of (?:a )?(?:man|woman|soldier))\b",
    re.I,
)
PERSON_WEAK = re.compile(
    r"\b(figures?|portraits?(?! (?:orientation|format|mode|layout))|self-portraits?|(?<!clock )(?<!watch )faces?|profiles?|men|man|women|woman|boys?|girls?|child(?:ren)?|people|persons?|lady|ladies|gentlem[ae]n|"
    r"king|queen|monk|saint|priest|knight|soldier|farmer|worker|nurse|angel|goddess|god|warrior|dancer|rider|musician)\b",
    re.I,
)
MEDIUM_FOR_PHOTO = "photograph"


def row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id", "?"))


def thought_texts(row: Mapping[str, Any]) -> list[str]:
    return [str(t.get("text", "")) if isinstance(t, Mapping) else str(t) for t in row.get("thoughts", [])]


def wordings(row: Mapping[str, Any]) -> Mapping[str, Any]:
    return row.get("wordings") or {}


def prose_texts(row: Mapping[str, Any]) -> list[str]:
    """Thoughts plus caption, prompt and critique (not tags)."""
    w = wordings(row)
    return thought_texts(row) + [str(w.get(k) or "") for k in ("caption", "prompt", "critique")]


def all_texts(row: Mapping[str, Any]) -> list[str]:
    w = wordings(row)
    return thought_texts(row) + [str(v) if isinstance(v, str) else " ".join(map(str, v)) for v in w.values()]


def spec_values(spec: Mapping[str, Any]) -> list[str]:
    """Every value in a spec, relation ends included, lower-cased."""
    out: list[str] = []
    for system, body in spec.items():
        if system == "relation":
            for rel in body:
                out.extend(str(rel.get(k, "")).lower() for k in ("a", "rel", "b"))
        elif isinstance(body, Mapping):
            out.extend(str(v).lower() for v in body.values())
    return out


# ------------------------------------------------------------------ the original eight


def genre_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    genre = row.get("genre")
    if genre in GENRES:
        return []
    return [Problem("genre_off_list", row_id(row), f"the genre {genre!r} is not on the genre list")]


def spec_language_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    for text in all_texts(row):
        match = SPEC_LANGUAGE.search(text)
        if match:
            return [Problem("spec_language", row_id(row), f"the text talks about the records ({match.group(0)!r}); say what the picture shows")]
    return []


def advice_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    for step in row.get("thoughts", []):
        text = str(step.get("text", ""))
        match = ADVICE.search(text)
        if match:
            return [Problem("advice", row_id(row), f"a thought gives advice ({match.group(0)!r}); advice belongs in the critique")]
    return []


def formula_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    out: list[Problem] = []
    for text in thought_texts(row):
        hard = FORMULA_HARD.search(text)
        if hard:
            out.append(Problem("formula_word", row_id(row), f"meta-language in a thought ({hard.group(0)!r}); name the evidence in the picture's terms"))
            break
    else:
        for text in thought_texts(row):
            soft = FORMULA_SOFT.search(text)
            if soft:
                out.append(Problem("formula_word", row_id(row), f"the word {soft.group(0)!r} in a thought; fine as a purpose noun, not as talk about the data", FLAG))
                break
    return out


def domain_medium_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    if (row.get("source") or {}).get("kind") != "seeded":
        return []
    medium = ((row.get("spec") or {}).get("form") or {}).get("medium")
    if not medium or medium == "none":
        return []
    domain = row.get("domain")
    if (domain == "photograph") != (medium in PHOTO_MEDIA) and not (domain == "photograph" and medium == "none"):
        return [Problem("domain_medium", row_id(row), f"the domain is {domain!r} but the medium record is {medium!r}", FLAG)]
    return []


def wording_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    w = wordings(row)
    depth = row.get("depth")
    need = {"medium": ("caption", "prompt", "tags"), "deep": ("caption", "prompt", "tags", "critique")}.get(depth, ())
    missing = [kind for kind in need if not w.get(kind)]
    if missing:
        return [Problem("wording_missing", row_id(row), f"a {depth} row carries {', '.join(need)}; missing {', '.join(missing)}")]
    return []


RELATION_KEY = re.compile(r"^rel(\d+) (a|rel|b)$")


def relation_key_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    out: list[Problem] = []
    relations = (row.get("spec") or {}).get("relation") or []
    for q in row.get("questions", []):
        key = str(q.get("key", ""))
        if not key.startswith("rel"):
            continue
        match = RELATION_KEY.match(key)
        if not match:
            out.append(Problem("relation_key", row_id(row), f"a relation question is asked as 'rel1 a', 'rel1 rel' or 'rel1 b', not {key!r}"))
        elif int(match.group(1)) > len(relations):
            out.append(Problem("relation_key", row_id(row), f"question {key!r} asks about a relation the row does not hold"))
    return out


def spelling_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    texts = all_texts(row) + [str(q.get("value", "")) for q in row.get("questions", []) if q.get("needs") != "record"]
    found = sorted({m.group(0).lower() for t in texts for m in BRITISH.finditer(t)})
    if found:
        return [Problem("us_spelling", row_id(row), f"use American spelling in prose and tags: {found}")]
    return []


def style_case_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    found = sorted({m for t in prose_texts(row) for m in STYLE_WORDS.findall(t)})
    if found:
        return [Problem("style_lowercase", row_id(row), f"capitalize style, period and nationality names in prose: {found}")]
    return []


def garment_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    spec = row.get("spec") or {}
    if "person" not in spec or row.get("depth") == "simple":
        return []
    w = wordings(row)
    caption = str(w.get("caption") or "").lower()
    tags = str(w.get("tags") or "").lower()
    thoughts = json.dumps(row.get("thoughts", [])).lower()
    garments = [spec[k].get("name") for k in ("upper", "lower", "outer", "shoes") if k in spec and spec[k].get("name")]
    clothing = (spec.get("wear") or {}).get("clothing")
    if clothing:
        garments.append(clothing)
    out: list[Problem] = []
    for name in garments:
        if name not in thoughts and name not in caption:
            out.append(Problem("garment_missing_text", row_id(row), f"the recorded garment {name!r} is not named in the thoughts or the caption"))
        if name != "jeans" and name not in tags and name.replace(" ", "") not in tags.replace(" ", ""):
            out.append(Problem("garment_missing_tags", row_id(row), f"the recorded garment {name!r} is not in the tags"))
    if re.search(r"\ba dress\b", caption) and clothing != "dress":
        out.append(Problem("dress_caption", row_id(row), "the caption says 'a dress' but no one-piece garment is recorded"))
    if re.search(r"(^|,\s*)jeans(,|$)", tags):
        out.append(Problem("jeans_tag", row_id(row), "'jeans' is not allowed as a tag item; write denim or blue pants"))
    return out


# ------------------------------------------------------------------ the gap checks


def label_opener_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    for step in row.get("thoughts", []):
        text = str(step.get("text", ""))
        if LABEL_OPENER.match(text):
            return [Problem("label_opener", row_id(row), f"a thought opens with a label and a colon ({text[:30]!r}); say it as the picture")]
    return []


def gloss_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    for step in row.get("thoughts", []):
        text = str(step.get("text", ""))
        if GLOSS.match(text) and len(text) <= GLOSS_MAX_LENGTH and not GLOSS_POINTS_AT_PICTURE.search(text):
            return [Problem("gloss", row_id(row), f"a thought may define a word instead of looking at this picture ({text[:40]!r})", FLAG)]
    return []


def relation_direction_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    out: list[Problem] = []
    for number, rel in enumerate((row.get("spec") or {}).get("relation") or [], 1):
        for end in ("a", "b"):
            value = str(rel.get(end, "")).strip().lower()
            if DIRECTION_END.match(value):
                out.append(Problem("relation_direction", row_id(row), f"relation {number} has {value!r} as an end; an end names a thing, not a direction or part of the frame"))
    return out


def person_in_hint_check(row: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> list[Problem]:
    """A seeded item whose hint implies a person the records leave out."""
    if not isinstance(item, Mapping) or not item.get("seed_id") or not item.get("hint"):
        return []
    if "person" in (row.get("spec") or {}):
        return []
    hint = str(item["hint"])
    strong = PERSON_STRONG.search(hint)
    if strong:
        return [Problem("person_in_hint", row_id(row), f"the hint implies a person the records leave out ({strong.group(0)!r}); skip the item", ERROR)]
    weak = PERSON_WEAK.search(hint)
    if weak:
        return [Problem("person_in_hint", row_id(row), f"the hint may imply a person the records leave out ({weak.group(0)!r})", FLAG)]
    return []


def unrecorded_color_check(row: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> list[Problem]:
    spec = row.get("spec") or {}
    evidence = " ".join(spec_values(spec))
    if isinstance(item, Mapping):
        evidence += " " + str(item.get("hint", "")).lower()
    held = {COLOR_FAMILY.get(w, w) for w in COLOR_RE.findall(evidence)}
    palette = ((spec.get("form") or {}).get("palette") or "").lower()
    held |= {COLOR_FAMILY.get(w, w) for w in COLOR_RE.findall(palette)}
    seen = {COLOR_FAMILY.get(w, w) for t in prose_texts(row) for w in COLOR_RE.findall(t.lower())}
    stray = sorted(seen - held)
    if stray:
        return [Problem("unrecorded_color", row_id(row), f"colors in the text that no record holds: {stray}", FLAG)]
    return []


def unrecorded_garment_check(row: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> list[Problem]:
    spec = row.get("spec") or {}
    if "person" not in spec:
        return []
    evidence = " ".join(spec_values(spec))
    held = {GARMENT_WORDS[w] for w in GARMENT_RE.findall(evidence)}
    for value in spec_values(spec):
        for word, canon in GARMENT_WORDS.items():
            if word in value:
                held.add(canon)
    seen = {GARMENT_WORDS[w] for t in prose_texts(row) for w in GARMENT_RE.findall(t.lower())}
    stray = sorted(seen - held)
    if stray:
        return [Problem("unrecorded_garment", row_id(row), f"garments in the text that no record holds: {stray}", FLAG)]
    return []


def passerby_check(row: Mapping[str, Any], item: Any = None) -> list[Problem]:
    if "person" in (row.get("spec") or {}):
        return []
    for step in row.get("thoughts", []):
        for sentence in re.split(r"(?<=[.!?])\s+", str(step.get("text", ""))):
            if PASSERBY.search(sentence) and not CONDITIONAL.search(sentence):
                return [Problem("passerby_present", row_id(row), f"an imagined passer-by is in the present tense ({sentence[:50]!r}); keep it conditional", FLAG)]
    return []


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def hint_copy_check(row: Mapping[str, Any], item: Mapping[str, Any] | None = None, run: int = 10) -> list[Problem]:
    """A run of `run` words that appears in both the hint and the row's text."""
    if not isinstance(item, Mapping) or not item.get("hint"):
        return []
    hint = _tokens(str(item["hint"]))
    grams = {tuple(hint[i : i + run]) for i in range(len(hint) - run + 1)}
    for text in all_texts(row):
        words = _tokens(text)
        for i in range(len(words) - run + 1):
            if tuple(words[i : i + run]) in grams:
                return [Problem("hint_copy", row_id(row), f"a run of {run} words is copied from the hint ({' '.join(words[i:i + run])!r})", FLAG)]
    return []


ROW_CHECKS: tuple[Callable[..., list[Problem]], ...] = (
    genre_check,
    spec_language_check,
    advice_check,
    formula_check,
    domain_medium_check,
    wording_check,
    relation_key_check,
    spelling_check,
    style_case_check,
    garment_check,
    label_opener_check,
    gloss_check,
    relation_direction_check,
    person_in_hint_check,
    unrecorded_color_check,
    unrecorded_garment_check,
    passerby_check,
    hint_copy_check,
)


def run_row_checks(row: Mapping[str, Any], item: Mapping[str, Any] | None = None) -> list[Problem]:
    out: list[Problem] = []
    for check in ROW_CHECKS:
        out.extend(check(row, item))
    return out


# ------------------------------------------------------------------ the call's assignment


def _unknown_questions(row: Mapping[str, Any]) -> list[str]:
    return [str(q.get("key", "")) for q in row.get("questions", []) if q.get("needs") == "unknown"]


def assignment_check(row: Mapping[str, Any], assignment: Mapping[str, Any] | None) -> list[Problem]:
    """Check a row against what the writer was told to do for this call (None: no choices were made)."""
    if not assignment:
        return []
    out: list[Problem] = []
    rid, depth = row_id(row), str(row.get("depth"))
    part = assignment.get("relation_part")
    asked = [m.group(2) for q in row.get("questions", []) if (m := RELATION_KEY.match(str(q.get("key", ""))) ) and m.group(1) == "1"]
    if part and asked and part not in asked:
        out.append(Problem("assigned_relation_part", rid, f"the relation question was assigned as 'rel1 {part}'; this row asks {', '.join(repr('rel1 ' + a) for a in asked)}"))
    key = (assignment.get("unknown_keys") or {}).get(depth)
    if key:
        asked_unknown = _unknown_questions(row)
        system, _, attr = key.partition(" ")
        held = (row.get("spec") or {}).get(system)
        if isinstance(held, Mapping) and held.get(attr) is not None:
            out.append(Problem("assigned_unknown_held", rid, f"the assigned unknown key {key!r} must not be recorded in the row's spec"))
        if asked_unknown and key not in asked_unknown:
            out.append(Problem("assigned_unknown_key", rid, f"the unknown question was assigned as {key!r}; this row asks {', '.join(repr(k) for k in asked_unknown)}"))
        elif not asked_unknown:
            out.append(Problem("assigned_unknown_missing", rid, f"no unknown question; {key!r} was assigned", FLAG))
    kinds = (assignment.get("wordings") or {}).get(depth)
    if kinds is not None:
        w = wordings(row)
        missing = [k for k in kinds if not w.get(k)]
        extra = sorted(k for k in w if w.get(k) and k not in kinds)
        if missing:
            out.append(Problem("assigned_wordings", rid, f"a {depth} row was assigned the wordings {', '.join(kinds)}; missing {', '.join(missing)}"))
        if extra:
            out.append(Problem("assigned_wordings_extra", rid, f"wordings beyond the assignment for a {depth} row: {', '.join(extra)}", FLAG))
    return out


# ------------------------------------------------------------------ series-level


def series_genre_check(rows: Sequence[Mapping[str, Any]]) -> list[Problem]:
    genres = {r.get("genre") for r in rows}
    if len(rows) > 1 and len(genres) > 1:
        return [Problem("series_genre", str(rows[0].get("series", "?")), f"one genre per series; this one holds {sorted(map(str, genres))}")]
    return []
