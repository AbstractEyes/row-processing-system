"""A store backed by a Hugging Face dataset repository.

The Hub counts commits per repository (about 320 per hour, failed attempts included)
and degrades with large commits, so this store is deliberately slow and batched:

    - at most one commit per `seconds_per_commit` seconds (default 60)
    - fewer than 100 files per commit (default 99)
    - at most `commits_per_hour` commit attempts in any hour (default 300)
    - exponential backoff on HTTP 429 and other transient errors

Each commit passes `parent_commit`, the head this store last saw. If the branch moved
(another writer committed), the store looks at the new commits: a commit made by
another writer with the same writer tag means two controllers share the repository,
which raises ConcurrentWriterError; commits by other actors (reviewers write under
their own folders) are fine, and the store carries on from the new head.

The HF token is read from the environment (HF_TOKEN by default) when the first remote
call is made, handed to `HfApi`, and never stored, printed or logged here.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from .base import ConcurrentWriterError, StoreError
from .remote import RemoteStore, classify_error, is_missing_file


class HubStore(RemoteStore):
    def __init__(
        self,
        repo_id: str,
        *,
        writer_tag: str = "controller",
        repo_type: str = "dataset",
        revision: str = "main",
        api: Any = None,
        token_env: str = "HF_TOKEN",
        seconds_per_commit: float = 60.0,
        files_per_commit: int = 99,
        commits_per_hour: int = 300,
        cache_dir: str | Path | None = None,
        **kwargs: Any,
    ) -> None:
        if not 1 <= files_per_commit <= 99:
            raise ValueError("files_per_commit must be between 1 and 99 (the Hub advises fewer than 100 files per commit)")
        kwargs.setdefault("max_attempts", 6)
        kwargs.setdefault("backoff_base", 5.0)
        super().__init__(
            max_pending_ops=files_per_commit,
            max_pending_seconds=seconds_per_commit,
            min_interval_seconds=seconds_per_commit,
            max_ops_per_commit=files_per_commit,
            hourly_cap=commits_per_hour,
            **kwargs,
        )
        self.repo_id = repo_id
        self.writer_tag = writer_tag
        self.repo_type = repo_type
        self.revision = revision
        self._api = api
        self._token_env = token_env
        self._cache_root = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "rps-hub-cache" / repo_id.replace("/", "__")
        self._head: str | None = None
        self._head_known = False
        self._own_commits: set[str] = set()

    # ------------------------------------------------------------ the Hub client

    def _client(self) -> Any:
        if self._api is None:
            token = os.environ.get(self._token_env, "")
            if not token:
                raise StoreError(f"{self._token_env} is not set; the dataset repository cannot be reached")
            from huggingface_hub import HfApi

            self._api = HfApi(token=token)
        return self._api

    def ensure_exists(self, private: bool = True) -> None:
        """Create the repository (private by default) if it does not exist."""
        self._call(self._client().create_repo, self.repo_id, private=private, repo_type=self.repo_type, exist_ok=True)

    def _fetch_head(self) -> str | None:
        # The branch's own ref is current; repo_info() can lag a commit made a moment ago.
        refs = self._call(self._client().list_repo_refs, self.repo_id, repo_type=self.repo_type)
        for branch in refs.branches:
            if branch.name == self.revision:
                return branch.target_commit
        return None

    # ------------------------------------------------------------ remote reads

    def _remote_read(self, path: str) -> bytes | None:
        self._cache_root.mkdir(parents=True, exist_ok=True)
        try:
            local = self._client().hf_hub_download(
                repo_id=self.repo_id,
                filename=path,
                repo_type=self.repo_type,
                revision=self.revision,
                local_dir=str(self._cache_root),
            )
        except Exception as exc:
            if is_missing_file(exc):
                return None
            raise
        return Path(local).read_bytes()

    def _remote_exists(self, path: str) -> bool:
        infos = self._client().get_paths_info(self.repo_id, [path], repo_type=self.repo_type, revision=self.revision)
        return any(info.path == path and not hasattr(info, "tree_id") for info in infos)

    def _remote_list(self, prefix: str) -> list[str]:
        directory = prefix.rsplit("/", 1)[0] if "/" in prefix else None
        try:
            items = list(
                self._client().list_repo_tree(
                    self.repo_id, path_in_repo=directory, recursive=True, repo_type=self.repo_type, revision=self.revision
                )
            )
        except Exception as exc:
            if is_missing_file(exc):
                return []
            raise
        return sorted(item.path for item in items if not hasattr(item, "tree_id") and item.path.startswith(prefix))

    # ------------------------------------------------------------ commits

    def _remote_apply(self, operations: list[tuple[str, tuple[str, bytes | None]]]) -> list[str]:
        from huggingface_hub import CommitOperationAdd, CommitOperationDelete

        api = self._client()
        commit_operations = []
        for path, (kind, data) in operations:
            if kind == "put":
                commit_operations.append(CommitOperationAdd(path_in_repo=path, path_or_fileobj=data))
            elif self._remote_exists(path):
                commit_operations.append(CommitOperationDelete(path_in_repo=path))
        if not commit_operations:
            return []
        moment = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        message = f"{self.writer_tag}: {len(commit_operations)} file(s) at {moment}"
        for _ in range(3):
            if not self._head_known:
                self._head = self._fetch_head()
                self._head_known = True
            try:
                info = api.create_commit(
                    repo_id=self.repo_id,
                    repo_type=self.repo_type,
                    operations=commit_operations,
                    commit_message=message,
                    parent_commit=self._head,
                    revision=self.revision,
                )
            except Exception as exc:
                if classify_error(exc) == "conflict":
                    self._resolve_conflict()
                    continue
                raise
            self._head = info.oid
            self._head_known = True
            self._own_commits.add(info.oid)
            return []
        raise ConcurrentWriterError("the repository head kept moving; gave up after three tries")

    def _resolve_conflict(self) -> None:
        """The branch moved under us. Stop if another writer shares our tag; otherwise adopt the new head."""
        api = self._client()
        commits = self._call(api.list_repo_commits, self.repo_id, repo_type=self.repo_type, revision=self.revision)
        newer = []
        for commit in commits:
            if commit.commit_id == self._head:
                break
            newer.append(commit)
        foreign = [
            commit
            for commit in newer
            if commit.title.startswith(f"{self.writer_tag}:") and commit.commit_id not in self._own_commits
        ]
        if foreign:
            raise ConcurrentWriterError(
                f"another '{self.writer_tag}' writer committed to {self.repo_id} "
                f"(commit {foreign[0].commit_id[:8]}); only one controller may write at a time"
            )
        self._head = self._fetch_head()
        self._head_known = True
