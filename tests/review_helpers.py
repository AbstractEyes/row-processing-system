"""Builders shared by the tests of the review settings: who may keep alone, rows reopened, packets withdrawn."""

from __future__ import annotations

import json

from rps import layout
from rps.assess import Assessment
from rps.layout import operator_actor
from rps.providers import SimulatedCrash
from rps.state import State
from rps.store import ActorStore

from helpers import make_config


def review_config(reviewers, *, packet_size=3, writer_family="writer-family", policy=None, **extra):
    """A configuration whose only review setting that varies is the reviewer list (and the writer's family)."""
    return make_config(
        review={
            "packet_size": packet_size,
            "reviewers": list(reviewers),
            "provider_families": {"fake": writer_family},
            "policy": {"audit_fraction": 0.0, **(policy or {})},
        },
        **extra,
    )


def by_family(mapping):
    """A reviewer function that answers by the reviewer's family."""

    def verdict(reviewer, item):
        word = mapping[reviewer.family]
        if word == "keep":
            return {"verdict": "keep"}
        return {"verdict": word, "reasons": ["needs_work"], "notes": f"{reviewer.family} found a problem in thought 1"}

    return verdict


def by_line(mapping, default="keep"):
    """A reviewer function that answers by the spec line of the row (a line not in the mapping gets `default`)."""

    def verdict(reviewer, item):
        word = mapping.get(item.task["line_id"], default)
        if word == "keep":
            return {"verdict": "keep"}
        return {"verdict": word, "reasons": ["needs_work"], "notes": f"line {item.task['line_id']} needs work in thought 1"}

    return verdict


def flag_everything(row, context):
    """An assess function that passes every row with a flag, so that no single keep accepts it."""
    return Assessment(passed=True, flags=("needs_a_look",))


def flag_lines(*lines):
    """An assess function that passes every row and flags the rows of the given spec lines."""

    def assess(row, context):
        return Assessment(passed=True, flags=("needs_a_look",) if context.line in lines else ())

    return assess


def rows_by_line(controller):
    return {row.line: row for row in controller.state.rows.values()}


def write_hand_decisions(world, decisions, name="decisions.jsonl"):
    """The owner's decisions for rows in the hand queue, as a file under control/hand/."""
    text = "".join(json.dumps(item) + "\n" for item in decisions)
    ActorStore(world.state_store, operator_actor()).write_text(layout.CONTROL_HAND_PREFIX + name, text)


def crash_after(count):
    """An `on_event` hook that kills the run after the count-th event it sees (counted across controllers it is given to)."""
    seen = [0]

    def hook(event):
        seen[0] += 1
        if seen[0] == count:
            raise SimulatedCrash(f"killed after event {count} ({event.kind})")

    return hook


def crash_on(kind, nth=1):
    seen = [0]

    def hook(event):
        if event.kind == kind:
            seen[0] += 1
            if seen[0] == nth:
                raise SimulatedCrash(f"killed after {kind} #{nth}")

    return hook


def outcome(world):
    """What a finished run leaves behind, ignoring how packets happened to be grouped."""
    state = State.from_events(world.events())
    rows = {
        row_id: (row.status, sorted((v["family"], v["verdict"], v["audit"]) for v in row.verdicts))
        for row_id, row in state.rows.items()
    }
    return rows, sorted(row["row_id"] for row in world.shipped_rows())


def packet_files(world, reviewer_id, packet_id=None):
    """The files in the state store under a reviewer's packets (or under one packet)."""
    prefix = f"packets/{reviewer_id}/" if packet_id is None else f"{layout.packet_dir(reviewer_id, packet_id)}/"
    return world.state_store.list(prefix)


def pictures_in_the_store(world):
    return [path for path in world.state_store.list("packets/") if "/images/" in path]
