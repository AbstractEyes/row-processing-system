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
    screen(spec) -> reason|None                reads a spec line before its first call: a reason sets
                                               the line aside with no call (see the README, "Plugging
                                               in"); None sends the line on

Without them, a spec is sent as text (a string as it is, anything else as JSON) and the
answer is read as it comes. When an earlier attempt failed its checks, the check
messages follow the task text so the writer can fix them (the lines `explain` gives are
part of the message for an answer whose rows could not be read). The choices are also handed
to the assessor (`AssessContext.assignment`), as plain data, so a check can compare a
row with what the writer was told. A row a reviewer sent back (the repair step, see `rps.repair`)
is written again from the same task text followed by a block of its own wording (`repair_text`):
the reviewers' notes and the row as it was written, in place of the check messages.

A rule set can also carry a format block (`json_format`): text that shows the form of the
answer to a provider that has no schema mode. A package supplies it as a file called
`json_format.md`, or as a function `json_format()` that returns the text. Notes in HTML
comments are left out, and a block with nothing else in it is no block. Only a provider
that needs it sends it (the DeepSeek writer appends it to the system message); the others
have a schema and ignore it.

Prompt profiles. A rules package may hold other wordings of its prompt, one folder each under
`profiles/<name>/`, and a route names the one it writes with (`profile` in the route's settings;
`default` is the package's own files). A profile folder holds:

    writer_system.md      the system prompt
    task.tpl.txt          the user message, with named fields in braces ({series_id}, ...), filled
                          by the package's `task_text.profile_task_text(name, layout, hint_layout)`
    retry.tpl.txt         the block that follows the user message after a failed answer;
                          {check_messages} is the check lines, each "- " + the message
    VERSION               the profile's version (a content hash of its files when missing)
    hint.tpl.txt          optional: the block that holds a real picture's hint, for the layout
    json_format.md        optional: the profile's own format block, in place of the package's
    openapi_schema.json   optional: the row schema in the OpenAPI form with field descriptions, sent
                          where a provider takes that form (Gemini's `response_schema`)

Everything else (the schema, the per-call choices, the conversion of answers into rows, the
checker and the screen) is the package's own and shared by every profile. `profile_rules` gives
the rules of a named profile; an unknown name is a ConfigError.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import DEFAULT_PROFILE, ConfigError, RulesConfig
from .ids import canonical_json

DEFAULT_SYSTEM_FILE = "writer_system.md"
DEFAULT_SCHEMA_FILE = "row_schema.json"
DEFAULT_VERSION_FILE = "VERSION"
DEFAULT_JSON_FORMAT_FILE = "json_format.md"

# Prompt profiles (see the module notes); DEFAULT_PROFILE is the rule set's own prompt.
PROFILES_FOLDER = "profiles"
PROFILE_TASK_FILE = "task.tpl.txt"
PROFILE_RETRY_FILE = "retry.tpl.txt"
PROFILE_HINT_FILE = "hint.tpl.txt"
PROFILE_OPENAPI_FILE = "openapi_schema.json"
CHECK_MESSAGES_FIELD = "check_messages"

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# A named field of a layout: a lower-case name in braces. Other braces (JSON in the text) are left alone.
FIELD_PATTERN = re.compile(r"\{([a-z_]+)\}")

# Rule sets that come with a checker of their own, by package name.
DEFAULT_ASSESSORS: Mapping[str, str] = {"rps.rules": "rps.assessor:Assessor"}
# Rule sets that come with a screen of their own (a function of one spec line), by package name.
DEFAULT_SCREENS: Mapping[str, str] = {"rps.rules": "rps.assessor:screen_item"}

TaskTextFn = Callable[..., str]
AssignFn = Callable[[Any, int], Any]
NormalizeFn = Callable[[str | None, Any, str], str | None]
ExplainFn = Callable[[str | None, Any, str], Sequence[str]]
ScreenFn = Callable[[Any], str | None]


def clean_json_format(text: str | None) -> str | None:
    """A format block as it is sent: without the notes in HTML comments, or None when nothing is left."""
    if text is None:
        return None
    return _HTML_COMMENT.sub("", str(text)).strip() or None


def fill_fields(template: str, fields: Mapping[str, str]) -> str:
    """A layout with each named field ({name}) replaced by its text; a name not in `fields` and other braces stay.

    The layout is read once, so braces inside a filled text are never read as fields.
    """
    return FIELD_PATTERN.sub(lambda match: fields.get(match.group(1), match.group(0)), template)


@dataclass(frozen=True)
class WriterRules:
    """What every writer call is told: the system prompt, the answer schema and the version.

    `schema_name` is the name some providers want beside a schema. `json_format` is the format
    block (see the module notes), already without its comments; None when the rule set has none.
    `task_text`, `assign`, `normalize`, `explain`, `assessor` and `screen` are the optional parts
    described in the module notes.

    `profile` names the prompt profile these rules are (see the module notes). `retry` is the
    profile's retry block, with the field {check_messages}; None writes the check messages as
    `feedback_text` does. `openapi_schema` is the schema in the OpenAPI form with field descriptions,
    for a provider that takes that form; None lets the provider build the form from `schema`.
    `profile_loader` gives the rules of another profile of the same rule set (see `profile_rules`).
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
    screen: ScreenFn | None = field(default=None, compare=False, repr=False)
    profile: str = DEFAULT_PROFILE
    retry: str | None = field(default=None, repr=False)
    openapi_schema: Mapping[str, Any] | None = field(default=None, repr=False)
    profile_loader: Callable[[str], "WriterRules"] | None = field(default=None, compare=False, repr=False)

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
        if not isinstance(self.profile, str) or not _NAME_PATTERN.match(self.profile):
            raise ConfigError(f"profile name {self.profile!r} must be letters, digits, '_' or '-' (up to 64)")
        if self.retry is not None and (not isinstance(self.retry, str) or not self.retry.strip()):
            raise ConfigError("the retry block of the writer rules must be text")
        if self.openapi_schema is not None and (not isinstance(self.openapi_schema, Mapping) or not self.openapi_schema):
            raise ConfigError("the OpenAPI form of the schema must be an object")
        object.__setattr__(self, "json_format", clean_json_format(self.json_format))


def profile_rules(rules: WriterRules, name: str) -> WriterRules:
    """The rules of the named prompt profile of the rule set `rules` belongs to.

    The rules themselves when they are that profile. A rule set with no such profile (or no profiles
    at all) raises ConfigError, naming the profiles it has.
    """
    if name == rules.profile:
        return rules
    if rules.profile_loader is None:
        raise ConfigError(f"unknown prompt profile {name!r}: these writing rules have no prompt profiles")
    return rules.profile_loader(name)


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
    screen: ScreenFn | None = None,
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
        screen=screen,
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


def make_screen(path: str) -> ScreenFn:
    """The screen function a "module:name" path names: called with a spec line, it returns the reason to set the line aside, or None."""
    module_name, _, attribute = path.partition(":")
    if not module_name or not attribute:
        raise ConfigError(f"rules.screen: expected 'module:name', got {path!r}")
    try:
        found = getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as error:
        raise ConfigError(f"rules.screen {path!r} cannot be loaded: {error}") from None
    if not callable(found):
        raise ConfigError(f"rules.screen {path!r} is not a function")
    return found


def load_writer_rules(config: RulesConfig, *, base_dir: str | Path | None = None) -> WriterRules:
    """Read the rules a `RulesConfig` names.

    Relative file names are taken from `base_dir` (the current folder when omitted).
    A package supplies `writer_system.md`, `row_schema.json` and, optionally, a version
    (its `rules_version()` function, else a `VERSION` file), a format block (its
    `json_format()` function, else a `json_format.md` file) and the functions
    `task_text.build_task_text`, `balance.assign`, `form.normalize_output` and
    `form.explain_failures`. The
    checker comes from `config.assessor`, or from the package's default (see
    `DEFAULT_ASSESSORS`); the screen comes from `config.screen`, or from the package's default
    (see `DEFAULT_SCREENS`). The rules returned are the package's default profile; the other
    profiles of a package (see the module notes) are read when `profile_rules` first asks for one.
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
    screen_path = config.screen if config.screen is not None else DEFAULT_SCREENS.get(config.package or "")
    screen = None
    if screen_path and screen_path.lower() != "none":
        screen = make_screen(screen_path)
    rules = WriterRules(
        system=system,
        schema=schema,
        version=version,
        task_text=task_text,
        normalize=normalize,
        assign=assign,
        assessor=assessor,
        json_format=json_format,
        explain=explain,
        screen=screen,
    )
    if config.package:
        return _with_profiles(rules, config.package, root)
    return rules


def _package_profiles(root: Any) -> list[str]:
    """The prompt profiles a rules package holds: the folders under profiles/ that hold a system prompt."""
    folder = root / PROFILES_FOLDER
    try:
        if not folder.is_dir():
            return []
        return sorted(child.name for child in folder.iterdir() if child.is_dir() and (child / DEFAULT_SYSTEM_FILE).is_file())
    except OSError:
        return []


def _with_profiles(rules: WriterRules, package: str, root: Any) -> WriterRules:
    """A package's rules with the loader of its prompt profiles; each profile is read once, when first asked for."""
    loaded: dict[str, WriterRules] = {}

    def loader(name: str) -> WriterRules:
        if name == DEFAULT_PROFILE:
            return base
        if name not in loaded:
            loaded[name] = _package_profile(package, root, base, name)
        return loaded[name]

    base = dataclasses.replace(rules, profile_loader=loader)
    return base


def _profile_text(folder: Any, name: str, where: str, *, optional: bool = False) -> str | None:
    path = folder / name
    try:
        if optional and not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"{where}: cannot read {name}: {error}") from None


def _package_profile(package: str, root: Any, base: WriterRules, name: str) -> WriterRules:
    """The rules of one prompt profile of a package: the profile's own texts on the package's shared parts."""
    known = _package_profiles(root)
    if name not in known:
        raise ConfigError(f"unknown prompt profile {name!r}; the rules package {package!r} has {', '.join([DEFAULT_PROFILE, *known])}")
    where = f"prompt profile {name!r} of the rules package {package!r}"
    folder = root / PROFILES_FOLDER / name
    system = _profile_text(folder, DEFAULT_SYSTEM_FILE, where) or ""
    layout = _profile_text(folder, PROFILE_TASK_FILE, where) or ""
    retry = _profile_text(folder, PROFILE_RETRY_FILE, where) or ""
    hint = _profile_text(folder, PROFILE_HINT_FILE, where, optional=True)
    own_format = _profile_text(folder, DEFAULT_JSON_FORMAT_FILE, where, optional=True)
    openapi_text = _profile_text(folder, PROFILE_OPENAPI_FILE, where, optional=True)
    version_text = _profile_text(folder, DEFAULT_VERSION_FILE, where, optional=True)
    if not layout.strip():
        raise ConfigError(f"{where}: the task layout ({PROFILE_TASK_FILE}) is empty")
    if CHECK_MESSAGES_FIELD not in FIELD_PATTERN.findall(retry):
        raise ConfigError(f"{where}: the retry block has no {{{CHECK_MESSAGES_FIELD}}} field for the check messages")
    openapi: dict[str, Any] | None = None
    if openapi_text is not None:
        try:
            openapi = json.loads(openapi_text)
        except json.JSONDecodeError as error:
            raise ConfigError(f"{where}: {PROFILE_OPENAPI_FILE} is not valid JSON: {error}") from None
        if not isinstance(openapi, dict):
            raise ConfigError(f"{where}: {PROFILE_OPENAPI_FILE} must hold a JSON object")
    builder = _hook(package, "task_text", "profile_task_text")
    if builder is None:
        raise ConfigError(f"{where}: the package has no task_text.profile_task_text to fill the profile's layout")
    task_text = builder(name, layout, hint)
    json_format = clean_json_format(own_format) if own_format is not None else base.json_format
    version = (version_text or "").strip() or _profile_hash(system, base.schema, json_format, layout, retry, hint, openapi)
    return dataclasses.replace(
        base,
        system=system,
        version=version,
        json_format=json_format,
        task_text=task_text,
        profile=name,
        retry=retry,
        openapi_schema=openapi,
    )


def _profile_hash(system: str, schema: Mapping[str, Any], json_format: str | None, *texts: Any) -> str:
    """A short content hash of a profile's texts, for a profile folder without a VERSION file."""
    digest = hashlib.sha256(rules_version(system, schema, json_format).encode("utf-8"))
    for text in texts:
        digest.update(b"\0" + (canonical_json(text) if isinstance(text, Mapping) else str(text or "")).encode("utf-8"))
    return digest.hexdigest()[:12]


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


def retry_text(template: str, feedback: Sequence[str]) -> str:
    """A profile's retry block with the check messages in it, each "- " + the message, as `feedback_text` writes
    them (empty when there are none)."""
    if not feedback:
        return ""
    return fill_fields(template, {CHECK_MESSAGES_FIELD: "\n".join(f"- {line}" for line in feedback)})


# The words that send a row back to its writer (the repair step, see `rps.repair`). One wording for every
# prompt profile: it says nothing about checks, which is what a retry block is about.
REPAIR_INTRO = (
    "An earlier row for this item was sent back by a reviewer. The notes from the review are below. "
    "Write the row again: fix exactly the problems the notes name, and keep everything else in the earlier row "
    "that was right. Change nothing the notes do not mention, and add no fact the item does not give."
)
REPAIR_NOTES_HEAD = "The notes from the review:"
REPAIR_ROW_HEAD = "The earlier row, as it was written:"
REPAIR_CLOSING = (
    "Answer in the form the task above asks for, with the problems fixed: "
    "the complete result, not a patch and not an explanation."
)


def repair_text(feedback: Sequence[str]) -> str:
    """The block that sends a row back to its writer to be written again (empty when there is no feedback).

    `feedback` is what a repair call carries (`rps.repair.repair_feedback`): the reviewers' notes, one item
    each, then the row as it was written (JSON text) as the last item. The block tells the writer that a
    reviewer sent the earlier row back, lists the notes, shows the earlier row, and asks for the row again with
    exactly those problems fixed and everything else that was right kept.
    """
    items = [str(item) for item in feedback]
    if not items:
        return ""
    *notes, earlier = items
    parts = [REPAIR_INTRO]
    if notes:
        parts.append(REPAIR_NOTES_HEAD + "\n" + "\n".join("- " + note.replace("\n", "\n  ") for note in notes))
    parts.append(REPAIR_ROW_HEAD + "\n" + earlier)
    parts.append(REPAIR_CLOSING)
    return "\n\n".join(parts)


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


PICTURE_LINE = "The picture is attached."
# A prompt profile words the line in its own style; a profile not listed here uses the default wording.
PROFILE_PICTURE_LINES: Mapping[str, str] = {
    "openai": "PICTURE\nThe picture is attached.",
    "gemini": "<picture>The picture is attached.</picture>",
}


def picture_line(rules: WriterRules | None) -> str:
    """The line that tells the writer the picture is attached, in the words of the rules' prompt profile."""
    return PROFILE_PICTURE_LINES.get(rules.profile, PICTURE_LINE) if rules is not None else PICTURE_LINE


def render_task_text(
    spec: Any,
    feedback: Sequence[str] = (),
    *,
    ordinal: int = 0,
    rules: WriterRules | None = None,
    repair: bool = False,
    picture: bool = False,
) -> str:
    """The user text of one call: the task text for the spec line, then any check messages.

    The runner's choices (`assign`) are worked out once here and handed to the task text,
    so the text and the assessor's `AssessContext.assignment` carry the same values. Rules with a
    retry block of their own (a prompt profile) follow the task text with it, after one blank line.

    For a call of the repair step (`repair` true) the feedback is not check messages but the reviewers'
    notes and the row that was sent back: the task text is followed by `repair_text` instead, whatever the
    prompt profile.

    With `picture` true (the call carries the picture) the task text ends with one line saying so; a call
    without a picture is rendered exactly as before.
    """
    if rules is not None and rules.task_text is not None:
        choices = assignment_for(spec, ordinal, rules)
        text = rules.task_text(spec, ordinal) if choices is None else rules.task_text(spec, ordinal, choices)
    else:
        text = spec_text(spec)
    if picture:
        text = text + ("\n" if text.endswith("\n") else "\n\n") + picture_line(rules)
    if repair:
        block = repair_text(feedback)
        return text + ("\n" if text.endswith("\n") else "\n\n") + block if block else text
    if rules is not None and rules.retry is not None:
        block = retry_text(rules.retry, feedback)
        if not block:
            return text
        return text + ("\n" if text.endswith("\n") else "\n\n") + block
    return text + feedback_text(feedback)
