"""The compiled rule pool: what an API writer is told and what a reviewer reads.

Files in this folder (package data):

    pool.md             every rule, numbered once, tagged by who can check it
    writer_system.md    the system prompt for a writing call (built by `compile`)
    row_schema.json     JSON Schema for a writer's output (built by `compile`)
    review_brief.md     the brief for reviewers
    examples/           worked examples: a spec item and the output a writer returns
    VERSION             a content hash of the files above; recorded on every row

`balance` assigns the per-call choices, `task_text` renders the user message of a
call, and `form` turns a writer's output into stored rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _read(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def rules_version() -> str:
    """The version of the committed rule files (the runner's rules_version and cache key)."""
    return _read("VERSION").strip()


def writer_system() -> str:
    return _read("writer_system.md")


def row_schema() -> dict[str, Any]:
    return json.loads(_read("row_schema.json"))


def review_brief() -> str:
    return _read("review_brief.md")


def pool() -> str:
    return _read("pool.md")


def examples() -> list[dict[str, Any]]:
    """The worked examples, each {"name", "note", "item", "output"}."""
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((HERE / "examples").glob("*.json"))]
