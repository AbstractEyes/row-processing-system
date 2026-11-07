"""The store interface, its errors, and the wrapper that enforces one writer per file."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from ..layout import Actor, may_write, normalize_path, owner_of


class StoreError(Exception):
    """Base class for store failures."""


class NotFound(StoreError, FileNotFoundError):
    """The path does not exist in the store."""


class OwnershipError(StoreError, PermissionError):
    """An actor tried to write or delete a file it does not own."""


class ConcurrentWriterError(StoreError):
    """Another writer changed files this store owns."""


class RateLimitedError(StoreError):
    """The store's own limit on commits would be exceeded."""


class StoreUnavailableError(StoreError):
    """The remote store kept failing after every retry."""


class Store(ABC):
    """A place that holds files addressed by relative "/" paths.

    Paths follow `rps.layout.normalize_path`. Text is UTF-8. `append_lines` takes
    lines without newlines and adds one after each. Deleting a missing file is not
    an error. Remote implementations buffer writes: `flush` makes them durable.
    """

    @abstractmethod
    def read_bytes(self, path: str) -> bytes:
        """Return the file's content, or raise NotFound."""

    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None:
        """Replace the file's content (creating it if needed)."""

    @abstractmethod
    def append_lines(self, path: str, lines: Iterable[str]) -> None:
        """Append lines (each followed by a newline), creating the file if needed."""

    @abstractmethod
    def list(self, prefix: str = "") -> "list[str]":
        """Return the sorted paths of all files whose path starts with the prefix."""

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete the file; a missing file is ignored."""

    @abstractmethod
    def exists(self, path: str) -> bool:
        """True if the file exists."""

    def read_text(self, path: str) -> str:
        return self.read_bytes(path).decode("utf-8")

    def write_text(self, path: str, text: str) -> None:
        self.write_bytes(path, text.encode("utf-8"))

    def flush(self, force: bool = False) -> bool:
        """Make buffered writes durable. Returns True when nothing is left pending.

        Without `force`, a store may hold writes back to respect its batching rules.
        """
        return True

    def is_pending(self, path: str) -> bool:
        """True while the store holds a write or delete of the path that it has not sent yet.

        A store that writes through (a local folder) never does. A remote store holds changes back and
        sends them together when a commit is due, which can be many passes after the call that handed a
        file over, and it may send part of what a pass wrote and keep the rest. This is how a caller
        learns that a file has really been sent: it is no longer pending.
        """
        return False

    def close(self) -> None:
        self.flush(force=True)

    def delete_prefix(self, prefix: str) -> int:
        """Delete every file under the prefix; returns how many were listed."""
        paths = self.list(prefix)
        for path in paths:
            self.delete(path)
        return len(paths)


def check_lines(lines: Iterable[str]) -> list[str]:
    """Return the lines as a list, rejecting any that contain a line break."""
    checked = []
    for line in lines:
        if not isinstance(line, str):
            raise TypeError(f"lines must be str, got {type(line).__name__}")
        if "\n" in line or "\r" in line:
            raise ValueError("a line must not contain a line break")
        checked.append(line)
    return checked


class ActorStore(Store):
    """A view of a store for one actor: reads are open, writes only to files it owns.

    This is how the one-writer-per-file rule is enforced. A controller run can write
    its own event files, packets, accepted rows and ledger; a reviewer can write only
    under review/<its id>/. Anything else raises OwnershipError, as does a path no
    actor owns.
    """

    def __init__(self, inner: Store, actor: Actor) -> None:
        self._inner = inner
        self.actor = actor

    @property
    def inner(self) -> Store:
        return self._inner

    def _check(self, path: str) -> None:
        normalize_path(path)
        if not may_write(self.actor, path):
            owner = owner_of(path)
            who = "no actor" if owner is None else f"{owner.kind}{(' ' + owner.name) if owner.name else ''}"
            raise OwnershipError(f"{self.actor.label} may not write {path!r} (owned by {who})")

    def read_bytes(self, path: str) -> bytes:
        return self._inner.read_bytes(path)

    def write_bytes(self, path: str, data: bytes) -> None:
        self._check(path)
        self._inner.write_bytes(path, data)

    def append_lines(self, path: str, lines: Iterable[str]) -> None:
        self._check(path)
        self._inner.append_lines(path, lines)

    def list(self, prefix: str = "") -> "list[str]":
        return self._inner.list(prefix)

    def delete(self, path: str) -> None:
        self._check(path)
        self._inner.delete(path)

    def exists(self, path: str) -> bool:
        return self._inner.exists(path)

    def flush(self, force: bool = False) -> bool:
        return self._inner.flush(force=force)

    def is_pending(self, path: str) -> bool:
        return self._inner.is_pending(path)

    def close(self) -> None:
        self._inner.close()
