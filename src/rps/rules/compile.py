"""Build the generated rule files from the validator's lists and the worked examples.

    python -m rps.rules.compile            # rewrite writer_system.md, row_schema.json and VERSION
    python -m rps.rules.compile --check    # exit 1 when a committed file differs from what would be written

The closed lists, the sealed values and the step kinds come from the row validator
(the `thought_seeds` package), so the prompt cannot drift from the checks. The prose
lives in templates/writer_system.tpl.md; the worked examples live in examples/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXAMPLE_FOR_PROMPT = "people_series"  # the example row that sits inside the system prompt
VERSIONED = ("pool.md", "writer_system.md", "row_schema.json", "review_brief.md")
SKIP_CLASSES = ("person", "contradiction", "ungrounded", "other")


def _validator():
    from thought_seeds.core import HELD, tp  # imported late: the package is only needed to compile

    return tp, HELD


def list_text(tp, held) -> str:
    """The closed value lists, one line per group of keys that share a list."""
    lists = tp.lists_for(held, "train")
    groups: dict[tuple[str, ...], list[str]] = {}
    for key, name in tp.KEY_LIST.items():
        if key == "intent genre":
            continue  # same as the genre list
        groups.setdefault(tuple(lists.get(name, ())), []).append(key)
    lines = []
    for values, keys in groups.items():
        lines.append(f"{', '.join(keys)}: {', '.join(values)}")
    lines.append("relation: " + ", ".join(r for r in tp.RELATIONS if r in lists.get("relation", tp.RELATIONS)))
    lines.append("thing (relation ends and thing name): " + ", ".join(lists.get("thing", ())))
    return "\n".join(lines)


def genres(tp, held) -> str:
    return ", ".join(tp.lists_for(held, "train")["genre"])


def sealed_text(tp, held) -> str:
    return ", ".join(sorted(tp.sealed_all(held)))


def example_text(name: str = EXAMPLE_FOR_PROMPT) -> str:
    """One medium row of the named example, as the writer returns it."""
    data = json.loads((HERE / "examples" / f"{name}.json").read_text(encoding="utf-8"))
    row = next(r for r in data["output"]["rows"] if r["depth"] == "medium")
    return json.dumps({"skip": False, "reason": "", "class": "", "rows": [row]}, ensure_ascii=False)


def writer_system() -> str:
    tp, held = _validator()
    template = (HERE / "templates" / "writer_system.tpl.md").read_text(encoding="utf-8")
    text = (
        template.replace("{{SEALED}}", sealed_text(tp, held))
        .replace("{{STEPS}}", ", ".join(tp.STEPS))
        .replace("{{GENRES}}", genres(tp, held))
        .replace("{{LISTS}}", list_text(tp, held))
        .replace("{{EXAMPLE}}", example_text())
    )
    return text.rstrip() + "\n"


def row_schema() -> dict[str, Any]:
    """JSON Schema for a writer's output: strict-mode safe (every property required, no open objects, no size keywords)."""
    tp, _ = _validator()

    def obj(**props: Any) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}

    text = {"type": "string"}
    row = obj(
        id=text,
        series=text,
        depth={"type": "string", "enum": list(tp.DEPTHS)},
        rank={"type": "integer", "enum": [1, 2, 3]},
        domain={"type": "string", "enum": list(tp.DOMAINS)},
        genre=text,
        records={"type": "array", "items": obj(key=text, value=text)},
        relations={"type": "array", "items": obj(a=text, rel=text, b=text)},
        thoughts={"type": "array", "items": obj(step={"type": "string", "enum": list(tp.STEPS)}, text=text)},
        wordings={
            "type": "array",
            "items": obj(kind={"type": "string", "enum": ["caption", "prompt", "tags", "critique"]}, text=text),
        },
        questions={
            "type": "array",
            "items": obj(key=text, value=text, needs={"type": "string", "enum": list(tp.NEEDS)}),
        },
    )
    return obj(
        skip={"type": "boolean"},
        reason=text,
        **{"class": {"type": "string", "enum": ["", *SKIP_CLASSES]}},
        rows={"type": "array", "items": row},
    )


def rules_version(directory: Path = HERE) -> str:
    """A content hash of the versioned files and the worked examples (12 hex characters)."""
    digest = hashlib.sha256()
    names = list(VERSIONED) + [f"examples/{p.name}" for p in sorted((directory / "examples").glob("*.json"))]
    for name in names:
        data = (directory / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()[:12]


def outputs() -> dict[str, str]:
    return {
        "writer_system.md": writer_system(),
        "row_schema.json": json.dumps(row_schema(), indent=1, ensure_ascii=False) + "\n",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when a committed file is out of date")
    args = parser.parse_args(argv)
    stale = []
    for name, text in outputs().items():
        path = HERE / name
        current = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8") if path.exists() else None
        if current != text:
            stale.append(name)
            if not args.check:
                path.write_text(text, encoding="utf-8", newline="\n")
    version = rules_version()
    version_path = HERE / "VERSION"
    if (version_path.read_text(encoding="utf-8").strip() if version_path.exists() else None) != version:
        stale.append("VERSION")
        if not args.check:
            version_path.write_text(version + "\n", encoding="utf-8", newline="\n")
    if args.check and stale:
        print("out of date: " + ", ".join(stale), file=sys.stderr)
        return 1
    print("rules version " + version + (" (rewrote " + ", ".join(stale) + ")" if stale and not args.check else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
