"""Prompt profiles: other wordings of the writer prompt, one per route that names it.

The default profile stays the prompt of the release before profiles, to the byte. Each profile
compiles like the default, fills its own user-message layout (the sample written with it, to the byte),
follows a failed answer with its own retry block, and has its own version. A route names its
profile; the calls, rows and packet records carry it; the Gemini profile sends its described
OpenAPI form, which must keep the row schema's structure. Every item here is invented, except the
drawn series the profiles' samples were written for.
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import json
import re
import shutil
from fnmatch import fnmatch
from pathlib import Path

import pytest
from google.genai import types

from rps import rules as pool
from rps.config import Config, ConfigError, RouteConfig, RulesConfig
from rps.ids import canonical_json
from rps.ledger import PriceTable
from rps.prompting import FIELD_PATTERN, load_writer_rules, make_rules, profile_rules, render_task_text
from rps.providers import CallSpec, DeepSeekWriter, FakeOutcome, FakeWriter, GeminiBatchWriter, GeminiSyncWriter, OpenAISyncWriter, make_writer
from rps.providers.common import estimate_text_tokens
from rps.providers.deepseek_api import system_text
from rps.providers.schemas import described_openapi_problems, gemini_schema, without_descriptions
from rps.rules import balance, compile as rules_compile, task_text
from rps.rules.balance import WORDINGS_BY_DEPTH, Assignment
from rps.tasks import SpecLine, TaskSpec

from adapter_helpers import Namespace, Recorder, call as adapter_call
from harness import World, keep_all
from helpers import ROUTE_CAPS, make_config

DATA = Path(__file__).parent / "data" / "profiles"
ITEMS = json.loads((DATA / "items.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((DATA / "default_texts.json").read_text(encoding="utf-8"))
RULES_DIR = Path(pool.__file__).parent
PROFILES = ("openai", "gemini", "deepseek")
TODAYS_VERSION = "2b7a19bd4813"
GEMINI_MODEL = "gemini-3.8-flash"


@functools.lru_cache(maxsize=None)
def base_rules():
    return load_writer_rules(RulesConfig(package="rps.rules"))


def rules_of(profile):
    return profile_rules(base_rules(), profile)


def entry(name):
    return next(e for e in ITEMS["items"] if e["name"] == name)


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sample_plan():
    sample = entry("drawn_series_sample")["sample_assignment"]
    return Assignment(relation_part=sample["relation_part"], unknown_keys=dict(sample["unknown_keys"]), wordings=dict(WORDINGS_BY_DEPTH))


def item_names():
    return [e["name"] for e in ITEMS["items"]]


# ---------------------------------------------------------------------------- the default profile is today's prompt


def test_the_default_profile_is_the_prompt_of_the_release_before_profiles():
    rules = base_rules()
    assert rules.profile == "default" and rules.retry is None and rules.openapi_schema is None
    assert rules.version == pool.rules_version() == GOLDEN["version"] == TODAYS_VERSION
    assert sha256(rules.system) == GOLDEN["system_sha256"] and rules.system == pool.writer_system()
    assert sha256(rules.json_format) == GOLDEN["json_format_sha256"]
    assert profile_rules(rules, "default") is rules


@pytest.mark.parametrize("name", item_names())
def test_the_default_task_and_retry_texts_are_those_of_the_release_before_profiles(name):
    item, ordinal = entry(name)["item"], entry(name)["ordinal"]
    want = GOLDEN["texts"][name]
    assert render_task_text(item, ordinal=ordinal, rules=base_rules()) == want["task"]
    assert render_task_text(item, ITEMS["feedback"], ordinal=ordinal, rules=base_rules()) == want["retry"]
    assert task_text.build_task_text(item, ordinal) == want["task"]


# ---------------------------------------------------------------------------- each profile renders the sample written with it


@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_renders_the_sample_written_with_it_byte_for_byte(profile):
    sample = entry("drawn_series_sample")
    want = (DATA / f"{profile}_sample.txt").read_bytes().decode("utf-8")
    assert rules_of(profile).task_text(sample["item"], sample["ordinal"], sample_plan()) == want


def test_the_sample_fixture_is_a_drawn_series_of_all_three_depths():
    item = entry("drawn_series_sample")["item"]
    assert item["kind"] == "series" and not item.get("seed_id") and list(item["specs"]) == ["simple", "medium", "deep"]


# ---------------------------------------------------------------------------- compiled prompts and filled layouts


@pytest.mark.parametrize("profile", PROFILES)
def test_every_profile_compiles_with_no_placeholder_left(profile):
    rules = rules_of(profile)
    assert "{{" not in rules.system
    assert rules.system == rules_compile.profile_system(profile)  # what is committed is what compiles
    assert rules.system.endswith("\n") and not rules.system.endswith("\n\n")
    from thought_seeds.core import HELD, tp

    for filled in (", ".join(tp.STEPS), rules_compile.genres(tp, HELD), rules_compile.list_text(tp, HELD), rules_compile.sealed_text(tp, HELD)):
        assert filled in rules.system
    template = (RULES_DIR / "profiles" / profile / "writer_system.tpl.md").read_text(encoding="utf-8")
    assert rules.system.count(rules_compile.sealed_text(tp, HELD)) == template.count("{{SEALED}}")


def test_the_gemini_prompt_carries_its_sealed_list_at_each_of_its_three_places():
    from thought_seeds.core import HELD, tp

    assert rules_of("gemini").system.count(rules_compile.sealed_text(tp, HELD)) == 3


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("name", item_names())
def test_no_field_is_left_unfilled_in_a_profiles_task_or_retry_text(profile, name):
    item, ordinal = entry(name)["item"], entry(name)["ordinal"]
    for feedback in ((), ITEMS["feedback"]):
        text = render_task_text(item, feedback, ordinal=ordinal, rules=rules_of(profile))
        assert not FIELD_PATTERN.findall(text), (profile, name, FIELD_PATTERN.findall(text))
        assert "<check message>" not in text


def test_a_layout_that_names_a_field_no_profile_fills_is_refused_before_any_call():
    with pytest.raises(ConfigError, match="not filled: \\['colour_line'\\]"):
        task_text.profile_task_text("openai", "TASK\n{series_id}\n{colour_line}\n")
    with pytest.raises(ConfigError, match="hint block"):
        task_text.profile_task_text("gemini", "{hint_block}\n")
    with pytest.raises(ConfigError, match="no task text for the prompt profile 'mystery'"):
        task_text.profile_task_text("mystery", "{series_id}\n")


def test_a_filled_text_is_not_read_again_for_fields():
    item = copy.deepcopy(entry("seeded_single_simple")["item"])
    item["hint"] = "A note pinned to the door reads {series_id} and {hint_block} in chalk."
    for profile in PROFILES:
        text = render_task_text(item, ordinal=5, rules=rules_of(profile))
        assert "reads {series_id} and {hint_block} in chalk." in text


# ---------------------------------------------------------------------------- the fields each profile words its own way


def test_the_deepseek_layout_says_what_a_real_picture_allows_and_gives_its_hint():
    rules = rules_of("deepseek")
    seeded = entry("seeded_series")
    text = rules.task_text(seeded["item"], seeded["ordinal"])
    assert "Source: real picture; the records are facts about it and the hint below is evidence, never text to copy." in text
    assert f"\nHint: {seeded['item']['hint']}\n\nRecords to keep (key = value):\n" in text
    assert "deep (the picture itself; cut the lower ranks from it):\n" in text and "  relation 1: bench | facing | fountain" in text
    assert "Ranks and depths: 3 = deep.\n" in text and "Genre: architecture.\n" in text
    single = entry("seeded_single_simple")
    text = rules.task_text(single["item"], single["ordinal"])
    assert "Write one single row. Series id s-t0006. Row ids in order: art-t00012.\nRanks and depths: 1 = simple.\n" in text
    assert "- Unknown question key by depth: none.\n" in text  # a simple row is assigned no unknown key


def test_the_openai_layout_lists_records_and_relations_apart_and_words_the_choices_its_own_way():
    rules = rules_of("openai")
    seeded = entry("seeded_series")
    plan = balance.assign(seeded["item"], seeded["ordinal"])
    text = rules.task_text(seeded["item"], seeded["ordinal"])
    assert "Record layout: deep records only (the picture itself; cut simple and medium from them)\n" in text
    assert "PICTURE EVIDENCE\nReal picture. The records are facts about it;" in text
    assert f"Keep every record; add facts only where the rules allow.\nHint: {seeded['item']['hint']}\n\nRECORDS TO KEEP\n" in text
    records = text.split("RECORDS TO KEEP\n")[1].split("\n\nRELATIONS TO KEEP\n")[0]
    assert "relation" not in records and "  thing name = fountain" in records
    assert "RELATIONS TO KEEP\ndeep:\n  relation 1: bench | facing | fountain\n\n" in text
    assert f"Relation question: rows that hold a relation ask 'rel1 {plan.relation_part}'.\n" in text
    keys = "; ".join(f"{depth} = '{key}'" for depth, key in plan.unknown_keys.items())
    assert f"Unknown questions by depth: {keys}. Keep each key absent and unanswered in its assigned row.\n" in text
    relations = entry("drawn_series_relations")
    text = rules.task_text(relations["item"], relations["ordinal"])
    assert "RELATIONS TO KEEP\nmedium:\n  relation 1: bottle | beside | clock\ndeep:\n  relation 1: bottle | beside | clock\n  relation 2: clock | below | lamp\n" in text
    assert "Genre: still life.\n" in text and "Record layout: per-depth records\n" in text
    off = entry("seeded_single_off_list_genre")
    text = rules.task_text(off["item"], off["ordinal"])
    assert 'Genre: the drawn genre "teapot" is not an allowed genre (it is a topic or an object name, not a genre); choose the fitting genre from the allowed genre choices.\n' in text
    assert "Write one single row.\n" in text and "Record layout: one row's records\n" in text and "Ranks and depths in the same order: 1 = medium\n" in text
    only = entry("relations_only_single")
    text = rules.task_text(only["item"], only["ordinal"])
    assert "RECORDS TO KEEP\n  (no records)\n\nRELATIONS TO KEEP\n  relation 1: cat | inside | basket\n" in text
    simple = entry("seeded_single_simple")
    assert "Unknown questions by depth: none.\n" in rules.task_text(simple["item"], simple["ordinal"])


def test_the_gemini_layout_puts_a_real_pictures_hint_in_its_evidence_block():
    rules = rules_of("gemini")
    seeded = entry("seeded_series")
    text = rules.task_text(seeded["item"], seeded["ordinal"])
    block = (RULES_DIR / "profiles" / "gemini" / "hint.tpl.txt").read_text(encoding="utf-8").rstrip("\n")
    assert "</task_spec>\n" + block.replace("{hint_text}", seeded["item"]["hint"]) + "\n<seed_records>\n" in text
    assert "Task: Write one series.\n" in text and "Genre rule: architecture.\n" in text
    drawn = entry("drawn_series")
    text = rules.task_text(drawn["item"], drawn["ordinal"])
    assert "</task_spec>\n\n<seed_records>\n" in text and "<source_evidence>" not in text
    single = entry("drawn_single_relation")
    assert "Task: Write one single row.\n" in rules.task_text(single["item"], single["ordinal"])


# ---------------------------------------------------------------------------- the retry block


@pytest.mark.parametrize("profile", PROFILES)
def test_a_profiles_retry_block_follows_the_task_text_with_the_check_lines_in_it(profile):
    rules = rules_of(profile)
    item, ordinal = entry("drawn_series")["item"], entry("drawn_series")["ordinal"]
    task = render_task_text(item, ordinal=ordinal, rules=rules)
    retry = render_task_text(item, ITEMS["feedback"], ordinal=ordinal, rules=rules)
    template = (RULES_DIR / "profiles" / profile / "retry.tpl.txt").read_text(encoding="utf-8")
    lines = "\n".join(f"- {line}" for line in ITEMS["feedback"])
    assert template.count("{check_messages}") == 1 and "<check message>" not in template
    assert retry == task + "\n" + template.replace("{check_messages}", lines)
    assert task.endswith("\n") and "\n\n\n" not in retry and retry.endswith("\n")
    assert rules.retry == template
    assert "failed these checks. Answer again and fix every one of them" not in retry  # not the default's block


def toy_package(root, name, *, retry="Fix these:\n{check_messages}\n"):
    """A small rules package of its own with one prompt profile, "terse", that has no VERSION file."""
    package = root / name
    folder = package / "profiles" / "terse"
    folder.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "writer_system.md").write_text("Write the rows as json.\n", encoding="utf-8")
    (package / "row_schema.json").write_text(json.dumps({"type": "object", "properties": {}}), encoding="utf-8")
    (package / "task_text.py").write_text(
        "from rps.prompting import fill_fields\n\n\n"
        "def build_task_text(item, ordinal, assignment=None):\n    return f'Item {item}'\n\n\n"
        "def profile_task_text(name, layout, hint_layout=None):\n"
        "    return lambda item, ordinal, assignment=None: fill_fields(layout, {'item': str(item)})\n",
        encoding="utf-8",
    )
    (folder / "writer_system.md").write_text("Write terse rows as json.\n", encoding="utf-8")
    (folder / "task.tpl.txt").write_text("Item: {item}\n", encoding="utf-8")
    (folder / "retry.tpl.txt").write_text(retry, encoding="utf-8")
    return package


def test_any_rules_package_can_hold_profiles_and_a_profile_without_a_version_file_gets_a_content_hash(tmp_path, monkeypatch):
    toy_package(tmp_path, "toy_rules_one")
    monkeypatch.syspath_prepend(str(tmp_path))
    base = load_writer_rules(RulesConfig(package="toy_rules_one"))
    terse = profile_rules(base, "terse")
    assert terse.profile == "terse" and terse.system == "Write terse rows as json.\n" and terse.schema == base.schema
    assert re.fullmatch(r"[0-9a-f]{12}", terse.version) and terse.version != base.version
    assert render_task_text("a lamp", ordinal=0, rules=base) == "Item a lamp"
    assert render_task_text("a lamp", ordinal=0, rules=terse) == "Item: a lamp\n"
    assert render_task_text("a lamp", ["one: two"], ordinal=0, rules=terse) == "Item: a lamp\n\nFix these:\n- one: two\n"
    assert profile_rules(terse, "default") is base and profile_rules(base, "terse") is terse  # read once
    with pytest.raises(ConfigError, match="unknown prompt profile 'loud'; the rules package 'toy_rules_one' has default, terse"):
        profile_rules(base, "loud")


def test_a_retry_block_without_its_field_is_refused(tmp_path, monkeypatch):
    toy_package(tmp_path, "toy_rules_two", retry="Fix the checks above.\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ConfigError, match="retry block has no \\{check_messages\\} field"):
        profile_rules(load_writer_rules(RulesConfig(package="toy_rules_two")), "terse")


# ---------------------------------------------------------------------------- versions


def test_each_profile_has_its_own_version_and_the_default_keeps_todays():
    base = base_rules()
    versions = {name: rules_of(name).version for name in PROFILES}
    assert base.version == TODAYS_VERSION and len({base.version, *versions.values()}) == 4
    for name, version in versions.items():
        assert re.fullmatch(r"[0-9a-f]{12}", version)
        assert version == rules_compile.profile_version(name) == (RULES_DIR / "profiles" / name / "VERSION").read_text(encoding="utf-8").strip()
    assert sorted(pool.profile_names()) == sorted(PROFILES) == rules_compile.profile_names()


def test_a_profiles_version_follows_its_own_files_and_the_shared_ones(tmp_path):
    copied = tmp_path / "rules"
    shutil.copytree(RULES_DIR, copied, ignore=shutil.ignore_patterns("__pycache__"))
    before = {name: rules_compile.profile_version(name, copied) for name in PROFILES}
    default = rules_compile.rules_version(copied)
    assert default == TODAYS_VERSION and before == {name: rules_of(name).version for name in PROFILES}
    layout = copied / "profiles" / "openai" / "task.tpl.txt"
    layout.write_text(layout.read_text(encoding="utf-8") + "One more line.\n", encoding="utf-8")
    after = {name: rules_compile.profile_version(name, copied) for name in PROFILES}
    assert after["openai"] != before["openai"] and after["gemini"] == before["gemini"] and after["deepseek"] == before["deepseek"]
    assert rules_compile.rules_version(copied) == default  # the default prompt does not depend on the profiles
    brief = copied / "review_brief.md"
    brief.write_text(brief.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    shared = {name: rules_compile.profile_version(name, copied) for name in PROFILES}
    assert all(shared[name] != after[name] for name in PROFILES) and rules_compile.rules_version(copied) != default


def test_the_compile_check_covers_the_profiles(tmp_path, monkeypatch, capsys):
    copied = tmp_path / "rules"
    shutil.copytree(RULES_DIR, copied, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(rules_compile, "HERE", copied)
    assert rules_compile.main(["--check"]) == 0
    assert {f"profiles/{name}/writer_system.md" for name in PROFILES} <= set(rules_compile.outputs())
    system = copied / "profiles" / "gemini" / "writer_system.md"
    system.write_text(system.read_text(encoding="utf-8") + "stale\n", encoding="utf-8")
    capsys.readouterr()
    assert rules_compile.main(["--check"]) == 1
    assert "profiles/gemini/writer_system.md" in capsys.readouterr().err
    assert rules_compile.main([]) == 0 and rules_compile.main(["--check"]) == 0  # compiling writes it back


def test_the_profile_files_ship_with_the_package():
    text = (RULES_DIR.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    listed = re.search(r"^rps = \[(.*?)\]", text, flags=re.S | re.M).group(1)
    patterns = re.findall(r'"([^"]+)"', listed)
    files = [p.relative_to(RULES_DIR.parent).as_posix() for p in (RULES_DIR / "profiles").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files and all(any(fnmatch(name, pattern) for pattern in patterns) for name in files), files


# ---------------------------------------------------------------------------- the Gemini profile's schema form


def descriptions(node):
    if isinstance(node, dict):
        return (1 if "description" in node else 0) + sum(descriptions(v) for k, v in node.items() if k != "description")
    if isinstance(node, list):
        return sum(descriptions(v) for v in node)
    return 0


def test_the_gemini_profiles_schema_has_the_structure_of_the_generated_openapi_form():
    rules = rules_of("gemini")
    built = gemini_schema(rules.schema, "openapi")
    assert without_descriptions(rules.openapi_schema) == without_descriptions(built)
    assert described_openapi_problems(rules.openapi_schema, rules.schema) == []
    assert descriptions(rules.openapi_schema) == 26
    assert rules.schema == base_rules().schema  # the answer's form is the same in every profile
    assert rules_of("openai").openapi_schema is None and rules_of("deepseek").openapi_schema is None
    parsed = types.Schema.model_validate(rules.openapi_schema)  # the SDK's own type reads it, descriptions too
    assert parsed.properties["rows"].items.properties["depth"].enum == ["simple", "medium", "deep"]
    assert parsed.properties["skip"].description.startswith("Set true ONLY to decline the item")


def test_a_row_schema_change_makes_the_gemini_profiles_schema_fail_loudly():
    rules = rules_of("gemini")
    changed = copy.deepcopy(dict(rules.schema))
    row = changed["properties"]["rows"]["items"]
    row["properties"]["mood"] = {"type": "string"}
    row["required"] = [*row["required"], "mood"]
    problems = described_openapi_problems(rules.openapi_schema, changed)
    assert any("'mood' of the row schema is missing" in problem for problem in problems)
    writer = GeminiBatchWriter("flash-batch-p", RouteConfig(provider="gemini", model=GEMINI_MODEL, kind="batch", options={"schema": "openapi"}))
    with pytest.raises(ConfigError, match="prompt profile 'gemini' differs from the row schema"):
        writer.check_rules(dataclasses.replace(rules, schema=changed))
    writer.check_rules(rules)


# ---------------------------------------------------------------------------- the adapters send the route's profile


def gemini_route(name="flash-batch-p", kind="batch", **fields):
    data = {"provider": "gemini", "model": GEMINI_MODEL, "kind": kind, "effort": "low", "profile": "gemini",
            "options": {"schema": "openapi", "schema_field": "response_schema"}}
    data.update(fields)
    return RouteConfig.from_dict(name, data)


def profile_call(rules, line="001", **fields):
    item = entry("drawn_series")["item"]
    return adapter_call(line, provider=fields.pop("provider", "gemini"), model=fields.pop("model", GEMINI_MODEL), rules=rules,
                        spec=item, task_text=render_task_text(item, ordinal=0, rules=rules), **fields)


@pytest.mark.parametrize("writer_class, kind", [(GeminiBatchWriter, "batch"), (GeminiSyncWriter, "api")])
def test_a_gemini_route_on_its_profile_sends_the_profiles_prompt_and_described_schema(writer_class, kind):
    rules = rules_of("gemini")
    writer = writer_class("flash-p", gemini_route(kind=kind))
    writer.check_rules(rules)
    [request] = writer.prepare([profile_call(rules)])
    config = request.payload["request"].config
    assert config.system_instruction == rules.system and request.payload["rules_version"] == rules.version
    assert config.response_schema == rules.openapi_schema and config.response_json_schema is None
    assert [part.text for part in request.payload["request"].contents[0].parts] == [profile_call(rules).task_text]
    expected = estimate_text_tokens(rules.system, profile_call(rules).task_text, canonical_json(rules.openapi_schema))
    assert request.est_input_tokens == expected
    [default] = writer.prepare([profile_call(base_rules())])
    assert default.payload["request"].config.response_schema == gemini_schema(base_rules().schema, "openapi")
    assert request.est_input_tokens > default.est_input_tokens  # the longer prompt is what the estimate counts
    for forbidden in ("temperature", "top_p", "top_k"):
        assert getattr(config, forbidden) is None


def test_a_gemini_route_variant_sets_its_thinking_level_to_medium():
    config = Config.from_dict(
        {
            "routes": {
                "flash-batch-p": {"provider": "gemini", "model": GEMINI_MODEL, "kind": "batch", "effort": "low", "profile": "gemini"},
                "flash-batch-pm": {"provider": "gemini", "model": GEMINI_MODEL, "kind": "batch", "effort": "medium", "profile": "gemini"},
            }
        }
    )
    levels = {}
    for name, route in config.routes.items():
        writer = make_writer(name, route)
        [request] = writer.prepare([profile_call(rules_of(route.profile), effort=route.effort)])
        levels[name] = request.payload["request"].config.thinking_config.thinking_level
    assert levels == {"flash-batch-p": types.ThinkingLevel.LOW, "flash-batch-pm": types.ThinkingLevel.MEDIUM}


def test_the_gemini_explicit_cache_is_one_per_profile():
    created = Recorder(Namespace(name="cachedContents/c1", expire_time=None))
    client = Namespace(caches=Namespace(create=created, update=Recorder(None)), batches=Namespace(create=Recorder(Namespace(name="batches/b1"))))
    writer = GeminiBatchWriter("flash-batch-p", gemini_route(options={"cache": True}), client=client)
    for rules in (base_rules(), rules_of("gemini"), rules_of("gemini")):
        writer.submit(writer.prepare([profile_call(rules)]))
    made = [(entry_["config"].display_name, entry_["config"].system_instruction) for entry_ in created.calls]
    assert made == [(f"rps-{TODAYS_VERSION}", base_rules().system), (f"rps-{rules_of('gemini').version}", rules_of("gemini").system)]


def test_an_openai_route_on_its_profile_sends_the_profiles_prompt_and_caches_on_its_version():
    rules = rules_of("openai")
    writer = OpenAISyncWriter("luna-sync-p", RouteConfig.from_dict("luna-sync-p", {"provider": "openai", "model": "gpt-6-luna", "kind": "api", "profile": "openai"}))
    writer.check_rules(rules)
    [request] = writer.prepare([profile_call(rules, provider="openai", model="gpt-6-luna")])
    body = request.payload["body"]
    assert body["instructions"] == rules.system and body["prompt_cache_key"] == rules.version != TODAYS_VERSION
    assert body["input"][0]["content"][0]["text"] == profile_call(rules).task_text
    assert body["text"]["format"]["schema"] == dict(base_rules().schema)  # the strict schema is the shared one
    assert "temperature" not in body and "top_p" not in body


def test_a_deepseek_route_on_its_profile_sends_the_profiles_prompt_and_format_block():
    rules = rules_of("deepseek")
    block = (RULES_DIR / "profiles" / "deepseek" / "json_format.md").read_text(encoding="utf-8").strip()
    assert rules.json_format == block != base_rules().json_format
    writer = DeepSeekWriter("deepseek-sync-p", RouteConfig.from_dict("deepseek-sync-p", {"provider": "deepseek", "model": "deepseek-flash", "kind": "api", "profile": "deepseek", "enabled": True}))
    writer.check_rules(rules)
    [request] = writer.prepare([profile_call(rules, provider="deepseek", model="deepseek-flash", effort=None)])
    system, user = request.payload["arguments"]["messages"]
    assert system == {"role": "system", "content": rules.system.rstrip() + "\n\n" + block} and system["content"] == system_text(rules)
    assert user["content"] == profile_call(rules).task_text
    assert set(request.payload["arguments"]) == {"model", "messages", "response_format", "max_tokens"}  # no temperature, no penalties
    assert rules_of("openai").json_format == rules_of("gemini").json_format == base_rules().json_format  # the shared block otherwise


# ---------------------------------------------------------------------------- routes name their profile


def test_a_route_names_its_profile_and_leaves_it_at_the_default_otherwise():
    route = RouteConfig.from_dict("luna", {"provider": "openai", "model": "gpt-6-luna"})
    assert route.profile == "default" and route.to_dict()["profile"] == "default"
    named = RouteConfig.from_dict("luna-p", {"provider": "openai", "model": "gpt-6-luna", "profile": "openai"})
    assert named.profile == "openai" and RouteConfig.from_dict("luna-p", json.loads(json.dumps(named.to_dict()))) == named
    for bad in ("", "open ai", "x" * 65, 3, None):
        with pytest.raises(ConfigError, match="profile"):
            RouteConfig.from_dict("luna-p", {"provider": "openai", "model": "gpt-6-luna", "profile": bad})


def pool_routes(**profiles):
    route = {"provider": "fake", "model": "fake-model", "kind": "batch", "effort": "low", "budget_usd": 10.0, "purpose_caps": dict(ROUTE_CAPS)}
    return {name: dict(route, profile=profile) for name, profile in profiles.items()}


def test_a_route_that_names_a_profile_the_rules_do_not_have_is_refused_at_setup(tmp_path):
    config = make_config(rules={"package": "rps.rules"}, routes=pool_routes(**{"fake-batch": "default", "fake-batch-x": "mystery"}))
    with pytest.raises(ConfigError, match="route 'fake-batch-x': unknown prompt profile 'mystery'; .* has default, deepseek, gemini, openai"):
        World(tmp_path, config=config).controller()
    no_rules = make_config(routes=pool_routes(**{"fake-batch": "gemini"}))
    with pytest.raises(ConfigError, match="names the prompt profile 'gemini', but no writing rules are set"):
        World(tmp_path / "b", config=no_rules).controller()
    plain = make_rules("Write json rows.", {"type": "object", "properties": {}})
    with pytest.raises(ConfigError, match="have no prompt profiles"):
        World(tmp_path / "c", config=make_config(routes=pool_routes(**{"fake-batch": "openai"}))).controller(rules=plain)


class Measured(FakeWriter):
    """A fake writer that keeps each call and estimates its prompt as the real writers do, from the call's own rules."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[CallSpec] = []

    def prepare(self, calls):
        self.calls.extend(calls)
        requests = super().prepare(calls)
        return [dataclasses.replace(request, est_input_tokens=estimate_text_tokens(call.rules.system, call.task_text)) for call, request in zip(calls, requests)]


