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

Nothing is rewritten: a schema that fails is reported with the path of each problem.
"""

from __future__ import annotations

from typing import Any, Mapping

ANTHROPIC = "anthropic"
OPENAI = "openai"
GEMINI = "gemini"
DEEPSEEK = "deepseek"

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
