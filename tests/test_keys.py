"""Keys come from environment variables and are never printed."""

import pytest

from rps.keys import SECRET_ENV_MAP, looks_like_secret, map_colab_secrets, redact


class SecretNotFound(Exception):
    """Like the error Colab raises for a secret that does not exist."""


class NotGranted(Exception):
    """Like the error Colab raises when the notebook has no access to a secret."""


class FakeUserdata:
    """Stands in for google.colab.userdata."""

    def __init__(self, values=None, not_granted=()):
        self.values = dict(values or {})
        self.not_granted = set(not_granted)
        self.asked = []

    def get(self, name):
        self.asked.append(name)
        if name in self.not_granted:
            raise NotGranted(f"the notebook cannot read {name}")
        if name not in self.values:
            raise SecretNotFound(name)
        return self.values[name]


def key_like(prefix, length=40):
    return prefix + "q" * length


def test_the_mapping_names_every_colab_secret_and_its_variable():
    assert dict(SECRET_ENV_MAP) == {
        "CLAUDE_API": "ANTHROPIC_API_KEY",
        "OPENAI_API": "OPENAI_API_KEY",
        "GEMINI_API": "GEMINI_API_KEY",
        "DEEPSEEK_API": "DEEPSEEK_API_KEY",
        "HF_TOKEN": "HF_TOKEN",
    }


def test_secrets_reach_the_environment_and_only_their_names_are_printed():
    values = {
        "CLAUDE_API": key_like("sk-ant-"),
        "OPENAI_API": key_like("sk-"),
        "GEMINI_API": key_like("AIza", 35),
        "HF_TOKEN": key_like("hf_", 34),
    }
    environ, printed = {}, []
    ready = map_colab_secrets(FakeUserdata(values), environ, printed.append)
    assert environ == {
        "ANTHROPIC_API_KEY": values["CLAUDE_API"],
        "OPENAI_API_KEY": values["OPENAI_API"],
        "GEMINI_API_KEY": values["GEMINI_API"],
        "HF_TOKEN": values["HF_TOKEN"],
    }
    assert ready == {"CLAUDE_API": True, "OPENAI_API": True, "GEMINI_API": True, "DEEPSEEK_API": False, "HF_TOKEN": True}
    assert printed == [
        "CLAUDE_API: ready",
        "OPENAI_API: ready",
        "GEMINI_API: ready",
        "DEEPSEEK_API: not available",
        "HF_TOKEN: ready",
    ]
    everything = "\n".join(printed)
    assert not any(value in everything for value in values.values()) and "qqqq" not in everything


def test_a_missing_or_not_granted_secret_prints_its_name_and_not_available():
    environ, printed = {}, []
    userdata = FakeUserdata({"CLAUDE_API": key_like("sk-ant-")}, not_granted={"OPENAI_API"})
    ready = map_colab_secrets(userdata, environ, printed.append)
    assert printed[1] == "OPENAI_API: not available"  # not shared with this notebook
    assert printed[2] == "GEMINI_API: not available"  # does not exist
    assert "OPENAI_API_KEY" not in environ and "GEMINI_API_KEY" not in environ
    assert ready["CLAUDE_API"] is True and ready["OPENAI_API"] is False
    assert userdata.asked == [name for name, _ in SECRET_ENV_MAP]  # every secret was asked for


def test_an_empty_secret_counts_as_not_available():
    environ, printed = {}, []
    map_colab_secrets(FakeUserdata({"CLAUDE_API": "   "}), environ, printed.append)
    assert printed[0] == "CLAUDE_API: not available" and "ANTHROPIC_API_KEY" not in environ


def test_a_key_already_in_the_environment_is_kept_when_there_is_no_secret():
    environ, printed = {"ANTHROPIC_API_KEY": key_like("sk-ant-")}, []
    ready = map_colab_secrets(FakeUserdata(), environ, printed.append)
    assert printed[0] == "CLAUDE_API: ready (already in the environment)"
    assert ready["CLAUDE_API"] is True and environ["ANTHROPIC_API_KEY"].startswith("sk-ant-")
    assert "qqqq" not in "\n".join(printed)


def test_outside_colab_nothing_breaks_and_nothing_is_set():
    environ, printed = {}, []
    ready = map_colab_secrets(None, environ, printed.append)  # there is no google.colab in the tests
    assert not any(ready.values()) and environ == {}
    assert printed == [f"{name}: not available" for name, _ in SECRET_ENV_MAP]


@pytest.mark.parametrize(
    "text",
    [
        key_like("sk-ant-api03-", 30),
        key_like("sk-proj-", 30),
        key_like("hf_", 30),
        key_like("AIza", 35),
        key_like("ghp_", 36),
        "Authorization: Bearer " + "z" * 30,
    ],
)
def test_text_shaped_like_a_key_is_recognized_and_redacted(text):
    assert looks_like_secret(text)
    cleaned = redact(f"the call failed: {text} (retry later)")
    assert "[redacted]" in cleaned and "qqqq" not in cleaned and "zzzz" not in cleaned
    assert cleaned.startswith("the call failed: ") and cleaned.endswith("(retry later)")


def test_ordinary_text_is_left_alone():
    for text in ("overloaded, try again", "skill-ful rows", "hf_hub is a library", "a task-id like t0123456789abcdef"):
        assert not looks_like_secret(text) and redact(text) == text
