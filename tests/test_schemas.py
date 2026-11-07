"""Schema checks per provider, and the rule pool's schema against all of them."""

import pytest

from rps import rules
from rps.providers.schemas import SchemaError, check_schema, schema_problems

STRICT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["a", "b"],
    "properties": {"a": {"type": "string"}, "b": {"type": "array", "items": {"type": "string"}}},
}


def test_a_strict_schema_is_accepted_everywhere():
    for provider in ("anthropic", "openai", "gemini", "deepseek"):
        assert schema_problems(STRICT, provider) == []


def test_the_rule_pools_row_schema_is_accepted_everywhere():
    for provider in ("anthropic", "openai", "gemini", "deepseek"):
        assert schema_problems(rules.row_schema(), provider) == []


def test_an_open_object_is_refused_by_anthropic_and_openai_but_not_gemini():
    open_schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert any("additionalProperties" in p for p in schema_problems(open_schema, "anthropic"))
    assert any("additionalProperties" in p for p in schema_problems(open_schema, "openai"))
    assert schema_problems(open_schema, "gemini") == []


def test_openai_strict_mode_wants_every_property_required():
    schema = dict(STRICT, required=["a"])
    problems = schema_problems(schema, "openai")
    assert len(problems) == 1 and "required" in problems[0] and "['b']" in problems[0]
    assert schema_problems(schema, "anthropic") == []


def test_nested_objects_are_checked_with_their_path():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rows"],
        "properties": {"rows": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}},
    }
    problems = schema_problems(schema, "anthropic")
    assert problems and problems[0].startswith("rows[]:")


@pytest.mark.parametrize("keyword, value", [("minimum", 0), ("maxLength", 10), ("maxItems", 3), ("multipleOf", 2), ("uniqueItems", True)])
def test_anthropic_refuses_limits_it_does_not_support(keyword, value):
    schema = {**STRICT, "properties": {"a": {"type": "string", keyword: value}, "b": STRICT["properties"]["b"]}}
    assert any(keyword in p for p in schema_problems(schema, "anthropic"))
    assert schema_problems(schema, "gemini") == []


def test_anthropic_allows_min_items_of_zero_or_one_only():
    def with_min_items(value):
        return {**STRICT, "properties": {"a": {"type": "string"}, "b": {"type": "array", "items": {"type": "string"}, "minItems": value}}}

    assert schema_problems(with_min_items(1), "anthropic") == []
    assert any("minItems" in p for p in schema_problems(with_min_items(3), "anthropic"))


def test_a_recursive_schema_is_refused_by_anthropic():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["node"],
        "properties": {"node": {"$ref": "#/$defs/node"}},
        "$defs": {
            "node": {
                "type": "object",
                "additionalProperties": False,
                "required": ["child"],
                "properties": {"child": {"$ref": "#/$defs/node"}},
            }
        },
    }
    assert any("recursive" in p for p in schema_problems(schema, "anthropic"))


def test_a_reference_is_followed_and_a_broken_one_is_reported():
    ok = {
        "type": "object",
        "additionalProperties": False,
        "required": ["x"],
        "properties": {"x": {"$ref": "#/$defs/inner"}},
        "$defs": {"inner": {"type": "object", "additionalProperties": False, "required": [], "properties": {}}},
    }
    assert schema_problems(ok, "openai") == []
    broken = dict(ok, properties={"x": {"$ref": "#/$defs/missing"}})
    assert any("cannot resolve" in p for p in schema_problems(broken, "openai"))


def test_the_root_must_be_an_object_schema():
    assert schema_problems({"type": "array", "items": {}}, "gemini")
    assert schema_problems({}, "gemini")


def test_check_schema_raises_with_the_problems_listed():
    with pytest.raises(SchemaError, match="would be rejected by anthropic"):
        check_schema({"type": "object", "properties": {}}, "anthropic")
    check_schema(STRICT, "anthropic")
