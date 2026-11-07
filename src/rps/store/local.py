"""A store backed by a local directory."""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from typing import Iterable

from ..layout import PathError, normalize_path
from .base import NotFound, Store, check_lines


class LocalStore(Store):
    """Files under a root directory.

    Whole-file writes are atomic (a temporary file, then a rename), so a reader
    never sees half a file. Appends write the complete batch of lines in one call;
    if a crash leaves a torn last line, the next append starts on a fresh line so
    the torn text never fuses with a good record. With `fsync` on (the default)
    every write is forced to disk before the call returns.
    """

    def __init__(self, root: str | Path, *, fsync: bool = True) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._append_lock = threading.Lock()

    def _target(self, path: str) -> Path:
        normalize_path(path)
        target = (self.root / path).resolve()
        if target != self.root and self.root not in target.parents:
            raise PathError(f"path escapes the store root: {path!r}")
        return target

    def read_bytes(self, path: str) -> bytes:
        target = self._target(path)
        try:
            return target.read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            raise NotFound(path) from None

    def write_bytes(self, path: str, data: bytes) -> None:
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
            self._replace(temp, target)
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _replace(source: Path, target: Path) -> None:
        # On Windows the rename can fail briefly while another process has the target open.
        for attempt in range(8):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def append_lines(self, path: str, lines: Iterable[str]) -> None:
        checked = check_lines(lines)
        if not checked:
            return
        payload = "".join(line + "\n" for line in checked).encode("utf-8")
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The lock keeps threads of this process from interleaving; across processes
        # the one-writer-per-file rule applies.
        with self._append_lock:
            with open(target, "ab+") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                prefix = b""
                if size > 0:
                    handle.seek(size - 1)
                    if handle.read(1) != b"\n":
                        prefix = b"\n"
                handle.write(prefix + payload)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())

    def list(self, prefix: str = "") -> "list[str]":
        if prefix:
            if prefix.startswith("/") or "\\" in prefix or ".." in prefix.split("/"):
                raise PathError(f"invalid prefix: {prefix!r}")
        directory_part = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        base = self.root / directory_part if directory_part else self.root
        if not base.is_dir():
            return []
        found = []
        for directory, _, names in os.walk(base):
            for name in names:
                if name.startswith(".") and name.endswith(".tmp"):
                    continue
                relative = (Path(directory) / name).relative_to(self.root).as_posix()
                if relative.startswith(prefix):
                    found.append(relative)
        return sorted(found)

    def delete(self, path: str) -> None:
        target = self._target(path)
        for attempt in range(8):
            try:
                target.unlink()
                return
            except FileNotFoundError:
                return
            except IsADirectoryError:
                raise PathError(f"not a file: {path!r}") from None
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def exists(self, path: str) -> bool:
        return self._target(path).is_file()
