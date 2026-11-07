"""The writing rules as inputs: from files or a package, with their optional functions and checker."""

from __future__ import annotations

import json

import pytest

from rps import rules as pool
from rps.config import ConfigError, RulesConfig
from rps.prompting import (
    WriterRules,
    assignment_data,
    feedback_text,
    load_writer_rules,
    make_assessor,
    make_rules,
    render_task_text,
    rules_version,
    spec_text,
)

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}


def write(tmp_path, system="Write json rows.", schema=SCHEMA, version=None):
    (tmp_path / "system.md").write_text(system, encoding="utf-8")
    (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    if version:
        (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
    return RulesConfig(system_file="system.md", schema_file="schema.json", version_file="VERSION" if version else None)


def test_rules_are_read_from_two_files_and_get_a_content_hash_version(tmp_path):
    rules = load_writer_rules(write(tmp_path), base_dir=tmp_path)
    assert rules.system == "Write json rows." and rules.schema == SCHEMA and rules.schema_name == "row"
    assert rules.version == rules_version("Write json rows.", SCHEMA) and len(rules.version) == 12
    assert rules.task_text is None and rules.normalize is None and rules.assign is None and rules.assessor is None


def test_a_changed_system_prompt_or_schema_is_a_new_version(tmp_path):
    first = load_writer_rules(write(tmp_path), base_dir=tmp_path).version
    second = load_writer_rules(write(tmp_path, system="Write other json rows."), base_dir=tmp_path).version
    third = load_writer_rules(write(tmp_path, schema={"type": "object", "properties": {"a": {}}}), base_dir=tmp_path).version
    assert len({first, second, third}) == 3


def test_a_version_in_the_configuration_or_a_version_file_overrides_the_hash(tmp_path):
    config = write(tmp_path, version="2026-11-07a")
    assert load_writer_rules(config, base_dir=tmp_path).version == "2026-11-07a"
    given = RulesConfig(system_file="system.md", schema_file="schema.json", version="pinned")
    assert load_writer_rules(given, base_dir=tmp_path).version == "pinned"


def test_missing_or_broken_files_are_configuration_errors(tmp_path):
    with pytest.raises(ConfigError, match="cannot read the system prompt file"):
        load_writer_rules(RulesConfig(system_file="nope.md", schema_file="nope.json"), base_dir=tmp_path)
    (tmp_path / "system.md").write_text("x", encoding="utf-8")
    (tmp_path / "schema.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_writer_rules(RulesConfig(system_file="system.md", schema_file="schema.json"), base_dir=tmp_path)
    (tmp_path / "schema.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be a JSON object"):
        load_writer_rules(RulesConfig(system_file="system.md", schema_file="schema.json"), base_dir=tmp_path)


def test_the_rule_pool_package_brings_its_files_version_functions_and_assessor():
    rules = load_writer_rules(RulesConfig(package="rps.rules"))
    assert rules.system == pool.writer_system() and rules.schema == pool.row_schema()
    assert rules.version == pool.rules_version()
    assert callable(rules.task_text) and callable(rules.normalize) and callable(rules.assign)
    from rps.assessor import Assessor

    assert isinstance(rules.assessor, Assessor)


def test_a_package_that_does_not_exist_is_a_configuration_error():
    with pytest.raises(ConfigError, match="cannot be imported"):
        load_writer_rules(RulesConfig(package="rps.no_such_rules"))


def test_the_assessor_can_be_named_replaced_or_turned_off():
    named = load_writer_rules(RulesConfig(package="rps.rules", assessor="rps.assessor:SimilarityIndex"))
    assert type(named.assessor).__name__ == "SimilarityIndex"
    assert load_writer_rules(RulesConfig(package="rps.rules", assessor="none")).assessor is None
    assert load_writer_rules(RulesConfig(package="rps.rules", assessor="None")).assessor is None


def test_an_assessor_that_cannot_be_loaded_or_built_is_a_configuration_error():
    with pytest.raises(ConfigError, match="cannot be loaded"):
        make_assessor("rps.no_such_module:Thing")
    with pytest.raises(ConfigError, match="cannot be loaded"):
        make_assessor("rps.assessor:NoSuchName")
    with pytest.raises(ConfigError, match="expected 'module:name'"):
        make_assessor("no_colon")
    with pytest.raises(ConfigError, match="could not be built"):
        make_assessor("rps.prompting:make_rules")  # called with no arguments, it needs some


def test_rules_need_a_system_prompt_a_schema_and_a_version():
    with pytest.raises(ConfigError, match="system prompt"):
        make_rules(" ", SCHEMA)
    with pytest.raises(ConfigError, match="JSON schema"):
        make_rules("x", {})
    with pytest.raises(ConfigError, match="version"):
        WriterRules(system="x", schema=SCHEMA, version=" ")
    with pytest.raises(ConfigError, match="schema name"):
        make_rules("x", SCHEMA, schema_name="has spaces")


def test_without_a_task_text_function_a_spec_goes_as_text_or_json():
    assert render_task_text("a plain spec") == "a plain spec"
    assert json.loads(render_task_text({"b": 1, "a": [1, 2]})) == {"a": [1, 2], "b": 1}
    assert spec_text({"b": 1, "a": 2}).index('"a"') < spec_text({"b": 1, "a": 2}).index('"b"')


def test_check_messages_follow_the_task_text_when_an_attempt_failed():
    text = render_task_text("a plain spec", ["rule_3: too many thoughts", "ids: wrong id"])
    assert text.startswith("a plain spec") and "failed these checks" in text
    assert "- rule_3: too many thoughts" in text and "- ids: wrong id" in text
    assert feedback_text([]) == ""


def test_the_task_text_function_gets_the_spec_the_ordinal_and_the_same_choices_the_assessor_sees():
    seen = []

    def assign(spec, ordinal):
        return {"part": "abc"[ordinal % 3], "spec": spec}

    def task(spec, ordinal, choices=None):
        seen.append((spec, ordinal, choices))
        return f"task {spec} #{ordinal} choice {choices['part']}"

    rules = make_rules("sys", SCHEMA, "v1", task_text=task, assign=assign)
    assert render_task_text("s", ["oops"], ordinal=4, rules=rules).startswith("task s #4 choice b")
    assert seen == [("s", 4, {"part": "b", "spec": "s"})]
    assert assignment_data(assign("s", 4)) == {"part": "b", "spec": "s"}


def test_a_task_text_function_without_choices_is_called_with_two_arguments():
    rules = make_rules("sys", SCHEMA, "v1", task_text=lambda spec, ordinal: f"{spec}/{ordinal}")
    assert render_task_text("s", ordinal=2, rules=rules) == "s/2"


def test_assignment_data_accepts_an_object_with_to_dict_a_mapping_or_nothing():
    class Choice:
        def to_dict(self):
            return {"a": 1}

    assert assignment_data(Choice()) == {"a": 1}
    assert assignment_data({"b": 2}) == {"b": 2}
    assert assignment_data(None) is None and assignment_data("text") is None


def test_the_pools_task_text_and_assignment_agree_for_every_example():
    rules = load_writer_rules(RulesConfig(package="rps.rules"))
    from rps.rules import balance, task_text

    for ordinal, example in enumerate(pool.examples()):
        item = example["item"]
        assert render_task_text(item, ordinal=ordinal, rules=rules) == task_text.build_task_text(item, ordinal)
        assert assignment_data(rules.assign(item, ordinal)) == balance.assign(item, ordinal).to_dict()
