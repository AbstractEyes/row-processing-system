"""An automated review seat: a Claude model answers the packets addressed to one reviewer id.

A desk seat is a person or an agent session that pulls a packet, judges each row and pushes one verdict
file. This seat is a reviewer of the same kind that needs nobody at the desk. It finds the packets
addressed to its reviewer id that have no verdict file yet, sends each row to a Claude model through the
Message Batches API, and writes one verdict file per packet through the reviewer client, so the file has
the form of a desk seat's and the controller reads it as it reads any other. The controller is not told
the seat exists: it is a reviewer in the configuration, like the others.

    discover   packets addressed to the reviewer that have no verdict file and are not already sent. A
               packet whose `brief_version` is not the version of the brief this package renders
               (`rps.rules.rules_version()`) is skipped and said so, never answered
    request    one request for each row: a row is judged alone, never beside the others. The system text
               is a short role, the judging rules (`judging.md`), the review brief exactly as the package
               renders it and the configured extra instructions, sent as one block with a cache mark. The
               user text is the row's blind review view and its task as packet.json holds them, and one
               line saying what to return. A row whose packet entry holds a picture (the packet builder
               leaves it out for a family the routing table refuses, and says so with `image_withheld`) is
               sent with the picture as an image block before the text, read from the packet's folder; a
               row without one is sent as text only, exactly as before
    answer     the answer is bound to {verdict, reasons, notes} by `output_config.format` (the reasons to
               the codes the brief lists), and the seat adds the row id itself. `temperature`, `top_p`,
               `top_k`, a prefill, a forced `tool_choice` and `thinking` are never sent; the effort goes
               in `output_config.effort`
    collect    results come back in any order and are matched by `custom_id`. A result that did not
               succeed, a refusal, an answer cut off at the token limit and an answer that fails the
               package's own verdict checks are failed attempts: the row is sent again in a later batch
               until `max_attempts` have failed, and then its packet is left unanswered, with the reason
               said. The seat never makes up a verdict
    write      a packet is answered only when every row has a valid verdict; its file is then written
               through `ReviewerClient` (download, a verdict for each row, check, push), so the client's
               own checks and its burn of local pictures apply

Money. Each model has list prices (dollars per million tokens). The Batches API bills half of them; a
cache read bills 0.1 of the list input price and a one-hour cache write twice it. The two cache prices
are taken at list, without the batch discount, which can only overstate a cost (the shipped price table
does the same). Before a batch is sent its estimate (the input counted at the dearest rate it can have,
a token for every three characters, and `max_tokens` of output for every request) must fit under
`budget_usd` less what has been spent and less the estimates of the batches still out; a batch that
does not fit is cut to the packets that do, and when the first packet does not fit nothing is sent. The
results settle the usage actually billed.

Files this seat writes in the state store, all under `review/<reviewer>/seat/`, a path of the reviewer's
own that the controller never reads and that cannot be taken for a verdict file (those lie directly
under `review/<reviewer>/`):

    seat/ledger.jsonl            what was planned, sent, settled and answered, one JSON line each
    seat/judged/<packet>.jsonl   the valid verdicts of a packet that is not complete yet

A batch is planned in the ledger (its requests and its estimate) before it is sent, and its id is
recorded before anything waits. A seat started after a crash reads the ledger, takes up the batches
still out and collects them (the provider keeps results for 29 days). A batch that was planned but whose
id was never recorded is looked for among the provider's recent batches by size and time, as the
Anthropic writer does, and dropped when none is found. Valid verdicts are written to the store before
the batch that produced them is marked settled, so a crash in between collects the batch again and
changes nothing.

Running: `ApiSeat.step()` is one pass (collect finished batches, write answered packets, send new work),
`ApiSeat.run(stop_event)` steps every `poll_seconds` until stopped, `start_in_thread` runs it beside a
controller and `python -m rps.review.api_seat --config seat.json [--once]` runs it from a shell. The
Anthropic key is read from ANTHROPIC_API_KEY and never logged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from .. import ids, layout
from .. import rules as rule_pool
from ..config import Config, ConfigError, _check_keys, _number, _reject_secret_values, _text
from ..events import format_ts, utc_now
from ..failures import KIND_AUTH, KIND_BAD_REQUEST
from ..ids import canonical_json
from ..ledger import EPSILON, MILLION, Rates, Usage
from ..providers.anthropic_api import (
    API_KEY_ENV,
    BATCH_MAX_BYTES,
    BATCH_MAX_REQUESTS,
    EFFORTS,
    IMAGE_TOKENS,
    RECOVER_MAX_LISTED,
    RECOVER_PAGE,
    RECOVER_SKEW,
    batch_size,
    usage_of,
)
from ..providers.common import api_key, base64_text, classify_exception, estimate_text_tokens, http_status, lookup, mime_type
from ..providers.schemas import ANTHROPIC, SchemaError, check_schema
from ..runtime import build_state_store
from ..store.base import ActorStore, NotFound, Store, StoreError
from ..store.remote import describe_error
from .client import ReviewerClient, ReviewError
from .packets import REASON_PATTERN, VERDICT_VALUES, PacketError, parse_packet, parse_verdict_lines, validate_verdict

SEAT_DIR = "seat"
DEFAULT_WORK_DIR = "seat_local"

# List prices, dollars per million tokens: (input, output).
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
BATCH_DISCOUNT = 0.5  # the Batches API bills this share of the list input and output prices
CACHE_READ_FACTOR = 0.1  # a cache read bills this share of the list input price
CACHE_WRITE_FACTOR = 2.0  # a one-hour cache write bills this multiple of the list input price
CACHE_TTL = "1h"
# A prompt past this many (estimated) tokens is billed at rates this seat does not model, so it is never sent.
LONG_PROMPT_TOKENS = 100_000
# What one picture counts as in the estimate and in the budget check: the adapter's figure for a picture.
PICTURE_TOKENS = IMAGE_TOKENS

# A batch that was planned and has no recorded id is looked for for this long before it is dropped.
RECOVER_WAIT = timedelta(minutes=2)
# Passes in a row that may fail before a running seat stops.
MAX_FAILED_STEPS = 10

ROLE = (
    "You review rows for a dataset. Other AI models write the rows; you read one row at a time against the task it was "
    "written for and return a verdict. You never write, edit or rewrite a row. Below are the rules for judging a row, "
    "then the review brief."
)
ASK_LINE = "Judge this row. Return its verdict as the JSON object the answer format asks for."

EXIT_OK, EXIT_INVALID, EXIT_REFUSED, EXIT_FAILED = 0, 1, 2, 3
PROG = "python -m rps.review.api_seat"

PLANNED, SUBMITTED, SETTLED, DROPPED = "planned", "submitted", "settled", "dropped"


class SeatError(Exception):
    """A seat operation that cannot be done."""


class SeatHalted(SeatError):
    """The provider turned the key or a request away: the seat stops for good, with the reason."""


# ---------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class ListPrice:
    """A model's list prices, US dollars per million tokens."""

    input: float
    output: float

    def batch_rates(self) -> Rates:
        """What a request in a batch bills: half the list input and output prices, and the cache prices at list."""
        return Rates(
            input=self.input * BATCH_DISCOUNT,
            output=self.output * BATCH_DISCOUNT,
            cache_write=self.input * CACHE_WRITE_FACTOR,
            cache_read=self.input * CACHE_READ_FACTOR,
        )


