"""The writing rules a writer is given, and the text of one task.

The rules are inputs, never part of an adapter: a system prompt, the JSON schema the
writer's answer must follow, and a version. The version is recorded on every row the
rules produced and keys the providers' prompt caches, so a changed rule set always
has a new version.

A rule set can also bring two functions:

    task_text(spec, ordinal) -> str            the user text of one call, built from a
                                               spec line and the line's position in its batch
    normalize(text, spec, model) -> str|None   turns the writer's answer into the JSON list
                                               of rows the controller reads

Without them, a spec is sent as text (a string as it is, anything else as JSON) and the
answer is read as it comes. When an earlier attempt failed its checks, the check
messages follow the task text so the writer can fix them.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import ConfigError, RulesConfig
from .ids import canonical_json

DEFAULT_SYSTEM_FILE = "writer_system.md"
DEFAULT_SCHEMA_FILE = "row_schema.json"
DEFAULT_VERSION_FILE = "VERSION"

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

TaskTextFn = Callable[[Any, int], str]
NormalizeFn = Callable[[str | None, Any, str], str | None]


@dataclass(frozen=True)
class WriterRules:
    """What every writer call is told: the system prompt, the answer schema and the version.

    `schema_name` is the name some providers want beside a schema. `task_text` and
    `normalize` are the optional functions described in the module notes.
    """

    system: str
    schema: Mapping[str, Any]
    version: str
    schema_name: str = "row"
    task_text: TaskTextFn | None = field(default=None, compare=False, repr=False)
    normalize: NormalizeFn | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.system, str) or not self.system.strip():
            raise ConfigError("the writer rules need a system prompt")
        if not isinstance(self.schema, Mapping) or not self.schema:
            raise ConfigError("the writer rules need a JSON schema (an object)")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ConfigError("the writer rules need a version")
        if not _NAME_PATTERN.match(self.schema_name):
            raise ConfigError(f"schema name {self.schema_name!r} must be letters, digits, '_' or '-' (up to 64)")


def rules_version(system: str, schema: Mapping[str, Any]) -> str:
    """A short content hash of a system prompt and a schema."""
    digest = hashlib.sha256(f"{system}\n{canonical_json(schema)}".encode("utf-8")).hexdigest()
    return digest[:12]


def make_rules(
    system: str,
    schema: Mapping[str, Any],
    version: str | None = None,
    schema_name: str = "row",
    *,
    task_text: TaskTextFn | None = None,
    normalize: NormalizeFn | None = None,
) -> WriterRules:
    """Build rules from text, with a content-hash version when none is given."""
    return WriterRules(
        system=system,
        schema=dict(schema),
        version=version or rules_version(system, schema),
        schema_name=schema_name,
        task_text=task_text,
        normalize=normalize,
    )


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read the {what} {str(path)!r}: {error}") from None


def _hook(package: str, module: str, name: str) -> Callable[..., Any] | None:
    """A function a rules package may provide, or None when the package does not have it."""
    full_name = f"{package}.{module}"
    try:
        found = getattr(importlib.import_module(full_name), name, None)
    except ModuleNotFoundError as error:
        if error.name == full_name:
            return None  # the package simply has no such module
        raise ConfigError(f"{full_name} could not be imported: {error}") from None
    except ImportError as error:
        raise ConfigError(f"{full_name} could not be imported: {error}") from None
    return found if callable(found) else None


def load_writer_rules(config: RulesConfig, *, base_dir: str | Path | None = None) -> WriterRules:
    """Read the rules a `RulesConfig` names.

    Relative file names are taken from `base_dir` (the current folder when omitted).
    A package supplies `writer_system.md`, `row_schema.json` and, optionally, a version
    (its `rules_version()` function, else a `VERSION` file) and the two functions
    `task_text.build_task_text` and `form.normalize_output`.
    """
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    task_text: TaskTextFn | None = None
    normalize: NormalizeFn | None = None
    if config.package:
        try:
            module = importlib.import_module(config.package)
            root = resources.files(config.package)
        except (ImportError, TypeError) as error:
            raise ConfigError(f"rules package {config.package!r} cannot be imported: {error}") from None
        try:
            system = (root / (config.system_file or DEFAULT_SYSTEM_FILE)).read_text(encoding="utf-8")
            schema_text = (root / (config.schema_file or DEFAULT_SCHEMA_FILE)).read_text(encoding="utf-8")
        except OSError as error:
            raise ConfigError(f"cannot read the rules from package {config.package!r}: {error}") from None
        version_text = None
        provided = getattr(module, "rules_version", None)
        if callable(provided):
            version_text = str(provided()).strip() or None
        if version_text is None:
            try:
                version_text = (root / (config.version_file or DEFAULT_VERSION_FILE)).read_text(encoding="utf-8").strip() or None
            except OSError:
                version_text = None
        task_text = _hook(config.package, "task_text", "build_task_text")
        normalize = _hook(config.package, "form", "normalize_output")
    else:
        system = _read(base / str(config.system_file), "system prompt file")
        schema_text = _read(base / str(config.schema_file), "schema file")
        version_text = _read(base / config.version_file, "version file").strip() if config.version_file else None
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"the row schema is not valid JSON: {error}") from None
    if not isinstance(schema, dict):
        raise ConfigError("the row schema must be a JSON object")
    version = config.version or version_text or rules_version(system, schema)
    return WriterRules(system=system, schema=schema, version=version, task_text=task_text, normalize=normalize)


def spec_text(spec: Any) -> str:
    """A spec line as text: a string as it is, anything else as JSON."""
    if isinstance(spec, str):
        return spec
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True)


def feedback_text(feedback: Sequence[str]) -> str:
    """The block that tells a writer which checks its previous answer failed (empty when none)."""
    if not feedback:
        return ""
    lines = "\n".join(f"- {line}" for line in feedback)
    return f"\n\nYour previous answer for this task failed these checks. Answer again and fix every one of them:\n{lines}"


def render_task_text(spec: Any, feedback: Sequence[str] = (), *, ordinal: int = 0, rules: WriterRules | None = None) -> str:
    """The user text of one call: the task text for the spec line, then any check messages."""
    if rules is not None and rules.task_text is not None:
        text = rules.task_text(spec, ordinal)
    else:
        text = spec_text(spec)
    return text + feedback_text(feedback)
