"""A reviewer's correction of one record: the light.

A reviewer returns verdicts and never edits a row. A reviewer whose configuration lists `may_amend`
may add one thing to a "fix" verdict: the value of a record the picture shows it plainly to be wrong
about. In this version the only record that may be corrected is `shot light`:

    {"row_id": ..., "verdict": "fix", "reasons": [...], "notes": "...", "amend": {"shot light": "soft window"}}

The value is one of the rules' list for the record, or "none": the picture shows no light that a
list value fits, so the row should record none. The packet of such a reviewer says which keys it may
amend and the values it may give (`may_amend`, `amend_values`). The controller keeps a valid correction
with the verdict; the repair step (`rps.repair`) then writes the row again from the item with the
record set (or removed), and the checker holds the new row to it.

Nothing here touches a store or the state.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any, Mapping, Sequence

LIGHT_KEY = "shot light"
AMEND_KEYS: tuple[str, ...] = (LIGHT_KEY,)  # the only records a reviewer may be allowed to correct
NONE_VALUE = "none"  # the picture shows no light that a list value fits: the row records none


@lru_cache(maxsize=None)
def _values(key: str) -> tuple[str, ...]:
    from thought_seeds.core import HELD, tp  # imported late: the package is read only when a list is wanted

    lists = tp.lists_for(HELD, "train")
    sealed = tp.sealed_all(HELD)
    return tuple(value for value in lists.get(tp.KEY_LIST[key], ()) if value not in sealed)


def amend_values(keys: Sequence[str] = AMEND_KEYS) -> dict[str, list[str]]:
    """The values a reviewer may give for each key: the rules' list for the record (the list the writer is
    offered), then "none"."""
    return {key: [*_values(key), NONE_VALUE] for key in keys}


def check_keys(keys: Sequence[Any]) -> list[str]:
    """The problems with a list of keys a reviewer is to be allowed to amend (empty when it is fine)."""
    return [f"only {', '.join(repr(key) for key in AMEND_KEYS)} may be listed, got {key!r}" for key in keys if key not in AMEND_KEYS]


def amend_problems(
    verdict: Any,
    amend: Any,
    may_amend: Sequence[str],
    values: Mapping[str, Sequence[str]] | None,
) -> list[str]:
    """What is wrong with the `amend` of a verdict (empty when it is fine).

    A correction goes only with the verdict "fix", names only a key the packet's `may_amend` lists, and
    gives only a value of the packet's `amend_values` for it.
    """
    if not isinstance(amend, Mapping) or not amend:
        return ["amend must be an object with a key and its value"]
    problems: list[str] = []
    if not may_amend:
        return ["amend is not allowed: this reviewer may not correct a record"]
    if verdict != "fix":
        problems.append("amend goes only with the verdict fix")
    allowed = values or {}
    for key, value in amend.items():
        if key not in may_amend:
            problems.append(f"amend key {key!r} is not one this reviewer may correct: {list(may_amend)}")
        elif not isinstance(value, str) or value not in set(allowed.get(key, ())):
            problems.append(f"amend value {value!r} for {key!r} is not in the packet's amend_values")
    return problems


# ---------------------------------------------------------------------------- the item a repair is written from


def _set(spec: Any, key: str, value: str | None) -> None:
    system, attribute = key.split(" ", 1)
    if not isinstance(spec, dict):
        return
    if value is None:
        body = spec.get(system)
        if isinstance(body, dict) and attribute in body:
            del body[attribute]
            if not body:
                del spec[system]
        return
    body = spec.setdefault(system, {})
    if isinstance(body, dict):
        body[attribute] = value


def _holds(spec: Any, key: str) -> bool:
    system, attribute = key.split(" ", 1)
    body = spec.get(system) if isinstance(spec, Mapping) else None
    return isinstance(body, Mapping) and attribute in body


def amended_item(item: Any, key: str, value: str) -> Any:
    """A copy of a spec item with the record set to `value` as a given record (for "none": any given record
    of that key removed). The copy carries `amend` ({key: value}) so the checker knows what was asked.

    A single item holds the record in its spec. A series holds it in each depth that has it, so the ranks
    keep nesting; when no depth has it, the deepest one is given it. An item that is not a mapping, or
    that holds no spec, is returned as it is.
    """
    if not isinstance(item, Mapping):
        return item
    out = copy.deepcopy(dict(item))
    target = None if value == NONE_VALUE else value
    if isinstance(out.get("spec"), dict):
        _set(out["spec"], key, target)
    elif isinstance(out.get("specs"), dict):
        specs = out["specs"]
        held = [depth for depth, spec in specs.items() if _holds(spec, key)]
        if target is not None and not held:
            held = [depth for depth in ("deep", "medium", "simple") if depth in specs][:1]
        for depth in held:
            _set(specs[depth], key, target)
    else:
        return item
    out["amend"] = {key: value}
    return out