def cost_of(usage: Usage, rates: Rates) -> float:
    """Dollars for the usage of a call at the given rates (reasoning tokens bill as output)."""
    dollars = (
        usage.input_tokens * rates.input
        + usage.cache_write_tokens * rates.cache_write
        + usage.cache_read_tokens * rates.cache_read
        + (usage.output_tokens + usage.reasoning_tokens) * rates.output
    ) / MILLION
    return round(dollars, 8)


def _usd(value: float) -> str:
    """Dollars for a progress line: at least cents, and four places while the amount is small ($5.00, $0.431, $0.0012)."""
    whole, _, cents = f"{value:.4f}".rstrip("0").partition(".")
    return f"${whole}.{cents.ljust(2, '0')}"


_SEAT_KEYS = {
    "reviewer_id", "model", "effort", "max_tokens", "budget_usd", "packets_per_batch", "poll_seconds", "max_attempts",
    "extra_instructions", "prices", "work_dir", "run_config", "also_brief_versions",
}


@dataclass(frozen=True)
class SeatConfig:
    """The settings of one seat. Unknown keys are refused, and so is a value that looks like a key.

    `reviewer_id` is the reviewer the seat answers for: it must be a reviewer in the run's configuration,
    since the controller only addresses packets to those. `model` must have a price (the defaults cover
    `claude-haiku-5-5` and `claude-sonnet-5-5`; any other model needs its list prices under `prices`).
    `budget_usd` is a hard cap on everything this seat spends, across runs. `packets_per_batch` is the most
    packets one batch holds. `max_attempts` is how many times a row may fail before its packet is left
    unanswered. `extra_instructions` is text added to the end of the system prompt. `work_dir` is the local
    folder the reviewer client works in (the seat's pictures and verdict files never leave it), and
    `run_config` names the run's configuration file, from which the command line takes the state store.
    `also_brief_versions` lists earlier brief versions whose packets the seat still judges, by the brief this
    package renders: after a change of the rules, packets made just before it would otherwise never be answered.
    The owner names a version there only when the newer brief judges those rows the same way.
    """

    reviewer_id: str
    model: str
    budget_usd: float
    effort: str = "high"
    max_tokens: int = 16000
    packets_per_batch: int = 50
    poll_seconds: float = 60.0
    max_attempts: int = 3
    extra_instructions: str | None = None
    prices: Mapping[str, ListPrice] = field(default_factory=lambda: {m: ListPrice(*p) for m, p in DEFAULT_PRICES.items()})
    work_dir: str | None = None
    run_config: str | None = None
    also_brief_versions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = self._base_dict()
        if self.also_brief_versions:  # only when set, so that a seat without it reads as it always did
            data["also_brief_versions"] = list(self.also_brief_versions)
        return data

    def _base_dict(self) -> dict[str, Any]:
        return {
            "reviewer_id": self.reviewer_id,
            "model": self.model,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "budget_usd": self.budget_usd,
            "packets_per_batch": self.packets_per_batch,
            "poll_seconds": self.poll_seconds,
            "max_attempts": self.max_attempts,
            "extra_instructions": self.extra_instructions,
            "prices": {m: {"input": p.input, "output": p.output} for m, p in sorted(self.prices.items())},
            "work_dir": self.work_dir,
            "run_config": self.run_config,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SeatConfig":
        where = "seat"
        if not isinstance(data, Mapping):
            raise ConfigError(f"{where}: expected an object")
        _reject_secret_values(data, where)
        _check_keys(data, _SEAT_KEYS, where)
        reviewer_id = data.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not layout.NAME_PATTERN.match(reviewer_id):
            raise ConfigError(f"{where}.reviewer_id: expected letters, digits, '.', '_' or '-', got {reviewer_id!r}")
        prices = {model: ListPrice(*price) for model, price in DEFAULT_PRICES.items()}
        prices.update(_prices(data.get("prices", {}), f"{where}.prices"))
        model = _text(data.get("model"), f"{where}.model")
        if model not in prices:
            raise ConfigError(
                f"{where}.model: no prices for {model!r}; give its list prices (dollars per million tokens) under "
                f'"prices", for example {{"prices": {{"{model}": {{"input": 1.0, "output": 5.0}}}}}}'
            )
        effort = data.get("effort", "high")
        if effort not in EFFORTS:
            raise ConfigError(f"{where}.effort: must be one of {list(EFFORTS)}, got {effort!r}")
        if "budget_usd" not in data:
            raise ConfigError(f"{where}.budget_usd: required (the most this seat may spend, in dollars)")
        also = data.get("also_brief_versions", [])
        if not isinstance(also, (list, tuple)) or not all(isinstance(v, str) and v.strip() for v in also):
            raise ConfigError(f"{where}.also_brief_versions: expected a list of brief versions")
        return cls(
            reviewer_id=reviewer_id,
            model=model,
            budget_usd=float(_number(data["budget_usd"], f"{where}.budget_usd", minimum=0)),
            effort=effort,
            max_tokens=_number(data.get("max_tokens", 16000), f"{where}.max_tokens", minimum=1, integer=True),
            packets_per_batch=_number(
                data.get("packets_per_batch", 50), f"{where}.packets_per_batch", minimum=1, integer=True
            ),
            poll_seconds=float(_number(data.get("poll_seconds", 60.0), f"{where}.poll_seconds", minimum=0)),
            max_attempts=_number(data.get("max_attempts", 3), f"{where}.max_attempts", minimum=1, maximum=10, integer=True),
            extra_instructions=_text(data.get("extra_instructions"), f"{where}.extra_instructions", allow_none=True),
            prices=prices,
            work_dir=_text(data.get("work_dir"), f"{where}.work_dir", allow_none=True),
            run_config=_text(data.get("run_config"), f"{where}.run_config", allow_none=True),
            also_brief_versions=tuple(dict.fromkeys(v.strip() for v in also)),
        )


def _prices(data: Any, where: str) -> dict[str, ListPrice]:
    if not isinstance(data, Mapping):
        raise ConfigError(f"{where}: expected an object with one entry for each model")
    found: dict[str, ListPrice] = {}
    for model, entry in data.items():
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where}.{model}: expected an object with input and output prices")
        _check_keys(entry, {"input", "output"}, f"{where}.{model}")
        for key in ("input", "output"):
            if key not in entry:
                raise ConfigError(f"{where}.{model}.{key}: required (dollars per million tokens)")
        found[str(model)] = ListPrice(
            input=float(_number(entry["input"], f"{where}.{model}.input", minimum=0)),
            output=float(_number(entry["output"], f"{where}.{model}.output", minimum=0)),
        )
    return found


# ---------------------------------------------------------------------------- the brief and the prompt


@dataclass(frozen=True)
class Brief:
    """The review brief a seat judges by and the version a packet must carry to be answered with it."""

    version: str
    text: str


def package_brief() -> Brief:
    """The brief this installed package renders: the rule pool's `review_brief.md` and its version (the VERSION file)."""
    return Brief(version=rule_pool.rules_version(), text=rule_pool.review_brief())


