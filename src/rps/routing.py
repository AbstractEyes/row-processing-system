"""Which pictures may go to which provider.

One table decides what an adapter may see, and adapters receive picture bytes only
through it. The class of a picture is the class of the picture's own origin: a
narration written for a COCO picture is COCO class, and one written for an Open
Images picture is public-common class.

    class           Claude   OpenAI   Gemini   DeepSeek
    public_common     yes      yes      yes      yes (one request at a time)
    coco              yes      yes      yes      yes
    ffhq              yes      yes      yes      yes
    deepfashion       no       no       no       no
    hico              no       no       no       no
    synthetic         no       no       no       no     (specs with no picture; text only)

Text is always allowed. A class that is not in the table is refused, and so is a
route that is not recognized: the answer for anything unknown is no.

Claude writers and Claude reviewers get the publicly viewable pictures of public datasets
(public_common, coco and ffhq) like the other families do; deepfashion, hico and synthetic
stay closed to every outside provider.

Review packets follow the same table, by the reviewer's model family: a packet carries
a row's picture only when `allowed_image(class, family)` is true, so a reviewer from a
family the table refuses (or one it does not know, such as the owner's own reviews)
reviews that row from its text.

A project may override single cells of the table in its configuration (`routing.image_policy`, see
`rps.config`); the table above is the package's default, and the owner answers for an override.
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
    PUBLIC_COMMON: {CLAUDE: True, OPENAI: True, GEMINI: True, DEEPSEEK: True},
    COCO: {CLAUDE: True, OPENAI: True, GEMINI: True, DEEPSEEK: True},
    FFHQ: {CLAUDE: True, OPENAI: True, GEMINI: True, DEEPSEEK: True},
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


def image_table(overrides: Mapping[str, Mapping[str, bool]] | None = None) -> Mapping[str, Mapping[str, bool]]:
    """The table as one project sees it: the package's table with the project's overrides laid over it.

    `overrides` is {class: {column: bool}} as `check_overrides` accepts it; without any, the answer is the
    package's own table. The package's table itself never changes.
    """
    if not overrides:
        return IMAGE_POLICY
    return {name: {**row, **overrides.get(name, {})} for name, row in IMAGE_POLICY.items()}


def check_overrides(data: object, where: str = "routing.image_policy") -> dict[str, dict[str, bool]]:
    """Check a project's overrides ({class: {column: true or false}}); raise ValueError, saying what is wrong.

    A class must be one of SOURCE_CLASSES and a column one of ROUTE_COLUMNS; a value must be true or false.
    The class `synthetic` has no picture, so it can never be opened.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"{where}: expected an object of source classes")
    out: dict[str, dict[str, bool]] = {}
    for name, columns in data.items():
        if name not in SOURCE_CLASSES:
            raise ValueError(f"{where}.{name}: not a source class; use one of {list(SOURCE_CLASSES)}")
        if not isinstance(columns, Mapping):
            raise ValueError(f"{where}.{name}: expected an object of provider columns")
        row: dict[str, bool] = {}
        for column, value in columns.items():
            if column not in ROUTE_COLUMNS:
                raise ValueError(f"{where}.{name}.{column}: not a provider column; use one of {list(ROUTE_COLUMNS)}")
            if not isinstance(value, bool):
                raise ValueError(f"{where}.{name}.{column}: expected true or false, got {value!r}")
            if name == SYNTHETIC and value:
                raise ValueError(f"{where}.{name}.{column}: synthetic specs have no picture, so the class can never be opened")
            row[column] = value
        out[name] = row
    return out


def allowed_image(source_class: str, route: str, policy: Mapping[str, Mapping[str, bool]] | None = None) -> bool:
    """True when a picture of this source class may be sent to this route.

    A class missing from the table and a route that is not recognized are both refused. `policy` is the
    table to ask (a project's view, see `image_table`); the package's table when left out.
    """
    table = IMAGE_POLICY if policy is None else policy
    row = table.get(source_class) if isinstance(source_class, str) else None
    column = route_column(route)
    if row is None or column is None:
        return False
    return row[column]


def check_image_allowed(source_class: str | None, route: str, policy: Mapping[str, Mapping[str, bool]] | None = None) -> None:
    """Raise ImageRoutingError, saying why, unless `allowed_image` is true."""
    table = IMAGE_POLICY if policy is None else policy
    if not source_class:
        raise ImageRoutingError(
            f"a picture needs a source class ({', '.join(SOURCE_CLASSES)}); none is set for the source or the spec line"
        )
    if source_class not in table:
        raise ImageRoutingError(f"source class {source_class!r} is not in the routing table ({', '.join(SOURCE_CLASSES)})")
    column = route_column(route)
    if column is None:
        raise ImageRoutingError(f"route {route!r} is not in the routing table, so no picture may go to it")
    if not table[source_class][column]:
        if source_class == SYNTHETIC:
            raise ImageRoutingError("synthetic specs have no picture to send")
        raise ImageRoutingError(f"pictures of class {source_class!r} may not be sent to {column} routes")
