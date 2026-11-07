"""Shared machinery for stores that live on a remote service.

Remote writes are slow and rate limited, so these stores buffer them. A write, an
append or a delete goes into a pending set (the last operation on a path wins) and a
later `flush` sends many operations in one call. Reads see pending changes first, so
a writer always reads what it wrote.

When a flush is due:

    count      the pending set has `max_pending_ops` entries
    age        the oldest pending entry is `max_pending_seconds` old
    spacing    but never sooner than `min_interval_seconds` after the last flush

A forced flush ignores the count, age and spacing rules (used at shutdown and after
events that must be durable at once) but still respects the hourly cap on attempts.
Each flush sends at most `max_ops_per_commit` operations; a forced flush loops until
nothing is pending.

Transient failures (HTTP 429, 5xx, timeouts, dropped connections) are retried with
exponential backoff and jitter, honoring a Retry-After header. Failed attempts count
toward the hourly cap, as the Hub counts them.

Subclasses supply the remote calls.
"""

from __future__ import annotations

import random
import time
from abc import abstractmethod
from collections import OrderedDict, deque
from typing import Any, Callable, Iterable, TypeVar

from ..keys import redact
from ..layout import normalize_path
from .base import (
    NotFound,
    RateLimitedError,
    Store,
    StoreError,
    StoreUnavailableError,
    check_lines,
)

T = TypeVar("T")
Operation = tuple[str, "bytes | None"]  # ("put", data) or ("delete", None)

_TRANSIENT_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "TimeoutException",
        "NetworkError",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "ProxyError",
        "ChunkedEncodingError",
        "ConnectionError",
        "Timeout",
    }
)
_MISSING_FILE_NAMES = frozenset({"EntryNotFoundError", "RemoteEntryNotFoundError", "FileNotFoundError"})
_MISSING_CONTAINER_NAMES = frozenset({"RepositoryNotFoundError", "BucketNotFoundError", "RevisionNotFoundError"})
_APPEND_CACHE_SIZE = 4


def status_code_of(exc: BaseException) -> int | None:
    """The HTTP status carried by an error, if it has one."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def classify_error(exc: BaseException) -> str:
    """"retry" for transient failures, "conflict" for a stale parent commit, else "fatal"."""
    status = status_code_of(exc)
    if status is not None:
        if status in (408, 425, 429) or status >= 500:
            return "retry"
        if status in (409, 412):
            return "conflict"
        return "fatal"
    for cls in type(exc).__mro__:
        if cls.__name__ in _TRANSIENT_NAMES:
            return "retry"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "retry"
    return "fatal"


def is_missing_file(exc: BaseException) -> bool:
    """True for "that file does not exist" (as opposed to a missing repository or bucket)."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _MISSING_CONTAINER_NAMES:
        return False
    if names & _MISSING_FILE_NAMES:
        return True
    return status_code_of(exc) == 404