def answers(ordinals):
    """The worked example's answer for the item of a call, with its questions asked as the call's choices assign them."""

    def behavior(payload):
        example = next(e for e in pool.examples() if e["item"]["ids"] == payload["spec"]["ids"])
        plan = balance.assign(payload["spec"], ordinals[payload["line"]])
        out = copy.deepcopy(example["output"])
        for row in out["rows"]:
            if plan.relation_part is None:
                row["questions"] = [q for q in row["questions"] if not re.fullmatch(r"rel\d+ (a|rel|b)", q["key"])]
            for question in row["questions"]:
                if plan.relation_part and re.fullmatch(r"rel1 (a|rel|b)", question["key"]):
                    question["key"] = f"rel1 {plan.relation_part}"
                    if question["needs"] == "record":
                        question["value"] = row["relations"][0][plan.relation_part]
                if question["needs"] == "unknown" and row["depth"] in plan.unknown_keys:
                    question["key"] = plan.unknown_keys[row["depth"]]
        return FakeOutcome(output=json.dumps(out))

    return behavior


def pool_world(tmp_path, **world):
    config = make_config(rules={"package": "rps.rules"}, routes=pool_routes(**{"fake-batch": "default", "fake-batch-p": "gemini"}))
    items = [e["item"] for e in pool.examples()]
    lines = tuple(SpecLine(item["ids"][0], item, item_id=item["ids"][0], license=item["license"]) for item in items)
    ordinals = {item["ids"][0]: ordinal for ordinal, item in enumerate(items)}
    return World(tmp_path, config=config, behavior=answers(ordinals), writer_class=Measured, **world), lines, items


