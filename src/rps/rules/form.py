"""Converting between the form a writer returns and the stored row.

A writer returns each row in a flat form that fits a strict JSON schema (no open-ended
objects): the spec as a list of key/value records plus a list of relations, the
wordings as a list of kind/text pairs. The stored row is the dataset's own shape
(a nested spec, a wordings object, a held block and a source block). The runner does
the conversion, so the fields a model must not invent (held, source) never come from it.

    writer form                      stored row
    records [{key, value}]       ->  spec {"system": {"attribute": value}}
    relations [{a, rel, b}]      ->  spec["relation"]
    wordings [{kind, text}]      ->  wordings {kind: text}
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

HELD_BLOCK = {"layout": None, "wordings": False, "values": []}
ROW_FIELDS = ("id", "series", "depth", "rank", "domain", "genre", "records", "relations", "thoughts", "wordings", "questions")
WRAPPER_FIELDS = ("skip", "reason", "class", "rows")


DEPTH_WORDS = ("simple", "medium", "deep")
ADDED_KEYS = {
    "mood": "'mood feeling' and 'mood from'",
    "intent": "'intent purpose' and 'intent for'",
    "story": "'story moment' and one of 'story before', 'story after', 'story cause'",
}


class FormError(ValueError):
    """A writer's output that cannot be turned into rows. The message names the field and the form it needs."""


