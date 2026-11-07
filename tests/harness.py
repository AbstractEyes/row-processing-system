"""A bench for controller tests: shared stores, a fake provider, reviewers, and a fake clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from rps.config import Config
from rps.controller import Controller
from rps.ledger import PriceTable
from rps.providers import FakeProvider, FakeWriter
from rps.review.client import ReviewerClient
from rps.store import LocalStore
from rps.tasks import SpecLine, TaskSpec

from helpers import make_config

DEFAULT_LINES = ("001", "002", "003", "004", "005", "006")


def task_spec(lines=DEFAULT_LINES, *, batch="1", route="fake-batch", purpose="production", variant="", source="photos", image=None):
    return TaskSpec(
        source=source,
        batch=batch,
        route=route,
        purpose=purpose,
        variant=variant,
        lines=tuple(SpecLine(line_id, f"spec for {line_id}", item_id=f"img-{line_id}", image=image) for line_id in lines),
    )


def keep_all(reviewer, item):
    return {"verdict": "keep"}


def fake_image_loader(source, line):
    """Stands in for reading and shrinking a picture file: every picture is the same 64 pixel JPEG."""
    return b"picture-bytes-for-" + line.line_id.encode(), "jpg", 64


class FakeNow:
    """UTC time that moves one second each time it is read."""

    def __init__(self) -> None:
        self.current = datetime(2026, 11, 7, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


class World:
    """Stores, provider, config and reviewers shared by successive controllers.

    Each call to `controller()` builds a new controller (a new run) on the same stores
    and the same pretend provider, which is how a restart is simulated.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        config: Config | None = None,
        behavior: Callable[[Any], Any] | None = None,
        polls_until_ended: int = 1,
        writer_class: type[FakeWriter] = FakeWriter,
        separate_ship_store: bool = True,
        prices: PriceTable | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.config = config or make_config()
        self.provider = FakeProvider()
        self.behavior = behavior
        self.polls_until_ended = polls_until_ended
        self.writer_class = writer_class
        self.prices = prices
        self.state_store = LocalStore(tmp_path / "state", fsync=False)
        self.ship_store = LocalStore(tmp_path / "ship", fsync=False) if separate_ship_store else None
        self.printed: list[str] = []
        self.now = FakeNow()
        self._runs = 0

    def writers(self) -> dict[str, FakeWriter]:
        return {
            name: self.writer_class(
                name, route, provider=self.provider, behavior=self.behavior, polls_until_ended=self.polls_until_ended
            )
            for name, route in self.config.routes.items()
        }

    def controller(self, **overrides: Any) -> Controller:
        self._runs += 1
        kwargs: dict[str, Any] = dict(
            ship_store=self.ship_store,
            prices=self.prices,
            image_loader=fake_image_loader,
            instance=f"20261107T09{self._runs:02d}00Z-test",
            now=self.now,
            printer=self.printed.append,
            sleep=lambda seconds: None,
        )
        kwargs.update(overrides)
        return Controller(self.config, self.state_store, self.writers(), **kwargs)

    def reviewer_client(self, reviewer_id: str) -> ReviewerClient:
        return ReviewerClient(reviewer_id, self.state_store, self.tmp_path / "reviewers" / reviewer_id, fsync=False)

    def run_reviewers(self, verdict_fn: Callable[[Any, Any], dict[str, Any]] = keep_all, only: str | None = None) -> int:
        """Every enabled reviewer works through the packets waiting for it. Returns packets finished."""
        finished = 0
        for reviewer in self.config.review.enabled_reviewers():
            if only is not None and reviewer.id != only:
                continue
            client = self.reviewer_client(reviewer.id)
            results = client.run(lambda item, reviewer=reviewer: verdict_fn(reviewer, item))
            finished += sum(1 for result in results if result.pushed)
        return finished

    def drive(self, controller: Controller, verdict_fn: Callable[[Any, Any], dict[str, Any]] = keep_all, rounds: int = 60) -> int:
        """Alternate controller passes and reviewer passes until everything has settled."""
        for round_number in range(rounds):
            controller.step()
            self.run_reviewers(verdict_fn)
            if controller.is_idle() and not controller.state.open_packets():
                return round_number + 1
        raise AssertionError("the run did not settle")

    # -- looking at what is stored

    def events(self) -> list[Any]:
        from rps.events import replay

        return replay(self.state_store).events

    def events_of(self, kind: str) -> list[Any]:
        return [event for event in self.events() if event.kind == kind]

    def shipped_rows(self) -> list[dict[str, Any]]:
        import json

        store = self.ship_store or self.state_store
        rows = []
        for path in store.list("accepted/"):
            rows.extend(json.loads(line) for line in store.read_text(path).splitlines() if line.strip())
        return rows
