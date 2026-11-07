"""Which pictures may go to which provider.

One table decides what an adapter may see, and adapters receive picture bytes only
through it. The class of a picture is the class of the picture's own origin: a
narration written for a COCO picture is COCO class, and one written for an Open
Images picture is public-common class.

    class           Claude   OpenAI   Gemini   DeepSeek
    public_common     no       yes      yes      yes (one request at a time)
    coco              no       yes      yes      yes
    ffhq              no       yes      yes      yes
    deepfashion       no       no       no       no
    hico              no       no       no       no
    synthetic         no       no       no       no     (specs with no picture; text only)

Text is always allowed. A class that is not in the table is refused, and so is a
route that is not recognized: the answer for anything unknown is no.
"""

from __future__ import annotations

from typing import Mapping

PUBLIC_COMMON = "public_common"
COCO = "coco"
FFHQ = "ffhq"
DEEPFASHION = "deepfashion"
HICO = "hico"
SYNTHETIC = "synthetic"

SOURCE_CLASSES: tuple[str, ...] = (PUBLIC_COMMON, COCO, FFHQ, DEEPFASHION, HICO, SYNTHETIC)

CLAUDE = "claude"
OPENAI = "openai"
GEMINI = "gemini"
DEEPSEEK = "deepseek"

ROUTE_COLUMNS: tuple[str, ...] = (CLAUDE, OPENAI, GEMINI, DEEPSEEK)

# True where a picture of that class may be sent to that route's provider.
IMAGE_POLICY: Mapping[str, Mapping[str, bool]] = {
    PUBLIC_COMMON: {CLAUDE: False, OPENAI: True, GEMINI: True, DEEPSEEK: True},
    COCO: {CLAUDE: False, OPENAI: True, GEMINI: True, DEEPSEEK: True},
    FFHQ: {CLAUDE: False, OPENAI: True, GEMINI: True, DEEPSEEK: True},
    DEEPFASHION: {CLAUDE: False, OPENAI: False, GEMINI: False, DEEPSEEK: False},
    HICO: {CLAUDE: False, OPENAI: False, GEMINI: False, DEEPSEEK: False},
    SYNTHETIC: {CLAUDE: False, OPENAI: False, GEMINI: False, DEEPSEEK: False},
}

# Names that stand for a column: provider names, the short route names, and the
# test double (which is treated like an image-capable route that sees public data only).
_COLUMN_OF: Mapping[str, str] = {
    "anthropic": CLAUDE,
    "claude": CLAUDE,
    "haiku": CLAUDE,
    "sonnet": CLAUDE,
    "openai": OPENAI,
    "luna": OPENAI,
    "sol": OPENAI,
    "gemini": GEMINI,
    "google": GEMINI,
    "flash": GEMINI,
    "argon": GEMINI,
    "deepseek": DEEPSEEK,
    "fake": OPENAI,
}


class ImageRoutingError(ValueError):
    """A picture may not go to the route it was meant for."""


def route_column(route: str) -> str | None:
    """The column of the policy table a route belongs to, or None if it is not recognized.

    `route` may be a provider name ("anthropic"), a short route name ("haiku") or a
    full route name that starts with one ("luna-batch", "deepseek-sync").
    """
    if not isinstance(route, str):
        return None
    name = route.strip().lower()
    if name in _COLUMN_OF:
        return _COLUMN_OF[name]
    for separator in ("-", "_", "."):
        head = name.split(separator, 1)[0]
        if head in _COLUMN_OF:
            return _COLUMN_OF[head]
    return None


def allowed_image(source_class: str, route: str) -> bool:
    """True when a picture of this source class may be sent to this route.

    A class missing from the table and a route that is not recognized are both refused.
    """
    row = IMAGE_POLICY.get(source_class) if isinstance(source_class, str) else None
    column = route_column(route)
    if row is None or column is None:
        return False
    return row[column]


def check_image_allowed(source_class: str | None, route: str) -> None:
    """Raise ImageRoutingError, saying why, unless `allowed_image` is true."""
    if not source_class:
        raise ImageRoutingError(
            f"a picture needs a source class ({', '.join(SOURCE_CLASSES)}); none is set for the source or the spec line"
        )
    if source_class not in IMAGE_POLICY:
        raise ImageRoutingError(f"source class {source_class!r} is not in the routing table ({', '.join(SOURCE_CLASSES)})")
    column = route_column(route)
    if column is None:
        raise ImageRoutingError(f"route {route!r} is not in the routing table, so no picture may go to it")
    if not IMAGE_POLICY[source_class][column]:
        if source_class == SYNTHETIC:
            raise ImageRoutingError("synthetic specs have no picture to send")
        raise ImageRoutingError(f"pictures of class {source_class!r} may not be sent to {column} routes")
