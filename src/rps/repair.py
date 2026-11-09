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

Nothing here touches a store or the state; the controller does the recording (see `rps.controller`).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

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


def fix_notes(verdicts: Sequence[Mapping[str, Any]]) -> list[str]:
    """The notes of every "fix" verdict, in the order the verdicts came: the notes alone, never who wrote them.

    A reviewer's note names the problem and where it is, and never holds corrected text. A fix verdict that
    came with no note at all (a fix needs a reason code, not a note) says so, with its reason codes, so that
    the writer is never handed an empty line.
    """
    notes = []
    for verdict in verdicts:
        if verdict.get("verdict") != "fix":
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
    row as it was written (JSON text) as the last item. `rps.prompting.repair_text` turns it into words."""
    return (*fix_notes(row.verdicts), written_row_text(row.record))