def test_each_route_writes_with_its_profiles_rules_and_every_record_says_which(tmp_path):
    world, lines, items = pool_world(tmp_path)
    controller = world.controller()
    gemini = rules_of("gemini")
    assert controller.rules_for("fake-batch") is controller.rules and controller.rules_for("fake-batch-p").profile == "gemini"
    assert set(controller.profiles) == {"default", "gemini"}
    for route in ("fake-batch", "fake-batch-p"):
        controller.add_task(TaskSpec(source="photos", batch="pool", route=route, purpose="production", variant=route, lines=lines))
    world.drive(controller, keep_all)
    # every call of a route is built from its own rules: the system prompt, the task text, the version
    for route, rules in (("fake-batch", base_rules()), ("fake-batch-p", gemini)):
        made = [call for call in controller.writers[route].calls]
        assert made and all(call.rules.system == rules.system and call.rules.version == rules.version for call in made)
        for call in made:
            ordinal = next(i for i, item in enumerate(items) if item["ids"][0] == call.line)
            assert call.task_text == render_task_text(call.spec, call.feedback, ordinal=ordinal, rules=rules)
    profile = {"fake-batch": ("default", TODAYS_VERSION), "fake-batch-p": ("gemini", gemini.version)}
    for event in world.events_of("call_prepared"):
        assert (event.payload["rules_profile"], event.payload["rules_version"]) == profile[event.payload["route"]]
    shipped = world.shipped_rows()
    assert len(shipped) == 2 * sum(len(e["output"]["rows"]) for e in pool.examples())
    by_task = {task.task_id: task.route for task in controller.state.tasks.values()}
    for row in shipped:
        label = row["writer"]
        assert (label["rules_profile"], label["rules_version"]) == profile[by_task[label["task_id"]]]
    # the packet records name the rules of each row; the packets the reviewers read do not
    items_published = [item for event in world.events_of("packet_published") for item in event.payload["items"]]
    assert items_published and {(i["rules_profile"], i["rules_version"]) for i in items_published} == set(profile.values())
    assert {event.payload["brief_version"] for event in world.events_of("packet_published")} == {TODAYS_VERSION}
    for path in [p for p in world.state_store.list("packets/") if p.endswith("packet.json")]:
        text = world.state_store.read_text(path)
        assert "rules_profile" not in text and gemini.version not in text and '"gemini"' not in text


