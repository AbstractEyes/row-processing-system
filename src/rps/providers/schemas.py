"""Checks that a JSON schema is one a provider's structured-output mode will take.

A provider that rejects a schema rejects every request that carries it, so the checks run
once, before anything is sent. They cover the rules the specifications state:

    Anthropic   every object has `additionalProperties: false`; no recursive schemas; none of
                the numeric, string-length or item-count limits (`minItems` of 0 or 1 is the
                one allowed); the assessor enforces such limits instead
    OpenAI      strict mode: every object has `additionalProperties: false` and lists every
                one of its properties in `required`
    Gemini      the root is an object (the provider takes the rest as given)
    DeepSeek    takes no schema (JSON mode); the answer is checked by the assessor

Nothing is rewritten by the checks: a schema that fails is reported with the path of each problem.
`gemini_schema` is the one translation here: the form of a schema that is sent to Gemini.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

ANTHROPIC = "anthropic"
OPENAI = "openai"
GEMINI = "gemini"
DEEPSEEK = "deepseek"

# The forms in which a schema can be sent to Gemini (see `gemini_schema`).
GEMINI_SCHEMA_MODES = ("plain", "exact", "none", "openapi")

_ANTHROPIC_FORBIDDEN = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "maxItems",
    "uniqueItems",
)
_MAX_PROBLEMS = 12


class SchemaError(ValueError):
    """A schema a provider would reject."""


def _resolve(root: Mapping[str, Any], ref: str) -> Mapping[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: Any = root
    for part in ref[2:].split("/"):
        node = node.get(part) if isinstance(node, Mapping) else None
        if node is None:
            return None
    return node if isinstance(node, Mapping) else None


def schema_problems(schema: Mapping[str, Any], provider: str) -> list[str]:
    """Every problem a provider's structured-output mode would have with a schema (empty when none)."""
    problems: list[str] = []

    def add(path: str, text: str) -> None:
        if len(problems) < _MAX_PROBLEMS:
            problems.append(f"{path or '(root)'}: {text}")

    def walk(node: Any, path: str, stack: tuple[str, ...]) -> None:
        if not isinstance(node, Mapping):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in stack:
                if provider == ANTHROPIC:
                    add(path, f"the schema is recursive through {ref}")
                return
            target = _resolve(schema, ref)
            if target is None:
                add(path, f"cannot resolve {ref}")
            else:
                walk(target, path, stack + (ref,))
            return
        if provider == ANTHROPIC:
            for keyword in _ANTHROPIC_FORBIDDEN:
                if keyword in node:
                    add(path, f"`{keyword}` is not supported")
            if "minItems" in node and node["minItems"] not in (0, 1):
                add(path, "`minItems` may only be 0 or 1")
        is_object = node.get("type") == "object" or "properties" in node
        if is_object and provider in (ANTHROPIC, OPENAI):
            if node.get("additionalProperties") is not False:
                add(path, "an object needs `additionalProperties: false`")
            if provider == OPENAI:
                properties = node.get("properties") or {}
                missing = [name for name in properties if name not in (node.get("required") or [])]
                if missing:
                    add(path, f"strict mode needs every property in `required`; missing {missing[:6]}")
        for name, child in (node.get("properties") or {}).items():
            walk(child, f"{path}.{name}" if path else name, stack)
        items = node.get("items")
        if isinstance(items, Mapping):
            walk(items, f"{path}[]", stack)
        elif isinstance(items, list):
            for index, child in enumerate(items):
                walk(child, f"{path}[{index}]", stack)
        for keyword in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for index, child in enumerate(node.get(keyword) or []):
                walk(child, f"{path}.{keyword}[{index}]", stack)

    # Definitions are checked where they are referenced; one that nothing references is never sent.
    if not isinstance(schema, Mapping) or not schema:
        return ["(root): the schema is empty or not an object"]
    if schema.get("type") != "object" and "properties" not in schema and "$ref" not in schema:
        add("", "the root must be an object schema")
    if provider != DEEPSEEK:
        walk(schema, "", ())
    return problems


def check_schema(schema: Mapping[str, Any], provider: str) -> None:
    """Raise SchemaError, listing the problems, unless the provider would take the schema."""
    problems = schema_problems(schema, provider)
    if problems:
        shown = "; ".join(problems)
        more = " (more not shown)" if len(problems) >= _MAX_PROBLEMS else ""
        raise SchemaError(f"the row schema would be rejected by {provider}: {shown}{more}")


# ---------------------------------------------------------------------------- Gemini

# Keywords whose value maps names to schemas, holds a list of schemas, or is one schema.
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_ONE = ("items", "additionalProperties", "not")


def _allowed_values(said: Any, allowed: list[Any]) -> str:
    """A description with the allowed values of a field written out after it."""
    listed = "Allowed values: " + ", ".join(json.dumps(value, ensure_ascii=False) for value in allowed) + "."
    return f"{str(said or '').strip()} {listed}".strip()


def _plain(node: Any) -> Any:
    """A schema without its value lists: each `enum` (and `const`) moves into the node's description."""
    if not isinstance(node, Mapping):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("enum", "const"):
            continue
        if key in _SCHEMA_MAPS and isinstance(value, Mapping):
            out[key] = {name: _plain(child) for name, child in value.items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_plain(child) for child in value]
        elif key in _SCHEMA_ONE:
            out[key] = _plain(value)
        else:
            out[key] = copy.deepcopy(value)
    allowed = node.get("enum") if isinstance(node.get("enum"), list) else ([node["const"]] if "const" in node else [])
    if allowed:
        out["description"] = _allowed_values(node.get("description"), allowed)
    return out


