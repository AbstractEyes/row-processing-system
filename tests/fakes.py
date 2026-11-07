"""In-memory stand-ins for the Hugging Face client, used to test the remote stores offline."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from huggingface_hub import CommitOperationAdd, CommitOperationDelete


class FakeHttpError(Exception):
    """An error that carries an HTTP status, like the client's HfHubHTTPError."""

    def __init__(self, status: int, message: str = "", headers: dict[str, str] | None = None) -> None:
        super().__init__(message or f"HTTP {status}")
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


class EntryNotFoundError(FakeHttpError):
    def __init__(self, message: str = "entry not found") -> None:
        super().__init__(404, message)


class RepositoryNotFoundError(FakeHttpError):
    def __init__(self, message: str = "repository not found") -> None:
        super().__init__(404, message)


class FakeBatchError(Exception):
    """Like the client's BucketBatchError: some operations of a batch failed."""

    def __init__(self, failures: list[dict[str, str]]) -> None:
        super().__init__(f"{len(failures)} operation(s) failed")
        self.failures = failures
        self.response = SimpleNamespace(status_code=200, headers={})


class FakeHubApi:
    """A dataset repository in memory, with the calls HubStore makes."""

    def __init__(self, clock=None) -> None:
        self.clock = clock
        self.files: dict[str, bytes] = {}
        self.commit_log: list[dict[str, Any]] = [{"id": "c0000", "title": "initial commit"}]  # newest first
        self.head = "c0000"
        self.create_commit_calls: list[dict[str, Any]] = []
        self.create_failures: list[Exception] = []  # raised one per create_commit call
        self.read_failures: list[Exception] = []
        self.created_repos: list[dict[str, Any]] = []
        self.downloads = 0
        self._counter = 0

    # -- what the store calls

    def create_commit(self, repo_id, operations, *, commit_message, parent_commit=None, repo_type=None, revision=None, **kwargs):
        operations = list(operations)
        self.create_commit_calls.append(
            {
                "repo_id": repo_id,
                "repo_type": repo_type,
                "operations": operations,
                "message": commit_message,
                "parent": parent_commit,
                "at": self.clock() if self.clock else None,
            }
        )
        if self.create_failures:
            raise self.create_failures.pop(0)
        if parent_commit is not None and parent_commit != self.head:
            raise FakeHttpError(412, "parent commit does not match")
        for operation in operations:
            if isinstance(operation, CommitOperationAdd):
                data = operation.path_or_fileobj
                assert isinstance(data, bytes)
                self.files[operation.path_in_repo] = data
            elif isinstance(operation, CommitOperationDelete):
                if operation.path_in_repo not in self.files:
                    raise FakeHttpError(404, "cannot delete a missing file")
                del self.files[operation.path_in_repo]
        return SimpleNamespace(oid=self._new_commit(commit_message))

    def list_repo_refs(self, repo_id, *, repo_type=None, **kwargs):
        return SimpleNamespace(branches=[SimpleNamespace(name="main", target_commit=self.head)])

    def list_repo_commits(self, repo_id, *, repo_type=None, revision=None, **kwargs):
        return [SimpleNamespace(commit_id=entry["id"], title=entry["title"]) for entry in self.commit_log]

    def list_repo_tree(self, repo_id, path_in_repo=None, *, recursive=False, repo_type=None, revision=None, **kwargs):
        if self.read_failures:
            raise self.read_failures.pop(0)
        base = f"{path_in_repo}/" if path_in_repo else ""
        entries = [SimpleNamespace(path=p, size=len(d), blob_id="x") for p, d in sorted(self.files.items()) if p.startswith(base)]
        if path_in_repo and not entries:
            raise EntryNotFoundError()
        return iter(entries)

    def get_paths_info(self, repo_id, paths, *, repo_type=None, revision=None, **kwargs):
        return [SimpleNamespace(path=p, size=len(self.files[p]), blob_id="x") for p in paths if p in self.files]

    def hf_hub_download(self, repo_id, filename, *, repo_type=None, revision=None, local_dir=None, **kwargs):
        if self.read_failures:
            raise self.read_failures.pop(0)
        if filename not in self.files:
            raise EntryNotFoundError(f"{filename} not found")
        self.downloads += 1
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.files[filename])
        return str(target)

    def create_repo(self, repo_id, *, private=None, repo_type=None, exist_ok=False, **kwargs):
        self.created_repos.append({"repo_id": repo_id, "private": private, "repo_type": repo_type, "exist_ok": exist_ok})

    # -- helpers for tests

    def _new_commit(self, title: str) -> str:
        self._counter += 1
        commit_id = f"c{self._counter:04d}"
        self.commit_log.insert(0, {"id": commit_id, "title": title})
        self.head = commit_id
        return commit_id

    def outside_commit(self, files: dict[str, bytes], title: str) -> str:
        """Another actor commits to the repository."""
        self.files.update(files)
        return self._new_commit(title)


class FakeBucketApi:
    """A storage bucket in memory, with the calls BucketStore makes."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.batch_calls: list[dict[str, Any]] = []
        self.call_failures: list[Exception] = []
        self.path_failures: dict[str, int] = {}  # path -> times its operation fails before succeeding
        self.created: list[dict[str, Any]] = []
        self.read_failures: list[Exception] = []

    def batch_bucket_files(self, bucket_id, *, add=None, copy=None, delete=None, token=None):
        self.batch_calls.append({"bucket": bucket_id, "add": list(add or []), "delete": list(delete or [])})
        if self.call_failures:
            raise self.call_failures.pop(0)
        failed: list[dict[str, str]] = []
        for data, path in add or []:
            if self.path_failures.get(path, 0) > 0:
                self.path_failures[path] -= 1
                failed.append({"path": path, "error": "temporary failure"})
                continue
            assert isinstance(data, bytes)
            self.files[path] = data
        for path in delete or []:
            if self.path_failures.get(path, 0) > 0:
                self.path_failures[path] -= 1
                failed.append({"path": path, "error": "temporary failure"})
            elif path in self.files:
                del self.files[path]
            else:
                failed.append({"path": path, "error": "file not found"})
        if failed:
            raise FakeBatchError(failed)

    def list_bucket_tree(self, bucket_id, prefix=None, *, recursive=None, token=None):
        if self.read_failures:
            raise self.read_failures.pop(0)
        base = prefix or ""
        return iter(
            [SimpleNamespace(type="file", path=p, size=len(d)) for p, d in sorted(self.files.items()) if p.startswith(base)]
        )

    def get_bucket_paths_info(self, bucket_id, paths, *, token=None):
        return [SimpleNamespace(type="file", path=p, size=len(self.files[p])) for p in paths if p in self.files]

    def download_bucket_files(self, bucket_id, files, *, raise_on_missing_files=False, token=None):
        if self.read_failures:
            raise self.read_failures.pop(0)
        for remote, local in files:
            if remote not in self.files:
                if raise_on_missing_files:
                    raise EntryNotFoundError(f"{remote} not found")
                continue
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_bytes(self.files[remote])

    def create_bucket(self, bucket_id, *, private=None, exist_ok=False, **kwargs):
        self.created.append({"bucket_id": bucket_id, "private": private, "exist_ok": exist_ok})


class Clock:
    """A clock the tests move by hand, plus a sleep that records and advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