def reason_codes(brief_text: str) -> tuple[str, ...]:
    """The reason codes the brief lists in its table "What to read for", in order (empty when it has no such table)."""
    codes: list[str] = []
    inside = False
    for line in brief_text.splitlines():
        if line.startswith("#"):
            inside = line.lstrip("#").strip().lower() == "what to read for"
        elif inside:
            match = re.match(r"^\|\s*([a-z0-9_]+)\s*\|", line)
            if match and REASON_PATTERN.match(match.group(1)) and match.group(1) not in codes:
                codes.append(match.group(1))
    return tuple(codes)


def judging_text() -> str:
    """The seat's judging rules, packaged beside this module."""
    return resources.files(__package__).joinpath("judging.md").read_text(encoding="utf-8")


def system_text(brief: Brief, extra_instructions: str | None = None) -> str:
    """The system prompt: the role, the judging rules, the brief exactly as rendered, then any extra instructions."""
    parts = [ROLE, judging_text().strip(), brief.text.strip()]
    if extra_instructions and extra_instructions.strip():
        parts.append(extra_instructions.strip())
    return "\n\n".join(parts) + "\n"


def answer_schema(reasons: Sequence[str] = ()) -> dict[str, Any]:
    """The shape of an answer, in the dialect Anthropic's structured outputs take (the size limits of
    `VERDICT_SCHEMA` are not allowed there, so the package's own checks read the answer after it arrives).

    `reasons` are the codes the brief lists; without any, a code is a short code as `VERDICT_SCHEMA` has it.
    """
    code: dict[str, Any] = {"type": "string", "enum": list(reasons)} if reasons else {"type": "string", "pattern": REASON_PATTERN.pattern}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["verdict", "reasons", "notes"],
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICT_VALUES)},
            "reasons": {
                "type": "array",
                "items": code,
                "description": "short codes; at least one is required for fix and reject",
            },
            "notes": {"type": "string", "description": "name the problem and where it is (which field or thought)"},
        },
    }


def request_custom_id(packet_id: str, ordinal: int, attempt: int) -> str:
    """The id a row's request goes under in a batch, made with the package's call id and custom id helpers.

    The call id's task is a hash of the packet id, its line is the row's place in the packet (counting from 1)
    and its attempt counts this seat's tries at the row; the result is within the provider's limits (letters,
    digits, "_" and "-", at most 64 characters) and the same for the same row and try.
    """
    task = "t" + hashlib.sha256(packet_id.encode("utf-8")).hexdigest()[:16]
    return ids.custom_id(ids.call_id(task, f"{ordinal:03d}", attempt))


def user_text(entry: Mapping[str, Any]) -> str:
    """The user message for a row: its blind review view and task, as packet.json holds them, and what to return."""
    view = {"row_id": entry["row_id"], "row": entry["row"], "task": entry.get("task")}
    return json.dumps(view, ensure_ascii=True, indent=1, allow_nan=False) + "\n\n" + ASK_LINE


# ---------------------------------------------------------------------------- results


@dataclass(frozen=True)
class Outcome:
    """What one request came to: a valid verdict, or a failed attempt with its kind and reason.

    `usage` and `usd` are what the provider billed for it (nothing for a request that did not run).
    """

    verdict: dict[str, Any] | None = None
    kind: str = ""
    reason: str = ""
    usage: Usage = field(default_factory=Usage)
    usd: float = 0.0


def read_answer(text: str, row_id: str) -> tuple[dict[str, Any] | None, str]:
    """A verdict from the model's answer text: (verdict, "") when it is valid, else (None, why not).

    The seat adds `row_id`; the answer holds only verdict, reasons and notes. The package's own verdict
    checks decide whether it is valid.
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None, "the answer is not valid JSON"
    if not isinstance(data, dict):
        return None, "the answer is not a JSON object"
    extra = sorted(set(data) - {"verdict", "reasons", "notes"})
    if extra:
        return None, f"the answer has unexpected field(s) {extra}"
    candidate = {"row_id": row_id, **data}
    problems = validate_verdict(candidate, allowed_row_ids=[row_id])
    if problems:
        return None, "; ".join(problems)
    return {
        "row_id": row_id,
        "verdict": candidate["verdict"],
        "reasons": list(candidate.get("reasons", [])),
        "notes": candidate.get("notes", ""),
    }, ""


def outcome_of(item: Any, row_id: str, rates: Rates) -> Outcome:
    """One line of a batch's results as an outcome: the result type first, then why the model stopped, then the answer."""
    result = lookup(item, "result")
    kind = lookup(result, "type")
    if kind == "errored":
        error = lookup(result, "error", "error")
        said = str(lookup(error, "message") or "")[:120]
        return Outcome(kind="errored", reason=f"{lookup(error, 'type')}: {said}" if said else str(lookup(error, "type")))
    if kind == "canceled":
        return Outcome(kind="canceled", reason="the request was canceled before it ran")
    if kind == "expired":
        return Outcome(kind="expired", reason="the request expired before it ran")
    if kind != "succeeded":
        return Outcome(kind="errored", reason=f"unknown result type {kind!r}")
    message = lookup(result, "message")
    usage = usage_of(lookup(message, "usage"))
    usd = cost_of(usage, rates)  # a billed answer costs money whether or not it is usable
    stop = lookup(message, "stop_reason")
    if stop == "refusal":
        category = lookup(message, "stop_details", "category")
        reason = f"the model declined to answer ({category})" if category else "the model declined to answer"
        return Outcome(kind="refusal", reason=reason, usage=usage, usd=usd)
    if stop == "max_tokens":
        return Outcome(kind="max_tokens", reason="the answer was cut off at the token limit", usage=usage, usd=usd)
    if stop != "end_turn":
        return Outcome(kind="stopped", reason=f"the answer ended with {stop or 'no stop reason'}", usage=usage, usd=usd)
    text = "".join(
        str(lookup(block, "text") or "") for block in (lookup(message, "content") or []) if lookup(block, "type") == "text"
    )
    verdict, problem = read_answer(text, row_id)
    if verdict is None:
        return Outcome(kind="invalid", reason=problem, usage=usage, usd=usd)
    return Outcome(verdict=verdict, usage=usage, usd=usd)


# ---------------------------------------------------------------------------- the ledger


@dataclass(frozen=True)
class Ask:
    """One request: a row of a packet, sent to be judged for the `attempt`-th time."""

    custom_id: str
    packet_id: str
    row_id: str
    attempt: int

    def to_dict(self) -> dict[str, Any]:
        return {"custom_id": self.custom_id, "packet_id": self.packet_id, "row_id": self.row_id, "attempt": self.attempt}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Ask":
        return cls(str(data["custom_id"]), str(data["packet_id"]), str(data["row_id"]), int(data["attempt"]))


@dataclass
class Batch:
    """A batch as the ledger knows it: planned, then submitted (it has the provider's id), then settled or dropped."""

    key: str
    model: str
    estimate_usd: float
    asks: tuple[Ask, ...]
    planned_at: datetime
    batch_id: str | None = None
    adopted: bool = False
    state: str = PLANNED

    @property
    def open(self) -> bool:
        return self.state in (PLANNED, SUBMITTED)


