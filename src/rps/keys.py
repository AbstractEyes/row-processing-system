"""API keys: read from environment variables only, never printed, logged or written to a file."""

from __future__ import annotations

import os
import re
from typing import Any, Callable, MutableMapping, Sequence

# (Colab secret name, environment variable name)
SECRET_ENV_MAP: tuple[tuple[str, str], ...] = (
    ("CLAUDE_API", "ANTHROPIC_API_KEY"),
    ("OPENAI_API", "OPENAI_API_KEY"),
    ("GEMINI_API", "GEMINI_API_KEY"),
    ("DEEPSEEK_API", "DEEPSEEK_API_KEY"),
    ("HF_TOKEN", "HF_TOKEN"),
)

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"),
]


def looks_like_secret(text: str) -> bool:
    """True if the text contains something shaped like an API key or token."""
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def redact(text: str) -> str:
    """Replace anything shaped like an API key or token with "[redacted]"."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def map_colab_secrets(
    userdata: Any = None,
    environ: MutableMapping[str, str] | None = None,
    printer: Callable[[str], Any] = print,
    mapping: Sequence[tuple[str, str]] = SECRET_ENV_MAP,
) -> dict[str, bool]:
    """Copy Colab secrets into environment variables without ever printing a value.

    For each secret only its name is printed, with "ready" or "not available".
    A secret that is missing or not shared with the notebook is simply skipped.
    `userdata` is `google.colab.userdata` (looked up when omitted); `environ`
    defaults to `os.environ`. Returns {secret name: ready}.
    """
    if userdata is None:
        try:
            from google.colab import userdata  # type: ignore[no-redef]
        except Exception:
            userdata = None
    env = os.environ if environ is None else environ
    ready: dict[str, bool] = {}
    for secret_name, env_name in mapping:
        value = None
        if userdata is not None:
            try:
                value = userdata.get(secret_name)
            except Exception:
                value = None
        if value is not None and str(value).strip():
            env[env_name] = str(value)
            ready[secret_name] = True
            printer(f"{secret_name}: ready")
        elif env.get(env_name):
            ready[secret_name] = True
            printer(f"{secret_name}: ready (already in the environment)")
        else:
            ready[secret_name] = False
            printer(f"{secret_name}: not available")
    return ready
