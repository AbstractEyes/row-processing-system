"""The repair step, as pure functions: which rows go back to their writer, and what the writer is told.

Reviewers never edit a row. With `review.policy.fix_to_hand` on, a row with a "fix" verdict waits in the hand
queue for the owner. With `repair.enabled` on, such a row is sent back instead to the route that wrote it
(the `repair_requested` event), so that the writer writes it again. A repair is made in at most `repair.attempts`
calls (one by default) and a row may be repaired in at most `repair.rounds` rounds (one by default):

    attempts       a repair whose row fails the checker, or whose call fails, is tried again for the same
                   original row, with the checker's errors on the previous attempt added to the notes, until
                   `repair.attempts` attempts are used; a repair the writer declined (it gave no row) is never
                   tried again. The attempts a row has used are the calls on its record that are not retries
                   of an earlier call (`attempts_used`)
    rounds         a row that is itself a repair (its `repair_of` chain is one link long) may be sent back once
                   more when `repair.rounds` is 2; the round of a row is the length of its `repair_of` chain
                   (`repair_round`). A row in its last round that is sent back again goes to the owner

    which rows     a row in the hand queue for a fix verdict that the owner has not answered, that was never
                   sent back before and is not in its last round, or a row that waits because its repair
                   failed the checker or its call failed and that has used fewer attempts than are allowed
                   now; and whose repairing route is on and has a cap for the purpose `repair`
                   (`candidates`, `route_may_repair`). The repairing route is
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
                   again in its last round is not repaired again: it goes to the owner
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

# The words that open a checker's error among the lines a later attempt is given (`repair_feedback`); `rps.prompting.repair_text`
# sets those lines apart from the notes by how they begin.
CHECK_LINE_START = "The checker refused your previous attempt: "

# Why a row whose repair came to nothing is in the hand queue again (the reason the owner reads).
FAILED_CHECKER_REASON = "the repair failed the checker: the row waits for the owner"
NO_ROW_REASON = "the repair gave no row, the writer set it aside instead of writing it again: the row waits for the owner"
SWITCHED_OFF_REASON = "the repair was not made, the step or the route's repair cap is switched off: the row waits for the owner"
CALL_FAILED_REASON_START = "the repair call failed"
UNPREPARED_ERROR = "call could not be prepared"  # how a call that could not be built is recorded as failed

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


def repair_round(row: "RowState", rows: Mapping[str, "RowState"] | None = None) -> int:
    """The round a row is in: the length of its `repair_of` chain (0 for a row no repair wrote, 1 for the row a first
    repair wrote, 2 for the row of a second round). Without `rows` the chain is read as far as the row itself."""
    length, seen, current = 0, {row.row_id}, row
    while current.repair_of is not None:
        length += 1
        if rows is None or current.repair_of in seen:
            break
        seen.add(current.repair_of)
        current = rows.get(current.repair_of)  # type: ignore[assignment]
        if current is None:
            break
    return length


def attempts_used(row: "RowState", calls: Mapping[str, Any] | None = None) -> int:
    """How many attempts the repair of a row has used: the calls on its repair record that are not retries of an
    earlier call (a call made again after a failure belongs to the attempt it repeats). Without `calls` every call
    on the record counts as an attempt."""
    ids = list((row.repair or {}).get("calls", []))
    if calls is None:
        return len(ids)
    return sum(1 for call_id in ids if call_id in calls and getattr(calls[call_id], "retry_of", None) is None)


def may_repeat_after(reason: Any) -> bool:
    """True when `reason` is one a row waits in the hand queue for after a repair that failed the checker or whose
    call failed: the repair may be made again. The reasons for a repair the writer declined, or that was not made,
    are not among them."""
    return reason == FAILED_CHECKER_REASON or str(reason).startswith(CALL_FAILED_REASON_START)


def failed_repair_may_repeat(
    row: "RowState",
    calls: Mapping[str, Any] | None,
    attempts: int,
    rows: Mapping[str, "RowState"] | None = None,
    rounds: int = 1,
) -> bool:
    """True for a row that waits in the hand queue because its repair failed the checker or its call failed, that
    has used fewer attempts than `attempts`, and whose round is below `rounds`. A row whose repair the writer
    declined, or was not made, stays; so does a row whose last attempt the checker set aside (a finding about the
    item that no rewrite changes) or whose call could not be built (that does not change either)."""
    reason = (row.hand or {}).get("reason")
    if row.repair is None or row.repair.get("failed") != reason:
        return False
    if not may_repeat_after(reason) or repair_round(row, rows) >= rounds:
        return False
    listed = list(row.repair.get("calls", []))
    last = calls.get(listed[-1]) if calls is not None and listed else None
    if last is not None:
        if str(getattr(last, "error", None) or "").startswith(UNPREPARED_ERROR):
            return False
        written = [rows[row_id] for row_id in getattr(last, "row_ids", []) if rows is not None and row_id in rows]
        if written and not any(item.status == "rejected" for item in written):
            return False
    return attempts_used(row, calls) < attempts


def is_candidate(
    row: "RowState",
    *,
    rows: Mapping[str, "RowState"] | None = None,
    calls: Mapping[str, Any] | None = None,
    attempts: int = 1,
    rounds: int = 1,
) -> bool:
    """True for a row that may be sent back to its writer, whatever route it came from.

    It is in the hand queue (so the owner has not answered it), and either
      - it is there for a fix verdict, it was never sent back before, and it is in a round before the last
        (`repair_round` < `rounds`: with one round, a row that is itself a repair is never sent back), or
      - its repair failed the checker or its call failed, and it has used fewer attempts than `attempts`.
    With the defaults (one attempt, one round) this is the rule of the releases before the two settings.
    """
    hand = row.hand or {}
    if row.status != "hand" or "decision" in hand:
        return False
    if hand.get("reason") == FIX_TO_HAND_REASON:
        return row.repair is None and repair_round(row, rows) < rounds
    return failed_repair_may_repeat(row, calls, attempts, rows, rounds)


def candidates(
    rows: Iterable["RowState"],
    *,
    all_rows: Mapping[str, "RowState"] | None = None,
    calls: Mapping[str, Any] | None = None,
    attempts: int = 1,
    rounds: int = 1,
) -> list["RowState"]:
    """The rows of `rows` that may be sent back to their writer, in row-id order (`all_rows`: every row of the
    state, to read the chains; `calls`: every call, to count the attempts)."""
    return sorted(
        (row for row in rows if is_candidate(row, rows=all_rows, calls=calls, attempts=attempts, rounds=rounds)),
        key=lambda row: row.row_id,
    )


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


def checker_lines(errors: Iterable[str]) -> list[str]:
    """The checker's errors on a previous attempt, one item each, in the words a later attempt is given."""
    return [CHECK_LINE_START + str(error).strip() for error in errors if str(error).strip()]


def repair_feedback(row: "RowState", checker_errors: Iterable[str] = ()) -> tuple[str, ...]:
    """The feedback a repair call carries: the notes of every fix verdict on the row, one item each, then the
    lines about a correction of the light (when a reviewer gave one: `light_lines`), then, for an attempt after
    one the checker refused, the checker's errors on that attempt (`checker_errors`), then the row as it was
    written (JSON text) as the last item. `rps.prompting.repair_text` turns it into words."""
    verdicts = row.writer_verdicts()  # all the live verdicts, or for a row under a review chain the ones whose notes go back
    return (
        *fix_notes(verdicts, every=row.under_chain),
        *light_lines(light_amendment(verdicts)),
        *checker_lines(checker_errors),
        written_row_text(row.record),
    )


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
