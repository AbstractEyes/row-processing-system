"""The repair step, as pure functions: which rows go back to their writer, and what the writer is told.

Reviewers never edit a row. With `review.policy.fix_to_hand` on, a row with a "fix" verdict waits in the hand
queue for the owner. With `repair.enabled` on, such a row is sent back instead, once, to the route that wrote it
(the `repair_requested` event), so that the writer writes it again:

    which rows     a row in the hand queue for a fix verdict that the owner has not answered, that was never
                   sent back before, that is not itself a repair, and whose repairing route is on and has a
                   cap for the purpose `repair` (`candidates`, `route_may_repair`). The repairing route is
                   the route that wrote the row, or the route `repair.routes` names for it
                   (`RepairConfig.route_for`), so the rows of a route that is switched off can be repaired
                   by another
    what it is     the same spec line, through the repairing route (its model, prompt profile and settings),
    told           and as feedback the notes of every fix verdict on the row (the notes alone, no reviewer's
                   name) followed by the row as it was written (`repair_feedback`); the wording around them
                   is `rps.prompting.repair_text`
    what comes     a new row with an id of its own, written by the repairing route (its writer label is that
    back           route's), linked to the row it repairs in the events and the state (never as a column of
                   a stored or shipped row), checked and reviewed like any row. A row that is sent back
                   again is never repaired again: it goes to the owner
    a correction   a reviewer whose configuration lists `may_amend` may give the light with a fix verdict
    of the light   (`rps.amend`). When the row's fix verdicts hold exactly one distinct value, the call is built from
                   the line's item with that record set as a given record (for "none", with any given `shot light`
                   removed: `repair_item`), the repair text says so (`light_lines`), and the new row is checked
                   against that same item, so the checker's given-record check holds it to the value

Nothing here touches a store or the state; the controller does the recording (see `rps.controller`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from .amend import LIGHT_KEY, NONE_VALUE, amended_item
from .config import REPAIR
from .provenance import RESERVED_ROW_KEYS
from .review.policy import FIX_TO_HAND_REASON

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .state import RowState

# Why a row whose repair came to nothing is in the hand queue again (the reason the owner reads).
FAILED_CHECKER_REASON = "the repair failed the checker: the row waits for the owner"
NO_ROW_REASON = "the repair gave no row, the writer set it aside instead of writing it again: the row waits for the owner"
SWITCHED_OFF_REASON = "the repair was not made, the step or the route's repair cap is switched off: the row waits for the owner"

NO_NOTE = "The reviewer gave no note."


def failure_reason(code: str, detail: str = "") -> str:
    """The reason a row goes back to the hand queue when its repair came to nothing.

    `code` is what `State.repairs_failed` says: `call_failed` (`detail` is the kind of failure), `no_row` or
    `checker`; `switched_off` is for a repair that was asked for and not sent before the step was turned off.
    """
    if code == "checker":
        return FAILED_CHECKER_REASON
    if code == "no_row":
        return NO_ROW_REASON
    if code == "switched_off":
        return SWITCHED_OFF_REASON
    what = f" ({detail})" if detail else ""
    return f"the repair call failed{what}: the row waits for the owner"


# ---------------------------------------------------------------------------- which rows


def is_candidate(row: "RowState") -> bool:
    """True for a row that may be sent back to its writer, whatever route it came from.

    It is in the hand queue (so the owner has not answered it) for a fix verdict, it was never sent back
    before, and it is not itself the result of a repair.
    """
    hand = row.hand or {}
    return (
        row.status == "hand"
        and hand.get("reason") == FIX_TO_HAND_REASON
        and "decision" not in hand
        and row.repair is None
        and row.repair_of is None
    )


def candidates(rows: Iterable["RowState"]) -> list["RowState"]:
    """The rows of `rows` that may be sent back to their writer, in row-id order."""
    return sorted((row for row in rows if is_candidate(row)), key=lambda row: row.row_id)


def route_may_repair(config: "Config", route_name: str) -> bool:
    """True when the repair step is on and the route is switched on and has a cap for the purpose `repair`.

    Asked about the repairing route of a row (`config.repair.route_for(row.route)`), not about the route that wrote
    it: the route that wrote it may be switched off, which is what `repair.routes` is for.
    """
    route = config.routes.get(route_name)
    return config.repair.enabled and route is not None and route.is_enabled and route.cap_for(REPAIR) > 0


# ---------------------------------------------------------------------------- what the writer is told


def fix_notes(verdicts: Sequence[Mapping[str, Any]], *, every: bool = False) -> list[str]:
    """The notes of every "fix" verdict, in the order the verdicts came: the notes alone, never who wrote them.
    With `every` (a row under a review chain, whose writer verdicts may include a reject that agreed with a fix) the
    notes of every verdict given, whatever it says.

    A reviewer's note names the problem and where it is, and never holds corrected text. A fix verdict that
    came with no note at all (a fix needs a reason code, not a note) says so, with its reason codes, so that
    the writer is never handed an empty line.
    """
    notes = []
    for verdict in verdicts:
        if verdict.get("verdict") != "fix" and not every:
            continue
        text = str(verdict.get("notes") or "").strip()
        if not text:
            codes = [str(code) for code in verdict.get("reasons") or [] if str(code).strip()]
            text = f"{NO_NOTE} Reason code(s): {', '.join(codes)}." if codes else NO_NOTE
        notes.append(text)
    return notes


def written_row_text(record: Mapping[str, Any]) -> str:
    """The row as its writer wrote it, as JSON text: the stored row without the labels the runner stamped on it
    (its id, the writer's label and the source label, which a writer never supplies)."""
    body = {key: value for key, value in record.items() if key not in RESERVED_ROW_KEYS}
    return json.dumps(body, ensure_ascii=False)


def repair_feedback(row: "RowState") -> tuple[str, ...]:
    """The feedback a repair call carries: the notes of every fix verdict on the row, one item each, then the
    lines about a correction of the light (when a reviewer gave one: `light_lines`), then the row as it was
    written (JSON text) as the last item. `rps.prompting.repair_text` turns it into words."""
    verdicts = row.writer_verdicts()  # all the live verdicts, or for a row under a review chain the ones whose notes go back
    return (*fix_notes(verdicts, every=row.under_chain), *light_lines(light_amendment(verdicts)), written_row_text(row.record))


# ---------------------------------------------------------------------------- a reviewer's correction of the light

# The sentences a repair call is given about the light (`light_lines`); `rps.prompting.repair_text` sets them apart
# from the notes by how they begin.
LIGHT_GIVEN = "A reviewer who saw the picture gives the light as: {value}. Keep this record exactly and make every sentence agree with it."
LIGHT_NONE = (
    "A reviewer who saw the picture found no light that a listed value fits. "
    "Record no shot light and state no light source or direction."
)
LIGHT_DIFFER = (
    "Reviewers who saw the picture read the light differently: {values}. No light record is set for you: "
    "look at the picture, and record a shot light only where it shows one plainly."
)
LIGHT_LINE_STARTS = ("A reviewer who saw the picture ", "Reviewers who saw the picture ")


def light_amendment(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """What the fix verdicts on a row say about the light: None when no reviewer corrected it, else
    {"key", "values" (the distinct values given, in the order they came), "value" (the one value to set,
    or None when two reviewers differ), "reviewers" (who gave which value, in order)}.

    Only a fix verdict counts, and a verdict the owner set aside is not among `verdicts` (see
    `RowState.live_verdicts`). The record is set (or, for "none", removed) only when exactly one distinct value
    was given.
    """
    given = [
        (str(verdict.get("reviewer_id", "")), str(value))
        for verdict in verdicts
        if verdict.get("verdict") == "fix"
        for key, value in (verdict.get("amend") or {}).items()
        if key == LIGHT_KEY
    ]
    if not given:
        return None
    values = list(dict.fromkeys(value for _reviewer, value in given))
    return {
        "key": LIGHT_KEY,
        "values": values,
        "value": values[0] if len(values) == 1 else None,
        "reviewers": [{"reviewer_id": reviewer, "value": value} for reviewer, value in given],
    }


def light_lines(amendment: Mapping[str, Any] | None) -> list[str]:
    """The line a repair call is given about the light (empty when no reviewer corrected it)."""
    if not amendment:
        return []
    values = amendment["values"]
    if len(values) == 1:
        return [LIGHT_NONE] if values[0] == NONE_VALUE else [LIGHT_GIVEN.format(value=values[0])]
    shown = ", ".join("no light a listed value fits" if value == NONE_VALUE else f'"{value}"' for value in values)
    return [LIGHT_DIFFER.format(values=shown)]


def repair_item(item: Any, row: "RowState | None") -> Any:
    """The spec item a row is written again from, and checked against: the line's own item, with the light a
    reviewer corrected set as a given record (or removed, for "none"). Two reviewers who differ set nothing."""
    amendment = light_amendment(row.writer_verdicts()) if row is not None else None
    if amendment is None or amendment["value"] is None:
        return item
    return amended_item(item, amendment["key"], amendment["value"])


def kept_value(record: Mapping[str, Any], key: str, value: str) -> bool:
    """Whether a row holds the corrected record: the row's `key` is `value`, or, for "none", the row holds no
    such record at all."""
    system, attribute = key.split(" ", 1)
    body = (record.get("spec") or {}).get(system)
    held = body.get(attribute) if isinstance(body, Mapping) else None
    return held is None if value == NONE_VALUE else held == value


def correction_outcomes(rows: Mapping[str, "RowState"]) -> list[dict[str, Any]]:
    """Every row a reviewer corrected the light of, whose repair is on record, in row-id order: what the
    reviewers gave and what the repair did with it.

    Each entry is {"row_id" (the row that was sent back), "key", "values", "value" (the value that was set, or
    None when reviewers differed), "reviewers" ([{reviewer_id, value}]), "repaired_by" (the ids of the rows the
    repair wrote), "outcomes" ([{row_id, "kept" (the row holds the value; None when no value was set), "status"}])}.
    """
    out = []
    for row_id in sorted(rows):
        row = rows[row_id]
        amendment = light_amendment(row.writer_verdicts())
        if amendment is None or row.repair is None:
            continue
        written = [rows[item] for item in row.repair.get("rows", []) if item in rows]
        value = amendment["value"]
        out.append(
            {
                "row_id": row_id,
                **amendment,
                "repaired_by": [item.row_id for item in written],
                "outcomes": [
                    {
                        "row_id": item.row_id,
                        "kept": None if value is None else kept_value(item.record, amendment["key"], value),
                        "status": item.status,
                    }
                    for item in written
                ],
            }
        )
    return out