def retry_after_seconds(exc: BaseException | None) -> float:
    """Seconds named by a Retry-After header, or 0."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return 0.0
    try:
        value = headers.get("Retry-After") or headers.get("retry-after")
        return max(0.0, float(value)) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def describe_error(exc: BaseException) -> str:
    """A short, key-free description of an error."""
    text = redact(str(exc)).strip().replace("\n", " ")
    status = status_code_of(exc)
    prefix = type(exc).__name__ + (f" (HTTP {status})" if status else "")
    return f"{prefix}: {text[:200]}" if text else prefix


class RemoteStore(Store):
    """A buffered store over a remote service. See the module description."""

    def __init__(
        self,
        *,
        max_pending_ops: int,
        max_pending_seconds: float,
        min_interval_seconds: float,
        max_ops_per_commit: int,
        max_attempts: int = 6,
        backoff_base: float = 5.0,
        backoff_cap: float = 300.0,
        hourly_cap: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if max_ops_per_commit < 1 or max_pending_ops < 1:
            raise ValueError("operation limits must be at least 1")
        self.max_pending_ops = max_pending_ops
        self.max_pending_seconds = max_pending_seconds
        self.min_interval_seconds = min_interval_seconds
        self.max_ops_per_commit = max_ops_per_commit
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.hourly_cap = hourly_cap
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._pending: dict[str, Operation] = {}
        self._appended: "OrderedDict[str, bytes]" = OrderedDict()
        self._first_pending_at: float | None = None
        self._last_flush_at: float | None = None
        self._attempts: deque[float] = deque()
        self.commits = 0  # successful remote calls that carried changes

    # ------------------------------------------------------------ subclass hooks

    @abstractmethod
    def _remote_read(self, path: str) -> bytes | None:
        """The file's bytes, or None if the file does not exist."""

    @abstractmethod
    def _remote_exists(self, path: str) -> bool:
        """True if the file exists remotely."""

    @abstractmethod
    def _remote_list(self, prefix: str) -> list[str]:
        """Remote file paths that start with the prefix."""

    @abstractmethod
    def _remote_apply(self, operations: list[tuple[str, Operation]]) -> list[str]:
        """Apply operations in one call. Returns the paths that failed (to be retried)."""

    # ------------------------------------------------------------ reads

    def read_bytes(self, path: str) -> bytes:
        normalize_path(path)
        pending = self._pending.get(path)
        if pending is not None:
            if pending[0] == "delete":
                raise NotFound(path)
            assert pending[1] is not None
            return pending[1]
        data = self._call(self._remote_read, path)
        if data is None:
            raise NotFound(path)
        return data

    def exists(self, path: str) -> bool:
        normalize_path(path)
        pending = self._pending.get(path)
        if pending is not None:
            return pending[0] == "put"
        return bool(self._call(self._remote_exists, path))

    def list(self, prefix: str = "") -> "list[str]":
        found = set(self._call(self._remote_list, prefix))
        for path, (kind, _) in self._pending.items():
            if path.startswith(prefix):
                if kind == "put":
                    found.add(path)
                else:
                    found.discard(path)
        return sorted(found)

    # ------------------------------------------------------------ writes

    def write_bytes(self, path: str, data: bytes) -> None:
        normalize_path(path)
        self._stage(path, ("put", bytes(data)))

    def append_lines(self, path: str, lines: Iterable[str]) -> None:
        checked = check_lines(lines)
        if not checked:
            return
        normalize_path(path)
        base = self._append_base(path)
        if base and not base.endswith(b"\n"):
            base += b"\n"
        data = base + "".join(line + "\n" for line in checked).encode("utf-8")
        self._appended[path] = data
        self._appended.move_to_end(path)
        while len(self._appended) > _APPEND_CACHE_SIZE:
            self._appended.popitem(last=False)
        self._stage(path, ("put", data))

    def delete(self, path: str) -> None:
        normalize_path(path)
        self._appended.pop(path, None)
        self._stage(path, ("delete", None))

    def _append_base(self, path: str) -> bytes:
        cached = self._appended.get(path)
        if cached is not None:
            return cached
        pending = self._pending.get(path)
        if pending is not None:
            return pending[1] or b""
        return self._call(self._remote_read, path) or b""

    def _stage(self, path: str, operation: Operation) -> None:
        if not self._pending:
            self._first_pending_at = self._clock()
        self._pending[path] = operation
        try:
            self.flush(force=False)
        except RateLimitedError:
            pass  # the write is buffered; a later flush will send it

    # ------------------------------------------------------------ flushing

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _due(self, now: float) -> bool:
        if not self._pending:
            return False
        if self._last_flush_at is not None and now - self._last_flush_at < self.min_interval_seconds:
            return False
        if len(self._pending) >= self.max_pending_ops:
            return True
        return self._first_pending_at is not None and now - self._first_pending_at >= self.max_pending_seconds

    def flush(self, force: bool = False) -> bool:
        """Send pending operations. Returns True when nothing is left pending.

        A normal flush sends one chunk, and only when one is due. A forced flush sends
        everything. Raises RateLimitedError when the hourly cap would be exceeded and
        StoreUnavailableError or StoreError when the remote keeps failing.
        """
        if not self._pending:
            return True
        if not force and not self._due(self._clock()):
            return False
        while self._pending:
            chunk = list(self._pending.items())[: self.max_ops_per_commit]
            self._send(chunk)
            for path, operation in chunk:
                if self._pending.get(path) is operation:
                    del self._pending[path]
            self._last_flush_at = self._clock()
            self._first_pending_at = self._last_flush_at if self._pending else None
            if not force:
                break
        return not self._pending

    def maybe_flush(self) -> bool:
        return self.flush(force=False)

    def _send(self, chunk: list[tuple[str, Operation]]) -> None:
        remaining = chunk
        attempt = 0
        while True:
            self._guard_hourly_cap()
            self._attempts.append(self._clock())
            error: BaseException | None = None
            try:
                failed = self._remote_apply(remaining)
            except Exception as exc:  # classified below; a bug in a subclass is "fatal"
                kind = classify_error(exc)
                if kind != "retry":
                    if isinstance(exc, StoreError):
                        raise
                    raise StoreError(f"remote write failed: {describe_error(exc)}") from exc
                error = exc
                failed = [path for path, _ in remaining]
            if not failed:
                self.commits += 1
                return
            failed_set = set(failed)
            remaining = [(path, operation) for path, operation in remaining if path in failed_set]
            attempt += 1
            if attempt >= self.max_attempts:
                detail = describe_error(error) if error is not None else f"{len(failed)} operation(s) failed"
                raise StoreUnavailableError(f"remote write kept failing after {attempt} attempts: {detail}") from error
            self._sleep(self._backoff(attempt, error))

    def _backoff(self, attempt: int, error: BaseException | None) -> float:
        delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        delay *= 0.5 + 0.5 * self._rng()
        return max(delay, retry_after_seconds(error))

    def _guard_hourly_cap(self) -> None:
        if self.hourly_cap is None:
            return
        now = self._clock()
        while self._attempts and now - self._attempts[0] >= 3600:
            self._attempts.popleft()
        if len(self._attempts) >= self.hourly_cap:
            wait = 3600 - (now - self._attempts[0])
            raise RateLimitedError(
                f"{len(self._attempts)} commit attempts in the last hour (cap {self.hourly_cap}); "
                f"the next slot opens in {wait:.0f} s"
            )

    def _call(self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a remote read with the same retry and backoff as writes."""
        attempt = 0
        while True:
            try:
                return function(*args, **kwargs)
            except Exception as exc:
                if isinstance(exc, StoreError):
                    raise
                if classify_error(exc) != "retry":
                    raise StoreError(f"remote read failed: {describe_error(exc)}") from exc
                attempt += 1
                if attempt >= self.max_attempts:
                    raise StoreUnavailableError(
                        f"remote read kept failing after {attempt} attempts: {describe_error(exc)}"
                    ) from exc
                self._sleep(self._backoff(attempt, exc))