def _shown(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def row_problems(row: Any) -> list[str]:
    """Why a row in the writer's form cannot be turned into a stored row: one plain sentence per field, empty when it can."""
    if not isinstance(row, Mapping):
        return [f"a row is an object with the fields {', '.join(ROW_FIELDS)}, not {_shown(row)}"]
    problems: list[str] = []
    missing = [name for name in ROW_FIELDS if name not in row]
    if missing:
        problems.append(f"the row lacks the field(s) {', '.join(repr(m) for m in missing)}; every row has {', '.join(ROW_FIELDS)}")
    if "depth" in row and row["depth"] not in DEPTH_WORDS:
        problems.append(f"'depth' is {_shown(row['depth'])}; it is one of the words simple, medium, deep (never a number)")
    rank = row.get("rank")
    if "rank" in row and (isinstance(rank, bool) or rank not in (1, 2, 3)):
        problems.append(f"'rank' is {_shown(rank)}; it is the number 1, 2 or 3 (simple 1, medium 2, deep 3)")
    for name in ("id", "series", "domain", "genre"):
        if name in row and not isinstance(row[name], str):
            problems.append(f"'{name}' is {_shown(row[name])}; it is text")
    problems += _list_problems(row, "records", ("key", "value"), "a record is {\"key\": \"system attribute\", \"value\": \"text\"}")
    problems += _list_problems(row, "relations", ("a", "rel", "b"), "a relation is {\"a\": \"thing\", \"rel\": \"relation\", \"b\": \"thing\"}")
    problems += _list_problems(row, "thoughts", ("step", "text"), "a thought is {\"step\": \"name\", \"text\": \"sentence\"}")
    problems += _list_problems(row, "wordings", ("kind", "text"), "a wording is {\"kind\": \"caption\", \"text\": \"sentence\"}; wordings is a list, not an object")
    problems += _list_problems(row, "questions", ("key", "value", "needs"), "a question is {\"key\": \"...\", \"value\": \"...\", \"needs\": \"record\"}")
    for number, record in enumerate(row.get("records") if isinstance(row.get("records"), list) else [], 1):
        if isinstance(record, Mapping) and isinstance(record.get("key"), str) and " " not in record["key"].strip():
            key = record["key"].strip()
            added = ADDED_KEYS.get(key)
            hint = f"; this system's records are {added}" if added else ""
            problems.append(f"record key {key!r} is one word; keys are 'system attribute', such as 'mood feeling'{hint}")
    return problems


def _list_problems(row: Mapping[str, Any], name: str, fields: tuple[str, ...], form: str) -> list[str]:
    if name not in row:
        return []
    items = row[name]
    if not isinstance(items, list):
        return [f"'{name}' is {_shown(items)}; it is a list ({form})"]
    for number, item in enumerate(items, 1):
        if not isinstance(item, Mapping) or any(field not in item for field in fields):
            return [f"{name} item {number} is {_shown(item)}; {form}"]
    return []


def spec_from_records(records: Sequence[Mapping[str, Any]], relations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    for record in records:
        key, value = record.get("key"), record.get("value")
        if not isinstance(key, str) or " " not in key.strip() or not isinstance(value, str):
            raise FormError(f"a record needs a key of the form 'system attribute' and a text value, got {record!r}")
        system, attribute = key.strip().split(" ", 1)
        spec.setdefault(system, {})[attribute] = value
    if relations:
        spec["relation"] = [{"a": r.get("a"), "rel": r.get("rel"), "b": r.get("b")} for r in relations]
    return spec


def records_from_spec(spec: Mapping[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    relations: list[dict[str, str]] = []
    for system, body in spec.items():
        if system == "relation":
            relations = [{"a": r["a"], "rel": r["rel"], "b": r["b"]} for r in body]
        else:
            records.extend({"key": f"{system} {attribute}", "value": value} for attribute, value in body.items())
    return records, relations


def source_block(item: Mapping[str, Any], model: str) -> dict[str, Any]:
    """The source block of a stored row, from the spec item and the writer's model name."""
    seed_id = item.get("seed_id")
    if not item.get("license"):
        raise FormError("the spec item carries no license; a row's license is never guessed")
    return {
        "kind": "seeded" if seed_id else "synthetic",
        "model": model,
        "seed_dataset": str(seed_id).split(":", 1)[0] if seed_id else None,
        "seed_id": seed_id or None,
        "license": item["license"],
    }


def to_row(model_row: Mapping[str, Any], item: Mapping[str, Any], model: str) -> dict[str, Any]:
    """The stored row for one row in the writer's form."""
    problems = row_problems(model_row)
    if problems:
        raise FormError("; ".join(problems))
    wordings: dict[str, str] = {}
    for wording in model_row["wordings"]:
        text = str(wording.get("text", "")).strip()
        if text:
            wordings[str(wording.get("kind"))] = text
    return {
        "id": model_row["id"],
        "series": model_row["series"],
        "depth": model_row["depth"],
        "rank": model_row["rank"],
        "domain": model_row["domain"],
        "genre": model_row["genre"],
        "spec": spec_from_records(model_row["records"], model_row["relations"]),
        "thoughts": [{"step": t.get("step"), "text": t.get("text")} for t in model_row["thoughts"]],
        "wordings": wordings,
        "questions": [{"key": q.get("key"), "value": q.get("value"), "needs": q.get("needs")} for q in model_row["questions"]],
        "held": dict(HELD_BLOCK, values=[]),
        "source": source_block(item, model),
    }


def to_model_form(row: Mapping[str, Any]) -> dict[str, Any]:
    """The writer's form of a stored row (used to build worked examples)."""
    records, relations = records_from_spec(row["spec"])
    return {
        "id": row["id"],
        "series": row["series"],
        "depth": row["depth"],
        "rank": row["rank"],
        "domain": row["domain"],
        "genre": row["genre"],
        "records": records,
        "relations": relations,
        "thoughts": [{"step": t["step"], "text": t["text"]} for t in row["thoughts"]],
        "wordings": [{"kind": kind, "text": text} for kind, text in row["wordings"].items()],
        "questions": [{"key": q["key"], "value": q["value"], "needs": q["needs"]} for q in row["questions"]],
    }


def wrap(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The writer's output object for rows that were written."""
    return {"skip": False, "reason": "", "class": "", "rows": list(rows)}


def skip_object(reason: str, skip_class: str) -> dict[str, Any]:
    """The writer's output object for an item it declines."""
    return {"skip": True, "reason": reason, "class": skip_class, "rows": []}


def normalize_output(text: str | None, item: Mapping[str, Any], model: str) -> str | None:
    """Turn a writer's JSON output into the text the controller parses: a JSON list of stored rows.

    A skip object stays a skip object. Output that is not the expected wrapper is returned
    unchanged so the controller records it as unusable; so is a wrapper with a row that cannot
    be converted (`explain_failures` says why, field by field).
    """
    if text is None or not text.strip():
        return text
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict) or "rows" not in data or "skip" not in data:
        return text
    if data.get("skip") is True:
        return json.dumps({"skip": True, "reason": str(data.get("reason", "")), "class": str(data.get("class", "other"))})
    try:
        rows = [to_row(row, item, model) for row in data["rows"]]
    except (FormError, TypeError, AttributeError):
        return text
    return json.dumps(rows, ensure_ascii=False)


def explain_failures(text: str | None, item: Mapping[str, Any], model: str) -> list[str]:
    """Why the rows of a writer's wrapper cannot be turned into stored rows: one line per failing row,
    "<row id>: <field and the form it needs>; ...". Empty when every row converts or the text is no wrapper.
    A caller that retries a writer can send these lines back with the retry."""
    if not text or not text.strip():
        return []
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict) or "rows" not in data or data.get("skip") is True:
        return []
    if not isinstance(data["rows"], list):
        return [f"'rows' is {_shown(data['rows'])}; it is a list of rows"]
    lines = []
    for position, row in enumerate(data["rows"], 1):
        try:
            to_row(row, item, model)
        except (FormError, TypeError, AttributeError) as error:
            label = row.get("id") if isinstance(row, Mapping) and isinstance(row.get("id"), str) else f"row {position}"
            lines.append(f"{label}: {error}")
    return lines