def _when(value: Any) -> datetime | None:
    """A timestamp from the SDK (a datetime) or from plain data (ISO text), in UTC; None when unreadable."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SeatLedger:
    """What the seat has planned, sent, spent and answered, kept in the store as `seat/ledger.jsonl`.

    One JSON line per record, appended and never rewritten; the state is a replay of the lines, so a seat
    started after a crash finds its batches and its spend where they were. A line that cannot be read (a
    torn last line) is skipped. Records:

        planned    key, at, model, estimate_usd, requests [{custom_id, packet_id, row_id, attempt}]
        submitted  key, at, batch_id, adopted (the provider's id; the batch is out)
        settled    key, at, batch_id, usd, usage, judged, counts, failed [{..., kind, reason}]
        dropped    key, at, reason (the batch never reached the provider, or it was not ours)
        answered   at, packet_id, rows, counts (the verdict file was pushed)
        gave_up    at, packet_id, reason (the packet is left unanswered)
    """

    def __init__(self, store: Store, reviewer_id: str) -> None:
        self._store = store
        self.path = f"{layout.review_prefix(reviewer_id)}{SEAT_DIR}/ledger.jsonl"
        self._lock = threading.Lock()
        self.batches: dict[str, Batch] = {}
        self.spent_usd = 0.0
        self.usage = Usage()
        self.rows_judged = 0
        self.verdicts: Counter[str] = Counter()
        self.failures: Counter[str] = Counter()
        self.failed: dict[tuple[str, str], list[str]] = {}  # (packet, row) -> the reasons of its failed attempts
        self.answered: set[str] = set()
        self.gave_up: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            text = self._store.read_text(self.path)
        except NotFound:
            return
        records, _problems = parse_verdict_lines(text)  # the same tolerant JSON-lines reader
        for record in records:
            try:
                self._apply(record)
            except (KeyError, TypeError, ValueError):
                continue  # a record this version cannot read is left out

    def add(self, record: Mapping[str, Any], *, durable: bool = False) -> None:
        """Append a record; with `durable` the store is flushed so the record has really been sent."""
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        self._store.append_lines(self.path, [line])
        if durable:
            self._store.flush(force=True)
        with self._lock:
            self._apply(record)

    def _apply(self, record: Mapping[str, Any]) -> None:
        kind = record.get("kind")
        key = record.get("key")
        batch = self.batches.get(key) if isinstance(key, str) else None
        if kind == "planned" and isinstance(key, str) and key not in self.batches:
            moment = _when(record["at"])
            if moment is None:
                raise ValueError("planned record without a time")
            self.batches[key] = Batch(
                key=key,
                model=str(record["model"]),
                estimate_usd=float(record["estimate_usd"]),
                asks=tuple(Ask.from_dict(item) for item in record["requests"]),
                planned_at=moment,
            )
        elif kind == "submitted" and batch is not None and batch.state == PLANNED:
            batch.batch_id = str(record["batch_id"])
            batch.adopted = bool(record.get("adopted"))
            batch.state = SUBMITTED
        elif kind == "dropped" and batch is not None and batch.open:
            batch.state = DROPPED
            batch.asks = ()  # a batch that is over is remembered by its key and id only
        elif kind == "settled" and batch is not None and batch.state == SUBMITTED:
            batch.state = SETTLED
            batch.asks = ()
            self.spent_usd += float(record["usd"])
            self.usage = self.usage + Usage.from_dict(record.get("usage"))
            self.rows_judged += int(record.get("judged", 0))
            self.verdicts.update({str(name): int(number) for name, number in dict(record.get("counts", {})).items()})
            for failure in record.get("failed", []):
                self.failed.setdefault((str(failure["packet_id"]), str(failure["row_id"])), []).append(str(failure["reason"]))
                self.failures[str(failure["kind"])] += 1
        elif kind == "answered":
            self.answered.add(str(record["packet_id"]))
        elif kind == "gave_up":
            self.gave_up[str(record["packet_id"])] = str(record["reason"])

    # ------------------------------------------------------------ queries

    def open_batches(self) -> list[Batch]:
        """The batches planned or out, oldest first."""
        with self._lock:
            return [batch for batch in self.batches.values() if batch.open]

    def held_usd(self) -> float:
        """The estimates of the batches still out: money spoken for but not yet spent."""
        return sum(batch.estimate_usd for batch in self.open_batches())

    def in_flight_packets(self) -> set[str]:
        return {ask.packet_id for batch in self.open_batches() for ask in batch.asks}

    def failed_attempts(self, packet_id: str, row_id: str) -> int:
        return len(self.failed.get((packet_id, row_id), ()))

    def last_failure(self, packet_id: str, row_id: str) -> str:
        reasons = self.failed.get((packet_id, row_id), ())
        return reasons[-1] if reasons else ""


# ---------------------------------------------------------------------------- the seat


@dataclass
class Built:
    """A request ready to send, with what it is estimated to cost."""

    ask: Ask
    payload: dict[str, Any]
    usd: float
    size: int


@dataclass
class Plan:
    """The batch to send next: whole packets, in order, that fit under what is left of the budget."""

    packets: list[str] = field(default_factory=list)
    built: list[Built] = field(default_factory=list)
    usd: float = 0.0
    size: int = 0
    blocked: tuple[str, float] | None = None  # the packet that did not fit, and what it was estimated to cost


@dataclass
class Scan:
    """What the store holds for this reviewer: packets published, verdict files pushed, and the difference."""

    remote: set[str]
    pushed: set[str]

    @property
    def pending(self) -> set[str]:
        return self.remote - self.pushed


@dataclass
class StepResult:
    """What one pass did."""

    settled: int = 0  # batches whose results were taken in
    answered: int = 0  # packets whose verdict file was written
    sent: int = 0  # batches sent
    requests: int = 0  # requests in them


def make_client() -> Any:
    """The Anthropic client, made from ANTHROPIC_API_KEY (SeatError when it is not set)."""
    key = api_key(API_KEY_ENV)
    if key is None:
        raise SeatError(f"{API_KEY_ENV} is not set")
    import anthropic  # imported here: the SDK is slow to load

    return anthropic.Anthropic(api_key=key)


class ApiSeat:
    """A reviewer that answers its packets through the Message Batches API. See the module description.

    `store` is the state store (the seat makes its own view of it for its ledger, and the reviewer client
    makes another for the verdict files). `client` is the Anthropic client; left out, it is made from the
    environment. `brief` is the brief to judge by (the package's own by default). `work_dir` is where the
    reviewer client keeps its local copies, one folder for each reviewer under it.
    """

    def __init__(
        self,
        config: SeatConfig | Mapping[str, Any],
        store: Store,
        *,
        client: Any | None = None,
        brief: Brief | None = None,
        work_dir: str | Path | None = None,
        printer: Callable[[str], Any] = print,
        now: Callable[[], datetime] = utc_now,
        fsync: bool = True,
    ) -> None:
        self.config = config if isinstance(config, SeatConfig) else SeatConfig.from_dict(config)
        self.reviewer_id = self.config.reviewer_id
        self.brief = brief if brief is not None else package_brief()
        self._rates = self.config.prices[self.config.model].batch_rates()
        self._schema = answer_schema(reason_codes(self.brief.text))
        try:
            check_schema(self._schema, ANTHROPIC)
        except SchemaError as error:
            raise ConfigError(f"the seat's answer format: {error}") from None
        self._system = system_text(self.brief, self.config.extra_instructions)
        self._base_tokens = estimate_text_tokens(self._system, canonical_json(self._schema))
        self._print = printer
        self._now = now
        self._store: Store = ActorStore(store, layout.reviewer_actor(self.reviewer_id))
        root = Path(work_dir if work_dir is not None else (self.config.work_dir or DEFAULT_WORK_DIR)) / self.reviewer_id
        self.reviewer = ReviewerClient(self.reviewer_id, store, root, fsync=fsync)
        self._api = client if client is not None else make_client()
        self.ledger = SeatLedger(self._store, self.reviewer_id)
        self._step_lock = threading.Lock()
        self._packets: dict[str, dict[str, Any]] = {}  # packet.json documents, read once
        self._skipped: dict[str, str] = {}  # packets this process leaves alone, and why
        self._noticed: set[str] = set()  # notices already printed
        self._judged: dict[str, dict[str, dict[str, Any]]] = self._load_judged()
        self._waiting = 0
        self.halted: str | None = None
        self.last_error: str | None = None
        self.running = False

    # ------------------------------------------------------------ small things

    def _say(self, text: str) -> None:
        self._print(f"[seat {self.reviewer_id}] {text}")

    def _notice(self, key: str, text: str) -> None:
        """Say something once (a condition that stays true would be said on every pass otherwise)."""
        if key not in self._noticed:
            self._noticed.add(key)
            self._say(text)

    def _stamp(self) -> str:
        return format_ts(self._now())

    def _judged_path(self, packet_id: str) -> str:
        return f"{layout.review_prefix(self.reviewer_id)}{SEAT_DIR}/judged/{ids.check_packet_id(packet_id)}.jsonl"

    def _load_judged(self) -> dict[str, dict[str, dict[str, Any]]]:
        """The valid verdicts kept for packets that were not complete when the last pass ended."""
        found: dict[str, dict[str, dict[str, Any]]] = {}
        prefix = f"{layout.review_prefix(self.reviewer_id)}{SEAT_DIR}/judged/"
        for path in self._store.list(prefix):
            name = path[len(prefix):]
            if not name.endswith(".jsonl") or "/" in name:
                continue
            packet_id = name[: -len(".jsonl")]
            try:
                verdicts, _problems = parse_verdict_lines(self._store.read_text(path))
            except NotFound:
                continue
            kept: dict[str, dict[str, Any]] = {}
            for verdict in verdicts:
                if not validate_verdict(verdict) and verdict["row_id"] not in kept:
                    kept[verdict["row_id"]] = {
                        key: verdict.get(key, "" if key == "notes" else []) for key in ("row_id", "verdict", "reasons", "notes")
                    }
            if kept:
                found[packet_id] = kept
        return found

    def _room(self) -> float:
        """What the budget still allows: the cap, less what was spent and less what batches still out may cost."""
        return self.config.budget_usd - self.ledger.spent_usd - self.ledger.held_usd()

    # ------------------------------------------------------------ one pass

    def step(self) -> StepResult:
        """One pass: collect finished batches, write the packets that are answered, send new work."""
        with self._step_lock:
            result = StepResult()
            self._recover_planned()
            result.settled = self._collect()
            scan = self._scan()
            result.answered = len(self._answer(scan))
            result.sent, result.requests = self._submit(scan)
            in_flight = self.ledger.in_flight_packets()
            self._waiting = len([pid for pid in scan.pending if pid not in in_flight and pid not in self._skipped])
            self._store.flush(force=True)
            return result

    def run(self, stop: threading.Event | None = None) -> str:
        """Step every `poll_seconds` until `stop` is set, the seat is halted, or too many passes in a row fail.

        Returns why it ended: "stopped", "halted" or "failing". A pass that fails (a dropped connection, a
        store that did not answer) is said and tried again on the next poll.
        """
        stop = stop if stop is not None else threading.Event()
        failed = 0
        why = "stopped"
        self.running = True
        try:
            while not stop.is_set():
                try:
                    self.step()
                    failed = 0
                    self.last_error = None
                except SeatHalted as halt:
                    self.halted = str(halt)
                    self._say(f"stopped: {halt}")
                    why = "halted"
                    break
                except Exception as error:  # the next poll tries again; a seat that keeps failing stops
                    failed += 1
                    self.last_error = describe_error(error)
                    self._say(f"pass failed ({self.last_error}); {failed} in a row")
                    if failed >= MAX_FAILED_STEPS:
                        self.halted = f"{failed} passes in a row failed (last: {self.last_error})"
                        self._say(f"stopped: {self.halted}")
                        why = "failing"
                        break
                stop.wait(self.config.poll_seconds)
        finally:
            self.running = False
            try:
                self._store.flush(force=True)
            except StoreError as error:
                self._say(f"the store did not take the last writes ({describe_error(error)})")
        return why

    # ------------------------------------------------------------ what the store holds

    def _scan(self) -> Scan:
        return Scan(remote=set(self.reviewer.remote_packet_ids()), pushed=self.reviewer.pushed_packet_ids())

    def _packet(self, packet_id: str) -> dict[str, Any] | None:
        """A packet's document (read once), or None when it cannot be answered: it is gone, or malformed."""
        if packet_id in self._packets:
            return self._packets[packet_id]
        if packet_id in self._skipped:
            return None
        try:
            document = parse_packet(self._store.read_text(layout.packet_json_path(self.reviewer_id, packet_id)))
        except NotFound:
            return None  # taken back since it was listed
        except PacketError as error:
            self._skip(packet_id, f"it is malformed ({error})")
            return None
        if document["reviewer_id"] != self.reviewer_id or document["packet_id"] != packet_id:
            self._skip(packet_id, "it is not addressed to this reviewer")
            return None
        if not all(isinstance(entry["row_id"], str) and entry["row_id"] for entry in document["rows"]):
            self._skip(packet_id, "a row has no id")
            return None
        self._packets[packet_id] = document
        return document

    def _skip(self, packet_id: str, why: str) -> None:
        self._skipped[packet_id] = why
        self._packets.pop(packet_id, None)
        self._say(f"packet {packet_id} skipped: {why}")

    # ------------------------------------------------------------ batches that were planned and have no id

    def _recover_planned(self) -> None:
        """Settle what a crash (or a send that failed) left half done: look for the batch, or drop it."""
        for entry in self.ledger.open_batches():
            if entry.batch_id is not None:
                continue
            requests = len(entry.asks)  # a batch that is dropped forgets its requests
            found = self._find_batch(entry)
            if found is not None:
                self.ledger.add({"kind": "submitted", "key": entry.key, "at": self._stamp(), "batch_id": found, "adopted": True}, durable=True)
                self._say(f"took up batch {found}, sent before the last stop ({requests} requests)")
            elif self._now() - entry.planned_at >= RECOVER_WAIT:
                dropped = {"kind": "dropped", "key": entry.key, "at": self._stamp(), "reason": "no batch was found for this send"}
                self.ledger.add(dropped, durable=True)
                self._say(f"dropped a send of {requests} requests: the provider has no batch for it")

    def _find_batch(self, entry: Batch) -> str | None:
        """Find the batch a plan became, by its size and when it was made (the way the Anthropic writer does)."""
        known = {batch.batch_id for batch in self.ledger.batches.values() if batch.batch_id}
        bound = entry.planned_at - RECOVER_SKEW
        listing = iter(self._api.messages.batches.list(limit=RECOVER_PAGE))
        for _ in range(RECOVER_MAX_LISTED):
            batch = next(listing, None)
            if batch is None:
                break
            created = _when(lookup(batch, "created_at"))
            if created is not None and created < bound:
                break  # the list is newest first, so everything after this is older still
            batch_id = lookup(batch, "id")
            if not batch_id or str(batch_id) in known or lookup(batch, "processing_status") == "canceling":
                continue
            if batch_size(batch) == len(entry.asks):
                return str(batch_id)
        return None

    # ------------------------------------------------------------ collect

    def _collect(self) -> int:
        """Take in the results of every batch that has ended. Returns how many were settled."""
        settled = 0
        for entry in self.ledger.open_batches():
            if entry.batch_id is None:
                continue
            try:
                batch = self._api.messages.batches.retrieve(entry.batch_id)
            except Exception as error:
                if http_status(error) != 404:
                    raise
                self._settle_unread(entry, "the provider no longer knows this batch")
                settled += 1
                continue
            if lookup(batch, "processing_status") != "ended":
                continue
            if self._settle(entry):
                settled += 1
        return settled

    def _settle(self, entry: Batch) -> bool:
        by_id = {ask.custom_id: ask for ask in entry.asks}
        outcomes: dict[str, Outcome] = {}
        try:
            for item in self._api.messages.batches.results(entry.batch_id):
                ask = by_id.get(str(lookup(item, "custom_id")))
                if ask is not None and ask.custom_id not in outcomes:
                    outcomes[ask.custom_id] = outcome_of(item, ask.row_id, self._rates_of(entry))
        except Exception as error:
            if http_status(error) != 404:
                raise
            self._settle_unread(entry, "the results of this batch can no longer be read")
            return True
        if entry.adopted and not outcomes:
            # Taken up after a crash by its size and time alone, and it holds none of the requests: not ours.
            dropped = {"kind": "dropped", "key": entry.key, "at": self._stamp(), "reason": "the batch taken up held none of these requests"}
            self.ledger.add(dropped, durable=True)
            self._say(f"batch {entry.batch_id} holds none of the requests that were sent; its rows are sent again")
            return False
        for ask in entry.asks:
            outcomes.setdefault(ask.custom_id, Outcome(kind="missing", reason="the batch returned no result for this request"))
        self._record_settled(entry, [(ask, outcomes[ask.custom_id]) for ask in entry.asks])
        return True

    def _rates_of(self, entry: Batch) -> Rates:
        """The rates a batch settles at: its own model's, or the most it could have cost when the model has no price now."""
        price = self.config.prices.get(entry.model)
        if price is not None:
            return price.batch_rates()
        return self._rates

    def _settle_unread(self, entry: Batch, why: str) -> None:
        """A batch whose results cannot be read: every request failed, and the whole estimate counts as spent
        (the batch may have run and been billed, and the budget is a hard cap)."""
        pairs = [(ask, Outcome(kind="lost", reason=why)) for ask in entry.asks]
        self._record_settled(entry, pairs, usd=entry.estimate_usd)

    def _record_settled(self, entry: Batch, pairs: Sequence[tuple[Ask, Outcome]], usd: float | None = None) -> None:
        verdicts: dict[str, list[dict[str, Any]]] = {}
        failed: list[dict[str, Any]] = []
        usage = Usage()
        spent = 0.0
        counts: Counter[str] = Counter()
        for ask, outcome in pairs:
            usage = usage + outcome.usage
            spent += outcome.usd
            if outcome.verdict is not None:
                counts[outcome.verdict["verdict"]] += 1
                verdicts.setdefault(ask.packet_id, []).append(outcome.verdict)
            else:
                failed.append({**ask.to_dict(), "kind": outcome.kind, "reason": outcome.reason})
        # The verdicts reach the store before the batch is marked settled: a crash between the two collects the
        # batch again, and keeping a verdict twice changes nothing.
        for packet_id, items in verdicts.items():
            self._keep(packet_id, items)
        self._store.flush(force=True)
        spent = round(spent if usd is None else usd, 8)
        self.ledger.add(
            {
                "kind": "settled",
                "key": entry.key,
                "at": self._stamp(),
                "batch_id": entry.batch_id,
                "usd": spent,
                "usage": usage.to_dict(),
                "judged": sum(counts.values()),
                "counts": dict(counts),
                "failed": failed,
            },
            durable=True,
        )
        shown = ", ".join(f"{number} {kind}" for kind, number in Counter(item["kind"] for item in failed).most_common())
        self._say(
            f"batch {entry.batch_id} done: {sum(counts.values())} judged, {len(failed)} failed"
            f"{f' ({shown})' if shown else ''}, {_usd(spent)} spent ({_usd(self.ledger.spent_usd)} of {_usd(self.config.budget_usd)})"
        )

    def _keep(self, packet_id: str, verdicts: Sequence[dict[str, Any]]) -> None:
        """Keep valid verdicts for a packet that is not complete yet (a verdict already kept is not written again)."""
        have = self._judged.setdefault(packet_id, {})
        new = [verdict for verdict in verdicts if verdict["row_id"] not in have]
        if not new:
            return
        self._store.append_lines(self._judged_path(packet_id), [json.dumps(item, ensure_ascii=True, separators=(",", ":")) for item in new])
        for item in new:
            have[item["row_id"]] = item

    # ------------------------------------------------------------ write

    def _answer(self, scan: Scan) -> list[str]:
        """Write the verdict file of every packet whose rows all have a valid verdict. Returns the packets answered."""
        answered: list[str] = []
        totals: Counter[str] = Counter()
        for packet_id in sorted(self._judged):
            if packet_id not in scan.pending:
                self._forget(packet_id, scan)
                continue
            document = self._packet(packet_id)
            if document is None:
                continue
            have = self._judged[packet_id]
            if any(entry["row_id"] not in have for entry in document["rows"]):
                continue
            counts = self._write(packet_id, document, have)
            if counts is not None:
                answered.append(packet_id)
                totals.update(counts)
                scan.pushed.add(packet_id)
        if answered:
            self._say(
                f"answered {len(answered)} packets ({sum(totals.values())} rows: "
                + ", ".join(f"{totals[name]} {name}" for name in VERDICT_VALUES)
                + ")"
            )
        return answered

    def _write(
        self, packet_id: str, document: Mapping[str, Any], have: Mapping[str, Mapping[str, Any]]
    ) -> Counter[str] | None:
        """Push a packet's verdict file through the reviewer client. Returns its verdict counts, or None when it did not go out."""
        try:
            if self.reviewer.local_packet(packet_id) is None:
                self.reviewer.download(packet_id)
            outcome = self.reviewer.run_packet(packet_id, lambda item: dict(have[item.row_id]))
        except (ReviewError, NotFound, PacketError) as error:
            self._notice(f"write:{packet_id}:{error}", f"packet {packet_id} not written yet: {error}")
            return None
        if not outcome.pushed:
            why = "; ".join(outcome.errors) or "the verdict file was not accepted"
            self._notice(f"write:{packet_id}:{why}", f"packet {packet_id} not written yet: {why}")
            return None
        counts = Counter(have[entry["row_id"]]["verdict"] for entry in document["rows"])
        self.ledger.add({"kind": "answered", "at": self._stamp(), "packet_id": packet_id, "rows": len(document["rows"]), "counts": dict(counts)})
        self._store.delete(self._judged_path(packet_id))
        shutil.rmtree(self.reviewer.root / packet_id, ignore_errors=True)  # the local copy is no longer needed
        self._judged.pop(packet_id, None)
        self._packets.pop(packet_id, None)
        return counts

    def _forget(self, packet_id: str, scan: Scan) -> None:
        """Drop the verdicts kept for a packet that is no longer waiting: it was answered, or the controller took it back."""
        if packet_id not in scan.pushed:
            self._say(f"packet {packet_id} is no longer in the store (taken back?); its verdicts are dropped")
        self._store.delete(self._judged_path(packet_id))
        self._judged.pop(packet_id, None)
        self._packets.pop(packet_id, None)

    # ------------------------------------------------------------ send

    def _picture_block(self, packet_id: str, entry: Mapping[str, Any]) -> dict[str, Any] | None:
        """The image block for a row whose packet entry holds a picture, or None for a text-only row.

        The picture is read from the packet's folder in the store. A picture the entry names that cannot be
        read is an error, never a silent fall back to the text: the row would be judged without what it is
        judged against.
        """
        name = entry.get("image")
        if not name:
            return None
        path = f"{layout.packet_dir(self.reviewer_id, packet_id)}/{name}"
        ext = str(name).rsplit(".", 1)[-1]
        try:
            data = self._store.read_bytes(path)
            media = mime_type(ext)
        except (NotFound, ValueError) as error:
            raise SeatError(f"packet {packet_id}: the picture {name!r} cannot be read ({error})") from None
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64_text(data)}}

    def _request(self, packet_id: str, ordinal: int, entry: Mapping[str, Any], attempt: int) -> Built | None:
        """The request for one row, or None when it cannot be priced (a prompt past the long-prompt limit).

        A picture is counted as `PICTURE_TOKENS` input tokens, a generous fixed figure for each picture.
        """
        text = user_text(entry)
        picture = self._picture_block(packet_id, entry)
        tokens = self._base_tokens + estimate_text_tokens(text) + (PICTURE_TOKENS if picture is not None else 0)
        if tokens > LONG_PROMPT_TOKENS:
            return None
        custom_id = request_custom_id(packet_id, ordinal, attempt)
        payload = {
            "custom_id": custom_id,
            "params": {
                "model": self.config.model,
                "max_tokens": self.config.max_tokens,
                "system": [{"type": "text", "text": self._system, "cache_control": {"type": "ephemeral", "ttl": CACHE_TTL}}],
                "messages": [{"role": "user", "content": [*([picture] if picture is not None else []), {"type": "text", "text": text}]}],
                "output_config": {"format": {"type": "json_schema", "schema": self._schema}, "effort": self.config.effort},
            },
        }
        usd = (tokens * self._rates.input_ceiling + self.config.max_tokens * self._rates.output) / MILLION
        return Built(Ask(custom_id, packet_id, str(entry["row_id"]), attempt), payload, round(usd, 8), len(canonical_json(payload)) + 1)

    def _plan(self, scan: Scan) -> Plan:
        """Choose the packets of the next batch: in order, whole, and only as many as the budget has room for."""
        plan = Plan()
        room = self._room()
        in_flight = self.ledger.in_flight_packets()
        for packet_id in sorted(scan.pending):
            if len(plan.packets) >= self.config.packets_per_batch:
                break
            if packet_id in in_flight or packet_id in self._skipped:
                continue
            document = self._packet(packet_id)
            if document is None:
                continue
            version = str(document["brief_version"])
            if version != self.brief.version and version not in self.config.also_brief_versions:
                self._skip(packet_id, f"its brief version is {version}, the brief this package renders is {self.brief.version}")
                continue
            have = self._judged.get(packet_id, {})
            todo = [(place, entry) for place, entry in enumerate(document["rows"], start=1) if entry["row_id"] not in have]
            if not todo:
                continue  # complete: the next pass writes it
            exhausted = [
                entry["row_id"] for _, entry in todo if self.ledger.failed_attempts(packet_id, entry["row_id"]) >= self.config.max_attempts
            ]
            if exhausted:
                self._give_up(packet_id, exhausted[0])
                continue
            self.ledger.gave_up.pop(packet_id, None)
            try:
                built = [self._request(packet_id, place, entry, self.ledger.failed_attempts(packet_id, entry["row_id"]) + 1) for place, entry in todo]
            except SeatError as error:
                self._skip(packet_id, str(error))
                continue
            if any(item is None for item in built):
                self._skip(packet_id, f"a row is longer than {LONG_PROMPT_TOKENS:,} tokens, which the seat does not price")
                continue
            ready: list[Built] = [item for item in built if item is not None]
            usd = sum(item.usd for item in ready)
            size = sum(item.size for item in ready)
            if plan.usd + usd > room + EPSILON:
                plan.blocked = (packet_id, usd)
                break
            if len(plan.built) + len(ready) > BATCH_MAX_REQUESTS or plan.size + size > BATCH_MAX_BYTES:
                break  # the batch is full
            plan.packets.append(packet_id)
            plan.built.extend(ready)
            plan.usd += usd
            plan.size += size
        return plan

    def _give_up(self, packet_id: str, row_id: str) -> None:
        reason = (
            f"row {row_id} failed {self.config.max_attempts} attempts "
            f"(last: {self.ledger.last_failure(packet_id, row_id)})"
        )
        if packet_id not in self.ledger.gave_up:
            self.ledger.add({"kind": "gave_up", "at": self._stamp(), "packet_id": packet_id, "reason": reason})
            self._say(f"packet {packet_id} left unanswered: {reason}")
        self.ledger.gave_up[packet_id] = reason

    def _submit(self, scan: Scan) -> tuple[int, int]:
        """Send the next batch if there is one that fits the budget. Returns (batches sent, requests in them)."""
        plan = self._plan(scan)
        if plan.blocked is not None:
            packet_id, usd = plan.blocked
            self._notice(
                f"budget:{packet_id}",
                f"{'no more' if plan.built else 'nothing'} sent: packet {packet_id} needs about {_usd(usd)} and "
                f"{_usd(max(0.0, self._room() - plan.usd))} of the {_usd(self.config.budget_usd)} budget is left",
            )
        if not plan.built:
            return 0, 0
        key = ids.instance_id(self._now(), secrets.token_hex(2))
        while key in self.ledger.batches:  # two plans in the same second must not share a key
            key = ids.instance_id(self._now(), secrets.token_hex(2))
        # The plan is recorded, and durable, before anything is sent: from here on its estimate is held.
        self.ledger.add(
            {
                "kind": "planned",
                "key": key,
                "at": self._stamp(),
                "model": self.config.model,
                "estimate_usd": round(plan.usd, 8),
                "requests": [item.ask.to_dict() for item in plan.built],
            },
            durable=True,
        )
        try:
            batch = self._api.messages.batches.create(requests=[item.payload for item in plan.built])
        except Exception as error:
            kind, _retryable = classify_exception(error)
            if kind in (KIND_AUTH, KIND_BAD_REQUEST):
                self.ledger.add({"kind": "dropped", "key": key, "at": self._stamp(), "reason": describe_error(error)}, durable=True)
                raise SeatHalted(f"the provider refused the request: {describe_error(error)}") from None
            raise  # it may have reached the provider: the next pass looks for the batch before sending again
        batch_id = lookup(batch, "id")
        if not batch_id:
            raise SeatError("the provider returned no batch id")  # the next pass finds the batch by its size and time
        self.ledger.add({"kind": "submitted", "key": key, "at": self._stamp(), "batch_id": str(batch_id)}, durable=True)
        self._say(
            f"sent batch {batch_id}: {len(plan.packets)} packets, {len(plan.built)} rows, estimate {_usd(plan.usd)} "
            f"({_usd(self._room())} of the {_usd(self.config.budget_usd)} budget left)"
        )
        return 1, len(plan.built)

    # ------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        """Where the seat stands: packets answered, rows judged, the verdicts given, spend, what is out, failures."""
        ledger = self.ledger
        open_batches = ledger.open_batches()
        held = sum(batch.estimate_usd for batch in open_batches)
        return {
            "reviewer_id": self.reviewer_id,
            "model": self.config.model,
            "effort": self.config.effort,
            "running": self.running,
            "halted": self.halted,
            "last_error": self.last_error,
            "packets_answered": len(ledger.answered),
            "rows_judged": ledger.rows_judged,
            "keep": ledger.verdicts.get("keep", 0),
            "fix": ledger.verdicts.get("fix", 0),
            "reject": ledger.verdicts.get("reject", 0),
            "spend_usd": round(ledger.spent_usd, 8),
            "held_usd": round(held, 8),
            "budget_usd": self.config.budget_usd,
            "in_flight": {
                "batches": len(open_batches),
                "requests": sum(len(batch.asks) for batch in open_batches),
                "packets": len({ask.packet_id for batch in open_batches for ask in batch.asks}),
            },
            "waiting_packets": self._waiting,
            "failures": dict(ledger.failures),
            "left_unanswered": dict(ledger.gave_up),
            "skipped": dict(self._skipped),
        }

    def format_status(self) -> str:
        """The status as a few short lines."""
        info = self.status()
        failures = ", ".join(f"{number} {kind}" for kind, number in sorted(info["failures"].items())) or "none"
        flight = info["in_flight"]
        lines = [
            f"reviewer {info['reviewer_id']} ({info['model']}, effort {info['effort']}): "
            + ("halted: " + info["halted"] if info["halted"] else "running" if info["running"] else "not running"),
            f"packets answered {info['packets_answered']}; rows judged {info['rows_judged']} "
            f"(keep {info['keep']}, fix {info['fix']}, reject {info['reject']})",
            f"in flight {flight['batches']} batches with {flight['requests']} requests; waiting {info['waiting_packets']} packets",
            f"spent {_usd(info['spend_usd'])} of {_usd(info['budget_usd'])} ({_usd(info['held_usd'])} held for batches in flight)",
            f"failed attempts: {failures}",
        ]
        if info["left_unanswered"]:
            lines.append(f"left unanswered: {len(info['left_unanswered'])} packets")
        return "\n".join(lines)


