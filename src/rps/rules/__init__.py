"""The compiled rule pool: what an API writer is told and what a reviewer reads.

Files in this folder (package data):

    pool.md             every rule, numbered once, tagged by who can check it
    writer_system.md    the system prompt for a writing call (built by `compile`)
    row_schema.json     JSON Schema for a writer's output (built by `compile`)
    review_brief.md     the brief for reviewers
    json_format.md      a field-by-field description of the answer (placeholders only), for routes with no schema
    examples/           worked examples: a spec item and the output a writer returns
    VERSION             a content hash of the files above; recorded on every row
    profiles/<name>/    a prompt profile: another wording of the writer prompt, for the routes that name it

A profile folder holds writer_system.tpl.md (its template, compiled like the default one into
writer_system.md), task.tpl.txt (the user message, with named fields in braces), retry.tpl.txt
(the block that follows the user message after a failed answer), VERSION, and optionally
hint.tpl.txt (the block for a real picture's hint), json_format.md (its own format block) and
openapi_schema.json (the row schema in the OpenAPI form, with field descriptions). The answer
form, the checks and the per-call choices are the same in every profile.

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


def json_format() -> str:
    """The answer's form, field by field, for a route that cannot enforce a schema (appended to its system message)."""
    return _read("json_format.md")


def pool() -> str:
    return _read("pool.md")


def examples() -> list[dict[str, Any]]:
    """The worked examples, each {"name", "note", "item", "output"}."""
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((HERE / "examples").glob("*.json"))]


def profile_names() -> list[str]:
    """The prompt profiles of the rule pool besides the default prompt, by folder name."""
    folder = HERE / "profiles"
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.iterdir() if path.is_dir() and (path / "writer_system.md").is_file())
