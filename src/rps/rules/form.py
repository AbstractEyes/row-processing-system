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


class FormError(ValueError):
    """A writer's output that cannot be turned into rows."""


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
    return {
        "kind": "seeded" if seed_id else "synthetic",
        "model": model,
        "seed_dataset": str(seed_id).split(":", 1)[0] if seed_id else None,
        "seed_id": seed_id or None,
        "license": item.get("license") or "Apache-2.0",
    }


def to_row(model_row: Mapping[str, Any], item: Mapping[str, Any], model: str) -> dict[str, Any]:
    """The stored row for one row in the writer's form."""
    missing = [name for name in ROW_FIELDS if name not in model_row]
    if missing:
        raise FormError(f"the row lacks {missing}")
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

    A skip object stays a skip object. Output that is not the expected wrapper, or whose
    rows cannot be converted, is returned unchanged so the controller records it as unusable.
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