# ---------------------------------------------------------------------------- running it


class SeatHandle:
    """A seat running on a thread of its own: its status, and a way to stop it."""

    def __init__(self, seat: ApiSeat, thread: threading.Thread, stop: threading.Event) -> None:
        self.seat = seat
        self.thread = thread
        self._stop = stop

    @property
    def alive(self) -> bool:
        return self.thread.is_alive()

    def status(self) -> dict[str, Any]:
        return self.seat.status()

    def format_status(self) -> str:
        return self.seat.format_status()

    def stop(self, timeout: float | None = 60.0) -> bool:
        """Ask the seat to stop and wait for it. True when the thread has ended. Batches already sent stay with the
        provider; the next seat for this reviewer collects them."""
        self._stop.set()
        self.thread.join(timeout)
        return not self.thread.is_alive()


def _run_config(value: Config | str | Path | None) -> Config | None:
    if value is None or isinstance(value, Config):
        return value
    return Config.load(value)


def open_seat(
    config: SeatConfig | Mapping[str, Any],
    *,
    store: Store | None = None,
    run_config: Config | str | Path | None = None,
    work_dir: str | Path | None = None,
    client: Any | None = None,
    printer: Callable[[str], Any] = print,
    **options: Any,
) -> ApiSeat:
    """A seat for a configuration. The state store is `store`, or the one the run's configuration names
    (`run_config`: a `Config` or the path of its file; left out, the seat's own `run_config` setting). The local
    folder is `work_dir`, else the seat's own, else a folder of the run's work folder, else `seat_local`. Other
    keywords go to `ApiSeat`."""
    seat_config = config if isinstance(config, SeatConfig) else SeatConfig.from_dict(config)
    run = _run_config(run_config if run_config is not None else seat_config.run_config)
    if store is None:
        if run is None:
            raise ConfigError("the seat needs a state store: pass `store`, or name the run's configuration in `run_config`")
        store = build_state_store(run)
    folder = work_dir or seat_config.work_dir or (run.work_path("seat") if run is not None else DEFAULT_WORK_DIR)
    return ApiSeat(seat_config, store, client=client, work_dir=folder, printer=printer, **options)


