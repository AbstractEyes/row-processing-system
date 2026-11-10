"""Fetching the pictures a run needs, from links the owner lists in the state store.

Seeds hold page links, and a runtime such as Colab holds no picture files. A manifest says where each
picture can be fetched from: any number of `*.jsonl` files under `control/pictures/` in the state store,
one JSON object a line,

    {"image": "coco/000000223816.jpg", "urls": ["https://...", "https://..."]}

`image` is the path under the source's `images_dir` exactly as a task line's `image` gives it; `urls` are
tried in order. A link is an http or https address, or `store:<path>`, which reads the picture's bytes from
the state store at that path (for a picture the owner has staged privately there). A later line for the same
image wins.

    controller = Controller(config, ..., image_loader=FetchingImageLoader(config, state_store))

The loader is called as the controller's `image_loader` is: `(source, line) -> (bytes, extension, longest side)`.
A picture already on the disk is loaded as the default loader loads it; a missing one is fetched once, kept
on the disk, and loaded. Every failure ends in `ImageError`, which the controller turns into a failed call.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from . import layout
from .images import ImageError, load_picture
from .store.base import NotFound, Store, StoreError

MANIFEST_PREFIX = f"{layout.CONTROL_DIR}/pictures/"
STORE_SCHEME = "store:"
USER_AGENT = "row-processing-system"
MANIFEST_REREAD_SECONDS = 60.0

# An opener takes a link and returns (status, body); a network failure or a timeout is raised as OSError.
Opener = Callable[..., "tuple[int, bytes]"]


def urllib_opener(url: str, *, timeout: float, max_bytes: int) -> tuple[int, bytes]:
    """Fetch a link with the standard library: (status, at most max_bytes + 1 bytes of the body)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (the scheme is checked before)
            return int(response.status), response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        return int(error.code), b""
    except urllib.error.URLError as error:
        raise OSError(str(error.reason)) from None


class FetchingImageLoader:
    """An `image_loader` that fetches a missing picture from the manifest's links. See the module notes."""

    def __init__(
        self,
        config: Any,
        state_store: Store,
        *,
        opener: Opener | None = None,
        timeout: float = 30.0,
        tries: int = 3,
        pause: float = 0.2,
        max_bytes: int = 30_000_000,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.config = config
        self.store = state_store
        self.opener: Opener = opener or urllib_opener
        self.timeout = timeout
        self.tries = max(1, int(tries))
        self.pause = pause
        self.max_bytes = max_bytes
        self._clock = clock
        self._sleep = sleep
        self._manifest: dict[str, list[str]] = {}
        self._read_at: float | None = None

    # ------------------------------------------------------------ the loader

    def __call__(self, source: Any, line: Any) -> tuple[bytes, str, int]:
        folder = Path(source.images_dir) if source.images_dir else self.config.work_path("images")
        target = folder / line.image
        if target.is_file():
            return load_picture(target, source.image_size)
        self._fetch(line.image, target)
        try:
            return load_picture(target, source.image_size)
        except ImageError as error:
            target.unlink(missing_ok=True)  # what a link gave was not a picture: do not keep it
            raise ImageError(f"picture {line.image!r} was fetched but is not a picture that can be read ({error})") from None

    # ------------------------------------------------------------ the manifest

    def _read_manifest(self) -> None:
        found: dict[str, list[str]] = {}
        try:
            for path in sorted(self.store.list(MANIFEST_PREFIX)):
                if not path.endswith(".jsonl"):
                    continue
                for raw in self.store.read_text(path).splitlines():
                    if not raw.strip():
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue  # a torn or foreign line is not a reason to lose the others
                    if isinstance(entry, Mapping) and isinstance(entry.get("image"), str) and isinstance(entry.get("urls"), list):
                        found[entry["image"]] = [u for u in entry["urls"] if isinstance(u, str)]
        except StoreError as error:
            raise ImageError(f"the picture manifest under {MANIFEST_PREFIX} cannot be read: {error}") from None
        self._manifest = found
        self._read_at = self._clock()

    def _links(self, image: str) -> list[str] | None:
        if self._read_at is None:
            self._read_manifest()
        if image not in self._manifest and self._clock() - (self._read_at or 0.0) >= MANIFEST_REREAD_SECONDS:
            self._read_manifest()
        return self._manifest.get(image)

    # ------------------------------------------------------------ fetching

    def _fetch(self, image: str, target: Path) -> None:
        links = self._links(image)
        if not links:
            raise ImageError(f"picture {image!r} is not on the disk and the manifest under {MANIFEST_PREFIX} has no link for it")
        failures: list[str] = []
        for url in links:
            data, why = self._get(url)
            if data is None:
                failures.append(f"{url}: {why}")
                continue
            self._keep(image, target, data)
            self._sleep(self.pause)
            return
        raise ImageError(f"picture {image!r} could not be fetched; every link failed ({'; '.join(failures)})")

    def _get(self, url: str) -> tuple[bytes | None, str]:
        """The bytes behind one link, or (None, why it failed)."""
        if url.startswith(STORE_SCHEME):
            try:
                data = self.store.read_bytes(url[len(STORE_SCHEME):])
            except NotFound:
                return None, "no such path in the state store"
            except (StoreError, ValueError) as error:
                return None, f"the state store could not read it ({error})"
            return (None, f"larger than {self.max_bytes} bytes") if len(data) > self.max_bytes else (data, "")
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            return None, "not an http or https link"
        why = "no answer"
        for attempt in range(self.tries):
            try:
                status, body = self.opener(url, timeout=self.timeout, max_bytes=self.max_bytes)
            except OSError as error:  # a timeout and a refused connection are both worth another try
                why = f"{type(error).__name__}: {error}"
            else:
                if status == 200:
                    return (None, f"larger than {self.max_bytes} bytes") if len(body) > self.max_bytes else (body, "")
                if status < 500:
                    return None, f"HTTP {status}"  # 404, 403 and the like: the next link
                why = f"HTTP {status}"
            if attempt + 1 < self.tries:
                self._sleep(self.pause * (2**attempt))
        return None, f"{why} after {self.tries} tries"

    @staticmethod
    def _keep(image: str, target: Path, data: bytes) -> None:
        """Write the bytes beside the target and rename them into place, so a half-written file is never read."""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")
            part.write_bytes(data)
            os.replace(part, target)
        except OSError as error:
            raise ImageError(f"picture {image!r} was fetched but cannot be written to {str(target)!r}: {error}") from None