# The keywords of the OpenAPI Schema object, which is what the `response_schema` field takes. Google's API
# reference lists exactly these. The SDK's `Schema` type has three more (`additionalProperties`, `$defs`, `$ref`)
# that only the other Google platform reads: the Gemini API refuses them by name ("Unknown name
# "additional_properties""), even as `false`.
OPENAPI_KEYWORDS = (
    "type",
    "format",
    "title",
    "description",
    "nullable",
    "enum",
    "items",
    "maxItems",
    "minItems",
    "properties",
    "required",
    "minProperties",
    "maxProperties",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
    "example",
    "anyOf",
    "propertyOrdering",
    "default",
)
_OPENAPI_TEXT = ("title", "description", "format", "pattern")
_OPENAPI_COUNTS = ("maxItems", "minItems", "maxProperties", "minProperties", "minLength", "maxLength")


def _openapi(node: Any, root: Mapping[str, Any], stack: tuple[str, ...], path: str) -> dict[str, Any]:
    """A schema as the OpenAPI Schema object of the Gemini API: only the keywords that object has."""
    if not isinstance(node, Mapping):
        raise SchemaError(f"{path or '(root)'}: a schema object is needed here")
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in stack:
            raise SchemaError(f"{path or '(root)'}: the schema is recursive through {ref}, which the OpenAPI form cannot hold")
        target = _resolve(root, ref)
        if target is None:
            raise SchemaError(f"{path or '(root)'}: cannot resolve {ref}")
        return _openapi({**target, **{key: value for key, value in node.items() if key != "$ref"}}, root, stack + (ref,), path)
    out: dict[str, Any] = {}
    kind = node.get("type")
    if isinstance(kind, list):  # JSON Schema allows a list of types; the OpenAPI object has `nullable` instead
        named = [name for name in kind if isinstance(name, str)]
        others = [name for name in named if name != "null"]
        if len(others) > 1:
            raise SchemaError(f"{path or '(root)'}: a list of types ({others}) cannot be held; write the choices as anyOf")
        if "null" in named:
            out["nullable"] = True
        kind = others[0] if others else "null"
    if isinstance(kind, str):
        out["type"] = kind
    for name in _OPENAPI_TEXT:
        if isinstance(node.get(name), str):
            out[name] = node[name]
    if isinstance(node.get("nullable"), bool):
        out["nullable"] = node["nullable"]
    for name in ("minimum", "maximum"):
        if isinstance(node.get(name), (int, float)) and not isinstance(node[name], bool):
            out[name] = node[name]
    for name in _OPENAPI_COUNTS:
        if isinstance(node.get(name), int) and not isinstance(node[name], bool):
            out[name] = node[name]
    for name in ("example", "default"):
        if name in node:
            out[name] = copy.deepcopy(node[name])
    for name in ("required", "propertyOrdering"):
        if isinstance(node.get(name), list):
            out[name] = [item for item in node[name] if isinstance(item, str)]
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        out["properties"] = {
            str(name): _openapi(child, root, stack, f"{path}.{name}" if path else str(name)) for name, child in properties.items()
        }
    if isinstance(node.get("items"), Mapping):
        out["items"] = _openapi(node["items"], root, stack, f"{path}[]")
    choices = node.get("anyOf") if isinstance(node.get("anyOf"), list) else node.get("oneOf")
    if isinstance(choices, list):
        out["anyOf"] = [_openapi(child, root, stack, f"{path}.anyOf[{index}]") for index, child in enumerate(choices)]
    # A value list stays a list only where the field can hold it: text values, none of them empty, on a text field.
    allowed = node.get("enum") if isinstance(node.get("enum"), list) else ([node["const"]] if "const" in node else [])
    if allowed:
        if out.get("type", "string") == "string" and all(isinstance(value, str) and value for value in allowed):
            out["enum"] = list(allowed)
            out.setdefault("type", "string")
        else:
            out["description"] = _allowed_values(out.get("description"), allowed)
    if "type" not in out and "anyOf" not in out:
        if "properties" in out:
            out["type"] = "object"
        elif "items" in out:
            out["type"] = "array"
    return out


def gemini_schema(schema: Mapping[str, Any], mode: str = "plain") -> dict[str, Any] | None:
    """The schema in the form that is sent to Gemini as its answer format.

        plain   the structure (types, nesting, required lists, `additionalProperties`) with every
                list of allowed values moved into the description of the field it belonged to.
                Gemini turns a schema into a decoding grammar, and a request it refuses in a batch
                comes back with an error that names no field; its own advice for a schema refused
                as too heavy includes fewer enum values. The assessor still checks every value
        exact   the schema as it is
        none    no schema: the answer is asked for as JSON and the writing rules describe its form
        openapi the schema as the `response_schema` field takes it, which is an OpenAPI Schema object: only the
                keywords that object has (`additionalProperties` and the other JSON Schema keywords it lacks are
                left out at every depth; `$ref` is written out in place), and a value list kept only where it is
                a list of non-empty text values on a text field (any other moves into the description, as in
                `plain`). Every `required` list is kept. A schema that cannot be written so (a recursive `$ref`,
                a list of several types) raises SchemaError

    The schema passed in is never changed.
    """
    if mode == "plain":
        return _plain(schema)
    if mode == "exact":
        return copy.deepcopy(dict(schema))
    if mode == "none":
        return None
    if mode == "openapi":
        return _openapi(schema, schema, (), "")
    raise ValueError(f"unknown schema mode {mode!r}; use one of {list(GEMINI_SCHEMA_MODES)}")
