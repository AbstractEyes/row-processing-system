"""Shared test setup: no network, no keys, and a clean environment."""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Tests run without keys: HF_TOKEN is empty and provider keys are removed."""
    monkeypatch.setenv("HF_TOKEN", "")
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("DO_NOT_TRACK", "1")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a network connection fails the test."""

    def refuse(*args, **kwargs):
        raise RuntimeError("network access is not allowed in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def local_store(tmp_path):
    from rps.store.local import LocalStore

    return LocalStore(tmp_path / "store", fsync=False)
