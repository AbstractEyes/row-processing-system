"""Adapter for the OpenAI API (not yet implemented).

The adapter's specification follows after the provider research. The key is read
from the OPENAI_API_KEY environment variable and never logged.
"""

from __future__ import annotations

from typing import Sequence

from .base import CallResult, CallSpec, Handle, PollStatus, Request, Writer

API_KEY_ENV = "OPENAI_API_KEY"
_NOTE = "the OpenAI adapter is a stub; its specification follows after the provider research"


class OpenAIWriter(Writer):
    def prepare(self, calls: Sequence[CallSpec]) -> list[Request]:
        raise NotImplementedError(_NOTE)

    def submit(self, requests: Sequence[Request]) -> Handle:
        raise NotImplementedError(_NOTE)

    def poll(self, handle: Handle) -> PollStatus:
        raise NotImplementedError(_NOTE)

    def results(self, handle: Handle) -> list[CallResult]:
        raise NotImplementedError(_NOTE)