def start_in_thread(
    config: SeatConfig | Mapping[str, Any],
    *,
    store: Store | None = None,
    run_config: Config | str | Path | None = None,
    work_dir: str | Path | None = None,
    client: Any | None = None,
    printer: Callable[[str], Any] = print,
    **options: Any,
) -> SeatHandle:
    """Start a seat on a background thread, beside a controller, and return its handle.

    The seat opens a view of the state store of its own (a store is not shared between threads), so pass the
    run's `Config` as `run_config` rather than the controller's store. See `open_seat` for the arguments.
    """
    seat = open_seat(config, store=store, run_config=run_config, work_dir=work_dir, client=client, printer=printer, **options)
    stop = threading.Event()
    thread = threading.Thread(target=seat.run, args=(stop,), name=f"rps-seat-{seat.reviewer_id}", daemon=True)
    seat.running = True  # so a status read right after the start already says so; the thread clears it when it ends
    thread.start()
    return SeatHandle(seat, thread, stop)


# ---------------------------------------------------------------------------- command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Answer the packets of one reviewer id through the Message Batches API.")
    parser.add_argument("--config", required=True, help="the seat's configuration, a JSON file (see SeatConfig)")
    parser.add_argument("--once", action="store_true", help="one pass (collect, write, send), then stop")
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Run a seat from a shell. Exit codes: 0 done, 1 invalid input, 2 the provider refused the key or a request,
    3 the configuration, the store or the key failed."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        try:
            data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"cannot read the seat configuration {args.config!r}: {error}") from None
        seat = open_seat(data, printer=lambda text: print(text, file=out, flush=True))
        if args.once:
            seat.step()
            out.write(seat.format_status() + "\n")
            return EXIT_OK
        try:
            seat.run()
        except KeyboardInterrupt:
            out.write("interrupted; batches already sent are collected the next time the seat starts\n")
        out.write(seat.format_status() + "\n")
        return EXIT_REFUSED if seat.halted else EXIT_OK
    except SeatHalted as error:
        err.write(f"error: {error}\n")
        return EXIT_REFUSED
    except (ConfigError, SeatError, StoreError) as error:
        err.write(f"error: {error}\n")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