def test_the_long_prompt_refusal_reads_the_prompt_of_the_routes_own_profile(tmp_path):
    item = pool.examples()[0]["item"]
    short = estimate_text_tokens(base_rules().system, render_task_text(item, ordinal=0, rules=base_rules()))
    long = estimate_text_tokens(rules_of("gemini").system, render_task_text(item, ordinal=0, rules=rules_of("gemini")))
    assert long > short
    limit = (short + long) // 2
    prices = PriceTable.from_dict({"date": "2026-11-07", "models": {"fake-model": {"sync": {"input": 1.0, "output": 2.0}, "batch": {"input": 1.0, "output": 2.0}, "long_prompt_tokens": limit}}})
    world, lines, _ = pool_world(tmp_path, prices=prices)
    controller = world.controller()
    for route in ("fake-batch", "fake-batch-p"):
        controller.add_task(TaskSpec(source="photos", batch="pool", route=route, purpose="production", variant=route, lines=lines[:1]))
    controller.step()
    refused = world.events_of("call_failed")
    assert [event.payload["kind"] for event in refused] == ["long_prompt"]
    assert controller.state.calls[refused[0].payload["call_id"]].route == "fake-batch-p"
    assert f"over the {limit:,}-token long-prompt threshold" in refused[0].payload["error"]
    assert [c for c in world.provider.submitted_calls if controller.state.calls[c].route == "fake-batch"]


