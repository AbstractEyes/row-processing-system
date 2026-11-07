"""A store backed by a Hugging Face storage bucket.

A bucket is non-versioned and mutable: a write replaces the file, a delete is
immediate and permanent. That suits event logs, review packets and their pictures,
which change often and whose pictures must really disappear once reviewed. Bucket
batch calls are not transactional (some operations can succeed while others fail),
so a failed operation is retried on its own; every operation here is an overwrite or
a delete, which makes a repeat harmless.

The bucket may be named with a folder, "owner/name/folder" (one or more parts after
the bucket id): every path of the store then lives under that folder, and nothing
outside it is ever listed, read, written or deleted. Test runs and production share a
bucket by each naming a folder of its own. The paths the rest of the system sees are the
same with or without a folder; only the remote paths change.

Writes are buffered and sent in batches (by count and by age), as in the other
remote stores. The token is read from the environment (HF_TOKEN by default) on the
first remote call and never stored, printed or logged here.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from ..layout import parse_bucket
from .base import StoreError
from .remote import RemoteStore, is_missing_file


class BucketStore(RemoteStore):
    def __init__(
        self,
        bucket_id: str,
        *,
        api: Any = None,
        token_env: str = "HF_TOKEN",
        events_per_flush: int = 100,
        seconds_per_flush: float = 5.0,
        min_interval_seconds: float = 1.0,
        max_ops_per_call: int = 500,
        cache_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("max_attempts", 6)
        kwargs.setdefault("backoff_base", 2.0)
        super().__init__(
            max_pending_ops=events_per_flush,
            max_pending_seconds=seconds_per_flush,
            min_interval_seconds=min_interval_seconds,
            max_ops_per_commit=max_ops_per_call,
            hourly_cap=None,
            **kwargs,
        )
        self.bucket_id, self.folder = parse_bucket(bucket_id)  # a PathError (a ValueError) when it is not a bucket name
        self._root = f"{self.folder}/" if self.folder else ""  # where this store's paths start in the bucket
        self._api = api
        self._token_env = token_env
        self._cache_root = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "rps-bucket-cache"

    def _client(self) -> Any:
        if self._api is None:
            token = os.environ.get(self._token_env, "")
            if not token:
                raise StoreError(f"{self._token_env} is not set; the bucket cannot be reached")
            from huggingface_hub import HfApi

            self._api = HfApi(token=token)
        return self._api

    def ensure_exists(self, private: bool = True) -> None:
        """Create the bucket (private by default) if it does not exist. A folder needs no creating."""
        self._call(self._client().create_bucket, self.bucket_id, private=private, exist_ok=True)

    # ------------------------------------------------------------ paths

    def _remote(self, path: str) -> str:
        """The path of a store file inside the bucket (under the folder, if there is one)."""
        return self._root + path

    def _local(self, remote: str) -> str:
        """The store path of a file at a remote path under the folder."""
        return remote[len(self._root) :]

    # ------------------------------------------------------------ remote reads

    def _remote_read(self, path: str) -> bytes | None:
        self._cache_root.mkdir(parents=True, exist_ok=True)
        target = self._cache_root / f"{secrets.token_hex(8)}.download"
        try:
            self._client().download_bucket_files(
                self.bucket_id, [(self._remote(path), str(target))], raise_on_missing_files=True
            )
            return target.read_bytes() if target.exists() else None
        except Exception as exc:
            if is_missing_file(exc):
                return None
            raise
        finally:
            try:
                target.unlink()
            except OSError:
                pass

    def _remote_exists(self, path: str) -> bool:
        remote = self._remote(path)
        infos = self._client().get_bucket_paths_info(self.bucket_id, [remote])
        return any(getattr(info, "path", None) == remote for info in infos)

    def _remote_list(self, prefix: str) -> list[str]:
        wanted = self._remote(prefix)  # the prefix as the bucket knows it: under the folder, if there is one
        directory = wanted.rsplit("/", 1)[0] + "/" if "/" in wanted else None
        items = self._client().list_bucket_tree(self.bucket_id, prefix=directory, recursive=True)
        return sorted(
            self._local(item.path)
            for item in items
            if getattr(item, "type", "file") == "file" and item.path.startswith(wanted)
        )

    # ------------------------------------------------------------ writes

    def _remote_apply(self, operations: list[tuple[str, tuple[str, bytes | None]]]) -> list[str]:
        add = [(data, self._remote(path)) for path, (kind, data) in operations if kind == "put"]
        delete = [self._remote(path) for path, (kind, _) in operations if kind == "delete"]
        try:
            self._client().batch_bucket_files(self.bucket_id, add=add or None, delete=delete or None)
        except Exception as exc:
            failures = getattr(exc, "failures", None)
            if failures is None:
                raise
            deleted = set(delete)
            failed = []
            for failure in failures:
                path = failure.get("path")
                text = str(failure.get("error", "")).lower()
                # Deleting a file that is already gone is the outcome we wanted.
                if path in deleted and ("not found" in text or "does not exist" in text or "no such" in text):
                    continue
                failed.append(path)
            if any(path and not path.startswith(self._root) for path in failed):
                # A failure names a file that is not under this folder, so it cannot be matched to an
                # operation. Send all of them again (an overwrite or a delete repeats harmlessly) rather
                # than count a failure as done.
                return [path for path, _ in operations]
            return [self._local(path) for path in failed if path]
        return []
