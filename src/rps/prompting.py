"""The writing rules a writer is given, and the text of one task.

The rules are inputs, never part of an adapter: a system prompt, the JSON schema the
writer's answer must follow, and a version. The version is recorded on every row the
rules produced and keys the providers' prompt caches, so a changed rule set always
has a new version.

A rule set can also bring some functions and an object:

    assign(spec, ordinal) -> choices           the choices the runner makes for one call
                                               (which part of a relation to ask about, ...),
                                               from the spec line and its position in the batch
    task_text(spec, ordinal[, choices]) -> str the user text of one call, built from a
                                               spec line, its position and the choices
    normalize(text, spec, model) -> str|None   turns the writer's answer into the JSON list
                                               of rows the controller reads
    explain(text, spec, model) -> [str]        why the rows of an answer that `normalize` could not
                                               turn into rows cannot be read: one line for each row,
                                               "<row id>: <what is wrong and the form it needs>"
    assessor                                   checks the rows a call returned (see rps.assess)

Without them, a spec is sent as text (a string as it is, anything else as JSON) and the
answer is read as it comes. When an earlier attempt failed its checks, the check
messages follow the task text so the writer can fix them (the lines `explain` gives are
part of the message for an answer whose rows could not be read). The choices are also handed
to the assessor (`AssessContext.assignment`), as plain data, so a check can compare a
row with what the writer was told.

A rule set can also carry a format block (`json_format`): text that shows the form of the
answer to a provider that has no schema mode. A package supplies it as a file called
`json_format.md`, or as a function `json_format()` that returns the text. Notes in HTML
comments are left out, and a block with nothing else in it is no block. Only a provider
that needs it sends it (the DeepSeek writer appends it to the system message); the others
have a schema and ignore it.
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
DEFAULT_JSON_FORMAT_FILE = "json_format.md"

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# Rule sets that come with a checker of their own, by package name.
DEFAULT_ASSESSORS: Mapping[str, str] = {"rps.rules": "rps.assessor:Assessor"}

TaskTextFn = Callable[..., str]
AssignFn = Callable[[Any, int], Any]
NormalizeFn = Callable[[str | None, Any, str], str | None]
ExplainFn = Callable[[str | None, Any, str], Sequence[str]]


def clean_json_format(text: str | None) -> str | None:
    """A format block as it is sent: without the notes in HTML comments, or None when nothing is left."""
    if text is None:
        return None
    return _HTML_COMMENT.sub("", str(text)).strip() or None


@dataclass(frozen=True)
class WriterRules:
    """What every writer call is told: the system prompt, the answer schema and the version.

    `schema_name` is the name some providers want beside a schema. `json_format` is the format
    block (see the module notes), already without its comments; None when the rule set has none.
    `task_text`, `assign`, `normalize`, `explain` and `assessor` are the optional parts described in
    the module notes.
    """

    system: str
    schema: Mapping[str, Any]
    version: str
    schema_name: str = "row"
    task_text: TaskTextFn | None = field(default=None, compare=False, repr=False)
    normalize: NormalizeFn | None = field(default=None, compare=False, repr=False)
    assign: AssignFn | None = field(default=None, compare=False, repr=False)
    assessor: Any = field(default=None, compare=False, repr=False)
    json_format: str | None = field(default=None, repr=False)
    explain: ExplainFn | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.system, str) or not self.system.strip():
            raise ConfigError("the writer rules need a system prompt")
        if not isinstance(self.schema, Mapping) or not self.schema:
            raise ConfigError("the writer rules need a JSON schema (an object)")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ConfigError("the writer rules need a version")
        if not _NAME_PATTERN.match(self.schema_name):
            raise ConfigError(f"schema name {self.schema_name!r} must be letters, digits, '_' or '-' (up to 64)")
        if self.json_format is not None and not isinstance(self.json_format, str):
            raise ConfigError("the format block of the writer rules must be text")
        object.__setattr__(self, "json_format", clean_json_format(self.json_format))


def rules_version(system: str, schema: Mapping[str, Any], json_format: str | None = None) -> str:
    """A short content hash of a system prompt and a schema (and of the format block, when there is one)."""
    text = f"{system}\n{canonical_json(schema)}"
    block = clean_json_format(json_format)
    if block is not None:
        text += f"\n{block}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def make_rules(
    system: str,
    schema: Mapping[str, Any],
    version: str | None = None,
    schema_name: str = "row",
    *,
    task_text: TaskTextFn | None = None,
    normalize: NormalizeFn | None = None,
    assign: AssignFn | None = None,
    assessor: Any = None,
    json_format: str | None = None,
    explain: ExplainFn | None = None,
) -> WriterRules:
    """Build rules from text, with a content-hash version when none is given."""
    return WriterRules(
        system=system,
        schema=dict(schema),
        version=version or rules_version(system, schema, json_format),
        schema_name=schema_name,
        task_text=task_text,
        normalize=normalize,
        assign=assign,
        assessor=assessor,
        json_format=json_format,
        explain=explain,
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


def make_assessor(path: str) -> Any:
    """Build a checker from a "module:name" path: a class or function called with no arguments.

    Something that is not callable is used as it is (an already built checker).
    """
    module_name, _, attribute = path.partition(":")
    if not module_name or not attribute:
        raise ConfigError(f"rules.assessor: expected 'module:name', got {path!r}")
    try:
        found = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise ConfigError(f"rules.assessor {path!r} cannot be loaded: {error}") from None
    if not callable(found):
        return found
    try:
        return found()
    except Exception as error:
        raise ConfigError(f"rules.assessor {path!r} could not be built: {type(error).__name__}: {error}") from None


def load_writer_rules(config: RulesConfig, *, base_dir: str | Path | None = None) -> WriterRules:
    """Read the rules a `RulesConfig` names.

    Relative file names are taken from `base_dir` (the current folder when omitted).
    A package supplies `writer_system.md`, `row_schema.json` and, optionally, a version
    (its `rules_version()` function, else a `VERSION` file), a format block (its
    `json_format()` function, else a `json_format.md` file) and the functions
    `task_text.build_task_text`, `balance.assign`, `form.normalize_output` and
    `form.explain_failures`. The
    checker comes from `config.assessor`, or from the package's default (see
    `DEFAULT_ASSESSORS`).
    """
    base = Path(base_dir) if base_dir is not None else Path.cwd()
    task_text: TaskTextFn | None = None
    normalize: NormalizeFn | None = None
    assign: AssignFn | None = None
    explain: ExplainFn | None = None
    json_format: str | None = None
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
        explain = _hook(config.package, "form", "explain_failures")
        assign = _hook(config.package, "balance", "assign")
        json_format = _package_json_format(config.package, module, root)
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
    version = config.version or version_text or rules_version(system, schema, json_format)
    assessor_path = config.assessor if config.assessor is not None else DEFAULT_ASSESSORS.get(config.package or "")
    assessor = None
    if assessor_path and assessor_path.lower() != "none":
        assessor = make_assessor(assessor_path)
    return WriterRules(
        system=system,
        schema=schema,
        version=version,
        task_text=task_text,
        normalize=normalize,
        assign=assign,
        assessor=assessor,
        json_format=json_format,
        explain=explain,
    )


def _package_json_format(package: str, module: Any, root: Any) -> str | None:
    """The format block a rules package carries: its `json_format()` function, else its `json_format.md` file."""
    provided = getattr(module, "json_format", None)
    if callable(provided):
        try:
            text = provided()
        except Exception as error:
            raise ConfigError(f"{package}.json_format() failed: {type(error).__name__}: {error}") from None
        if text is not None and not isinstance(text, str):
            raise ConfigError(f"{package}.json_format() must return text, got {type(text).__name__}")
        return clean_json_format(text)
    try:
        return clean_json_format((root / DEFAULT_JSON_FORMAT_FILE).read_text(encoding="utf-8"))
    except OSError:
        return None  # the package has no format block


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


def assignment_for(spec: Any, ordinal: int, rules: WriterRules | None) -> Any:
    """The runner's choices for one call, as the rule set's `assign` function returns them (or None)."""
    if rules is None or rules.assign is None:
        return None
    return rules.assign(spec, ordinal)


def assignment_data(choices: Any) -> dict[str, Any] | None:
    """The runner's choices as plain data for the assessor: `to_dict()` of the object, or the mapping itself."""
    if choices is None:
        return None
    if hasattr(choices, "to_dict"):
        choices = choices.to_dict()
    return dict(choices) if isinstance(choices, Mapping) else None


def render_task_text(spec: Any, feedback: Sequence[str] = (), *, ordinal: int = 0, rules: WriterRules | None = None) -> str:
    """The user text of one call: the task text for the spec line, then any check messages.

    The runner's choices (`assign`) are worked out once here and handed to the task text,
    so the text and the assessor's `AssessContext.assignment` carry the same values.
    """
    if rules is not None and rules.task_text is not None:
        choices = assignment_for(spec, ordinal, rules)
        text = rules.task_text(spec, ordinal) if choices is None else rules.task_text(spec, ordinal, choices)
    else:
        text = spec_text(spec)
    return text + feedback_text(feedback)