def test_the_gemini_probe_sends_the_prompt_of_the_profile_its_route_names(monkeypatch):
    from rps.geminiprobe import probe_gemini

    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")  # the probe's fake client stands in for the service
    seen = {}

    def generate(model, contents, config):
        seen["config"] = config
        return types.GenerateContentResponse.model_validate(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "{}"}]}, "finish_reason": "STOP"}], "usage_metadata": {"prompt_token_count": 1}}
        )

    client = Namespace(models=Namespace(generate_content=generate))
    report = probe_gemini("flash-batch-p", modes=("openapi",), route_config=gemini_route(), client=client, cap_usd=1.0, printer=lambda text: None)
    assert not report.refused, report.refused
    assert seen["config"].system_instruction == rules_of("gemini").system
    unknown = probe_gemini("flash-batch-x", modes=("openapi",), route_config=gemini_route(profile="mystery"), client=client, printer=lambda text: None)
    assert unknown.refused and "unknown prompt profile 'mystery'" in unknown.refused


# ---------------------------------------------------------------------------- the mood's source


def _writer_texts():
    texts = {"default": pool.writer_system()}
    texts.update({name: (RULES_DIR / "profiles" / name / "writer_system.md").read_text(encoding="utf-8") for name in PROFILES})
    return texts


@pytest.mark.parametrize("name", ["default", *PROFILES])
def test_each_writer_text_names_the_records_that_carry_a_mood_and_no_relation(name):
    text = _writer_texts()[name]
    flat = text.replace("`", "")
    assert "Never a relation, a position, a count, an angle, a framing or a name" in flat or "Never a relation, a position, a count, an angle, a framing or a name" in text
    assert "scene weather" in flat and "form palette" in flat and "body gesture" in flat
    assert "first add one fact its hint states plainly" in flat
    assert "no second feeling word" in flat
    for old in ("or a relation as often as", "relation word alone", "or relations as often as", "for a relation, \"rel1 rel\"", "or rel1 rel when", "only as mood from's value", "exactly two places"):
        assert old not in flat, old
    assert "shot light; for a relation" not in flat
    assert "never the value of mood from" in flat.replace('"', "")
