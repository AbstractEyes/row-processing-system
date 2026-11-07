"""The assessment hook.

After a row is written, the controller asks an assess function whether it passes.
The real assessor is plugged in later; until then `default_assess` accepts any row
that has content. An assess function takes the row and a context and returns an
`Assessment`:

    passed      False means a hard failure: the row is rejected, and the call is
                retried once with the error lines as feedback
    flags       short codes for things a reviewer should look at; a flagged row is
                never accepted on a single "keep"
    errors      one line per hard failure
    set_aside   a dict (class, reason) to set the row aside instead (no retry)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .provenance import RESERVED_ROW_KEYS


@dataclass(frozen=True)
class AssessContext:
    """What an assess function may need to know about a row's origin."""

    task_id: str
    source: str
    batch: str
    line: str
    attempt: int
    route: str
    pool_version: str
    spec: Any
    item_id: str | None


@dataclass(frozen=True)
class Assessment:
    passed: bool
    flags: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    set_aside: Mapping[str, Any] | None = None


AssessFn = Callable[[Mapping[str, Any], AssessContext], Assessment]


def default_assess(row: Mapping[str, Any], context: AssessContext) -> Assessment:
    """Pass any row that has at least one field of its own (the real assessor comes later)."""
    content = [key for key in row if key not in RESERVED_ROW_KEYS]
    if not content:
        return Assessment(passed=False, errors=("the row has no content",))
    return Assessment(passed=True)
