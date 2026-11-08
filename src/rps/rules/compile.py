"""Build the generated rule files from the validator's lists and the worked examples.

    python -m rps.rules.compile            # rewrite writer_system.md, row_schema.json and VERSION
    python -m rps.rules.compile --check    # exit 1 when a committed file differs from what would be written

The closed lists, the sealed values and the step kinds come from the row validator
(the `thought_seeds` package), so the prompt cannot drift from the checks. The prose
lives in templates/writer_system.tpl.md; the worked examples live in examples/.

Each folder under profiles/ is a prompt profile: another wording of the writer prompt for
the routes that name it (see rps.prompting). Its writer_system.tpl.md is compiled the same
way into writer_system.md beside it, and its VERSION is a hash of the files every profile
shares (`SHARED`, the worked examples) and of the profile's own files. The default prompt
and its VERSION do not depend on the profiles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXAMPLE_FOR_PROMPT: str | None = None  # name of the example file whose medium row sits inside the system prompt; None: no example
VERSIONED = ("pool.md", "writer_system.md", "row_schema.json", "review_brief.md", "json_format.md")
SKIP_CLASSES = ("person", "contradiction", "ungrounded", "other")
PROFILES = "profiles"  # one folder per prompt profile
PROFILE_TEMPLATE = "writer_system.tpl.md"
PROFILE_SYSTEM = "writer_system.md"
# The files the profiles share with the default prompt (the worked examples too): a profile's version hashes them with its own files.
SHARED = ("pool.md", "row_schema.json", "review_brief.md", "json_format.md")


def _validator():
    from thought_seeds.core import HELD, tp  # imported late: the package is only needed to compile

    return tp, HELD


def list_text(tp, held) -> str:
    """The closed value lists, one line per group of keys that share a list."""
    lists = tp.lists_for(held, "train")
    sealed = tp.sealed_all(held)
    groups: dict[tuple[str, ...], list[str]] = {}
    for key, name in tp.KEY_LIST.items():
        if key == "intent genre":
            continue  # same as the genre list
        values = tuple(v for v in lists.get(name, ()) if v not in sealed)  # the prompt never offers a value it forbids
        groups.setdefault(values, []).append(key)
    lines = []
    for values, keys in groups.items():
        lines.append(f"{', '.join(keys)}: {', '.join(values)}")
    lines.append("relation: " + ", ".join(r for r in tp.RELATIONS if r in lists.get("relation", tp.RELATIONS)))
    lines.append("thing (relation ends and thing name): " + ", ".join(lists.get("thing", ())))
    return "\n".join(lines)


def genres(tp, held) -> str:
    from rps.assessor.checks import EXTRA_GENRES

    listed = list(tp.lists_for(held, "train")["genre"])
    return ", ".join(listed + sorted(g for g in EXTRA_GENRES if g not in listed))


def sealed_text(tp, held) -> str:
    return ", ".join(sorted(tp.sealed_all(held)))


def example_text(name: str) -> str:
    """One medium row of the named example, as the writer returns it."""
    data = json.loads((HERE / "examples" / f"{name}.json").read_text(encoding="utf-8"))
    row = next(r for r in data["output"]["rows"] if r["depth"] == "medium")
    return json.dumps({"skip": False, "reason": "", "class": "", "rows": [row]}, ensure_ascii=False)


def fill_template(template: str, example: str | None = EXAMPLE_FOR_PROMPT) -> str:
    """A writer prompt template with its placeholders filled; `example` names an example file whose medium row
    fills {{EXAMPLE_BLOCK}} (None: no example, and each example placeholder goes with the line break after it)."""
    tp, held = _validator()
    text = (
        template.replace("{{SEALED}}", sealed_text(tp, held))
        .replace("{{STEPS}}", ", ".join(tp.STEPS))
        .replace("{{GENRES}}", genres(tp, held))
        .replace("{{LISTS}}", list_text(tp, held))
    )
    if example is None:
        text = text.replace("{{EXAMPLE_NOTE}}\n", "").replace("{{EXAMPLE_BLOCK}}\n", "").replace("{{EXAMPLE_BLOCK}}", "")
    else:
        text = text.replace("{{EXAMPLE_NOTE}}", "One example row is at the end.").replace(
            "{{EXAMPLE_BLOCK}}", "EXAMPLE ROW (one medium row of a series)\n" + example_text(example)
        )
    return text.rstrip() + "\n"


def writer_system(example: str | None = EXAMPLE_FOR_PROMPT) -> str:
    """The compiled writer prompt; `example` names an example file whose medium row ends it (None: no example)."""
    return fill_template((HERE / "templates" / "writer_system.tpl.md").read_text(encoding="utf-8"), example)


def profile_names(directory: Path | None = None) -> list[str]:
    """The prompt profiles: the folders under profiles/ that hold a writer prompt template."""
    folder = (directory or HERE) / PROFILES
    if not folder.is_dir():
        return []
    return sorted(path.name for path in folder.iterdir() if path.is_dir() and (path / PROFILE_TEMPLATE).is_file())


def profile_system(name: str, example: str | None = EXAMPLE_FOR_PROMPT) -> str:
    """The compiled writer prompt of a prompt profile, filled the same way as the default one."""
    return fill_template((HERE / PROFILES / name / PROFILE_TEMPLATE).read_text(encoding="utf-8"), example)


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


def _content_hash(directory: Path, names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        data = (directory / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()[:12]


def _example_names(directory: Path) -> list[str]:
    return [f"examples/{p.name}" for p in sorted((directory / "examples").glob("*.json"))]


def rules_version(directory: Path | None = None) -> str:
    """A content hash of the versioned files and the worked examples (12 hex characters)."""
    directory = directory or HERE
    return _content_hash(directory, list(VERSIONED) + _example_names(directory))


def profile_files(name: str, directory: Path | None = None) -> list[str]:
    """A profile's own files, by their path in the rule pool (its VERSION aside), in name order."""
    folder = (directory or HERE) / PROFILES / name
    own = sorted(p.name for p in folder.iterdir() if p.is_file() and p.name != "VERSION" and not p.name.startswith("."))
    return [f"{PROFILES}/{name}/{file_name}" for file_name in own]


def profile_version(name: str, directory: Path | None = None) -> str:
    """A profile's version: a content hash of the shared files, the worked examples and the profile's own files (12 hex characters)."""
    directory = directory or HERE
    return _content_hash(directory, list(SHARED) + _example_names(directory) + profile_files(name, directory))


def outputs() -> dict[str, str]:
    files = {
        "writer_system.md": writer_system(),
        "row_schema.json": json.dumps(row_schema(), indent=1, ensure_ascii=False) + "\n",
    }
    for name in profile_names():
        files[f"{PROFILES}/{name}/{PROFILE_SYSTEM}"] = profile_system(name)
    return files


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
    versions = {"VERSION": version}
    for name in profile_names():
        versions[f"{PROFILES}/{name}/VERSION"] = profile_version(name)
    for name, value in versions.items():
        version_path = HERE / name
        if (version_path.read_text(encoding="utf-8").strip() if version_path.exists() else None) != value:
            stale.append(name)
            if not args.check:
                version_path.write_text(value + "\n", encoding="utf-8", newline="\n")
    if args.check and stale:
        print("out of date: " + ", ".join(stale), file=sys.stderr)
        return 1
    named = "".join(f"; profile {name} {profile_version(name)}" for name in profile_names())
    print("rules version " + version + named + (" (rewrote " + ", ".join(stale) + ")" if stale and not args.check else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
