"""A small test of one real route: send a few real calls, wait for them, and show what came back.

From a terminal:

    python -m rps.dryrun --route luna-sync --calls 5

From a notebook (no argument parser needed):

    from rps.dryrun import dry_run
    report = dry_run("luna-sync", calls=5)

The calls carry a tiny plumbing prompt and a tiny answer schema, never the writing rules, so a
dry run says whether the route works (key, model name, request form, batch, answer, usage and
prices) and costs a few cents. They are charged to purpose "test". The tool refuses before
sending anything when the estimated cost, added to what earlier dry runs of the route spent
(kept in a small file), would pass the test cap. A batch's id is written to a file before the
wait, so a stopped wait can be picked up again with `resume`.

Keys are read from the environment variables the route's adapter names and are never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import ids
from .config import ConfigError, RouteConfig
from .images import ImageError, load_picture
from .keys import redact
from .ledger import PriceError, PriceTable, Usage
from .prompting import WriterRules, make_rules
from .providers import make_writer
from .providers.base import CallResult, CallSpec, Handle, Writer
from .routing import ImageRoutingError, check_image_allowed
from .routes import STANDARD_ROUTES, standard_route

MAX_CALLS = 25
DEFAULT_CALLS = 5
DEFAULT_CAP_USD = 0.50
DEFAULT_MAX_OUTPUT_TOKENS = 600
DEFAULT_WAIT_MINUTES = 25 * 60
DEFAULT_POLL_SECONDS = 20.0
DEFAULT_SPEND_FILE = "state_local/dryrun_spend.json"

SYSTEM = (
    "You are checking that a data pipeline works. Answer with one json object that follows the schema. "
    'Example: {"answer": "ok", "number": 3}.'
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "number"],
    "properties": {"answer": {"type": "string"}, "number": {"type": "integer"}},
}


def plumbing_rules() -> WriterRules:
    """The tiny rules a dry run sends. They have nothing to do with the real writing rules."""
    return make_rules(SYSTEM, SCHEMA, "dryrun-1", "check")


def task_text(number: int, with_picture: bool) -> str:
    if with_picture:
        return f"Call {number}. Look at the picture. Set answer to one word naming its dominant color, and number to {number}."
    return f"Call {number}. Set answer to the word ok and number to {number}."


# ---------------------------------------------------------------------------- the report


@dataclass
class DryRunReport:
    """What a dry run did. `refused` is set (and nothing was sent) when the tool refused to start."""

    route: str
    calls: int
    refused: str | None = None
    interrupted: bool = False
    outputs: list[Any] = field(default_factory=list)  # one parsed answer (or None) per call, in call order
    results: list[CallResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    estimate_usd: float = 0.0
    spent_usd: float = 0.0  # this route's test spend including this run
    handles: list[dict[str, Any]] = field(default_factory=list)
    handle_file: str | None = None
    minutes: float = 0.0

    @property
    def ok(self) -> bool:
        """True when nothing was refused and every call came back with an answer that fits the schema."""
        return (
            self.refused is None
            and not self.interrupted
            and len(self.results) == self.calls
            and all(result.error is None for result in self.results)
            and all(fits_schema(output) for output in self.outputs)
        )


def fits_schema(answer: Any) -> bool:
    """True when a parsed answer is the tiny schema's object: an `answer` string and a `number` integer."""
    return (
        isinstance(answer, dict)
        and set(answer) == {"answer", "number"}
        and isinstance(answer["answer"], str)
        and isinstance(answer["number"], int)
        and not isinstance(answer["number"], bool)
    )


# ---------------------------------------------------------------------------- the spend file


def read_spend(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def write_spend(path: Path, spend: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: round(v, 8) for k, v in sorted(spend.items())}, indent=1) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------- the run


def route_for(route: str, route_config: RouteConfig | None, cap_usd: float, max_output_tokens: int, calls: int) -> RouteConfig:
    """The route a dry run uses: the standard one by name, capped, with its test settings."""
    if route_config is not None:
        return route_config
    if route not in STANDARD_ROUTES:
        raise ConfigError(f"unknown route {route!r}; use one of {sorted(STANDARD_ROUTES)} or pass route_config")
    overrides: dict[str, Any] = {
        "budget_usd": cap_usd,
        "purpose_caps": {"test": cap_usd},
        "max_output_tokens": max_output_tokens,
        "workers": min(calls, 8),
    }
    if STANDARD_ROUTES[route]["provider"] == "deepseek":
        # A test of a few calls runs at any hour; the production gate keeps real work off-peak.
        overrides.update(enabled=True, options={"offpeak_only": False})
    return standard_route(route, **overrides)


def build_calls(route: str, config: RouteConfig, rules: WriterRules, calls: int, picture: tuple[bytes, str, str] | None, stamp: str) -> list[CallSpec]:
    prompts = {f"{n:03d}": task_text(n, picture is not None) for n in range(1, calls + 1)}
    task = ids.task_id("dryrun", stamp, prompts)
    specs = []
    for line, prompt in prompts.items():
        specs.append(
            CallSpec(
                call_id=ids.call_id(task, line, 1),
                task_id=task,
                line=line,
                attempt=1,
                route=route,
                provider=config.provider,
                model=config.model,
                kind=config.kind,
                effort=config.effort,
                max_output_tokens=config.max_output_tokens,
                spec=prompt,
                pool_version="dryrun",
                purpose="test",
                task_text=prompt,
                rules=rules,
                image=picture[0] if picture else None,
                image_ext=picture[1] if picture else None,
                image_class=picture[2] if picture else None,
            )
        )
    return specs


def _parse(output: str | None) -> Any:
    try:
        return json.loads(output) if output else None
    except json.JSONDecodeError:
        return None


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[col]) for row in rows) for col in range(len(rows[0]))]
    return ["  ".join(cell.ljust(widths[col]) for col, cell in enumerate(row)).rstrip() for row in rows]


def dry_run(
    route: str = "luna-sync",
    calls: int = DEFAULT_CALLS,
    *,
    cap_usd: float = DEFAULT_CAP_USD,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    image: str | Path | None = None,
    image_class: str = "public_common",
    image_size: int = 512,
    wait_minutes: float = DEFAULT_WAIT_MINUTES,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    spend_file: str | Path = DEFAULT_SPEND_FILE,
    resume: str | Path | None = None,
    route_config: RouteConfig | None = None,
    writer: Writer | None = None,
    prices: PriceTable | None = None,
    printer: Callable[[str], Any] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> DryRunReport:
    """Send `calls` real calls through one route and report. See the module description.

    `resume` is the handle file an earlier, stopped dry run wrote: the wait goes on where it
    stopped and nothing is sent again. `route_config`, `writer` and `prices` replace the
    standard route, its adapter and the packaged price table (the tests use them).
    """
    spend_path = Path(spend_file)
    prices = prices or PriceTable.default()
    today: date = now().astimezone(timezone.utc).date()
    started = clock()

    def refuse(reason: str, **extra: Any) -> DryRunReport:
        printer(f"[dryrun] refused: {reason}")
        return DryRunReport(route=route, calls=calls, refused=reason, **extra)

    if resume is None and not 1 <= calls <= MAX_CALLS:
        return refuse(f"a dry run sends between 1 and {MAX_CALLS} calls, not {calls}")
    config = route_for(route, route_config, cap_usd, max_output_tokens, calls)
    batch_mode = config.kind == "batch"
    try:
        prices.price(config.model)
    except PriceError as error:
        return refuse(str(error))
    rules = plumbing_rules()
    writer = writer or make_writer(route, config)
    try:
        writer.check_rules(rules)
    except ConfigError as error:
        return refuse(str(error))
    acceptance = writer.accepting()
    if not acceptance.ok:
        return refuse(f"route {route} is closed: {acceptance.reason}")

    picture = None
    if image is not None:
        try:
            check_image_allowed(image_class, config.provider)
            data, ext, _size = load_picture(image, image_size)
            picture = (data, ext, image_class)
        except (ImageRoutingError, ImageError) as error:
            return refuse(str(error))

    stamp = now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    handle_path = spend_path.parent / f"dryrun_{route}_{stamp}.json"
    spent = read_spend(spend_path)
    estimate = 0.0
    handles: list[Handle]
    call_ids: list[str]

    if resume is not None:
        saved = json.loads(Path(resume).read_text(encoding="utf-8"))
        handles = [Handle.from_dict(item) for item in saved["handles"]]
        call_ids = list(saved["call_ids"])
        calls = len(call_ids)
        estimate = float(saved.get("estimate_usd", 0.0))
        handle_path = Path(resume)
        printer(f"[dryrun] resuming {route}: {len(handles)} batch(es), {calls} calls")
    else:
        specs = build_calls(route, config, rules, calls, picture, stamp)
        try:
            requests = writer.prepare(specs)
        except (ValueError, ImageRoutingError) as error:
            return refuse(f"the calls could not be built: {error}")
        estimate = sum(
            prices.estimate(config.model, r.est_input_tokens, r.max_output_tokens, batch=batch_mode, on=today) for r in requests
        )
        so_far = spent.get(route, 0.0)
        printer(
            f"[dryrun] {route} ({config.provider} {config.model}, {config.kind}): {calls} calls, purpose test; "
            f"estimated at most ${estimate:.4f}; test cap ${cap_usd:.2f}; spent on earlier dry runs of this route ${so_far:.4f}"
        )
        if so_far + estimate > cap_usd + 1e-9:
            return refuse(
                f"${so_far:.4f} already spent plus ${estimate:.4f} estimated would pass the ${cap_usd:.2f} test cap",
                estimate_usd=estimate,
                spent_usd=so_far,
            )
        call_ids = [r.call_id for r in requests]
        handles = []
        for group in writer.split(requests):
            handle = writer.submit(group)
            handles.append(handle)
            # The id is on disk before any wait, so a stopped wait loses nothing.
            handle_path.parent.mkdir(parents=True, exist_ok=True)
            handle_path.write_text(
                json.dumps({"route": route, "created": stamp, "estimate_usd": estimate, "call_ids": call_ids, "handles": [h.to_dict() for h in handles]}, indent=1) + "\n",
                encoding="utf-8",
            )
            printer(f"[dryrun] sent {len(group)} calls as {handle.handle_id}; handle saved to {handle_path}")

    report = DryRunReport(route=route, calls=calls, estimate_usd=estimate, handles=[h.to_dict() for h in handles], handle_file=str(handle_path))
    results: dict[str, CallResult] = {}
    deadline = clock() + wait_minutes * 60
    try:
        for handle in handles:
            while True:
                status = writer.poll(handle)
                if status.state in ("partial", "ended"):
                    for result in writer.results(handle):
                        results[result.call_id] = result
                if status.state == "ended":
                    break
                if status.state in ("failed", "expired", "canceled"):
                    note = f": {status.note}" if status.note else ""
                    printer(f"[dryrun] {handle.handle_id} {status.state}{note}")
                    break
                if clock() >= deadline:
                    report.interrupted = True
                    printer(f"[dryrun] still waiting after {wait_minutes:g} minutes; continue later with resume={str(handle_path)!r}")
                    break
                printer(f"[dryrun] {handle.handle_id}: {status.state}, {len(results)}/{calls} calls in; checking again in {poll_seconds:g} s")
                sleep(poll_seconds)
            if report.interrupted:
                break
            writer.release(handle)
    except KeyboardInterrupt:
        report.interrupted = True
        printer(f"[dryrun] stopped; continue later with resume={str(handle_path)!r}")
    finally:
        writer.close()

    ordered = [results[call_id] for call_id in call_ids if call_id in results]
    report.results = ordered
    report.outputs = [_parse(result.output) for result in ordered]
    for result in ordered:
        report.usage = report.usage + result.usage
        report.cost_usd += prices.cost(config.model, result.usage, batch=batch_mode, tier=result.tier, on=today)
    report.cost_usd = round(report.cost_usd, 8)
    report.minutes = (clock() - started) / 60.0
    if not report.interrupted or ordered:
        spent[route] = spent.get(route, 0.0) + report.cost_usd
        write_spend(spend_path, spent)
    report.spent_usd = spent.get(route, 0.0)
    for line in render(report, config, call_ids):
        printer(line)
    return report


def render(report: DryRunReport, config: RouteConfig, call_ids: Sequence[str]) -> list[str]:
    """The result of a dry run as plain text lines."""
    rows = [["#", "result", "answer", "input", "cache wr", "cache rd", "output", "reasoning"]]
    by_id = {result.call_id: (result, parsed) for result, parsed in zip(report.results, report.outputs)}
    for number, call_id in enumerate(call_ids, 1):
        found = by_id.get(call_id)
        if found is None:
            rows.append([str(number), "no result", "", "", "", "", "", ""])
            continue
        result, parsed = found
        usage = result.usage
        if result.error:
            status, answer = f"failed ({result.kind})", redact(result.error)[:60]
        elif fits_schema(parsed):
            status, answer = "ok", json.dumps(parsed)
        else:
            status, answer = "wrong form", redact(str(result.output))[:60]
        rows.append(
            [str(number), status, answer, str(usage.input_tokens), str(usage.cache_write_tokens), str(usage.cache_read_tokens), str(usage.output_tokens), str(usage.reasoning_tokens)]
        )
    lines = _table(rows)
    total = report.usage
    lines.append(
        f"total tokens: input {total.input_tokens}, cache write {total.cache_write_tokens}, cache read {total.cache_read_tokens}, "
        f"output {total.output_tokens}, reasoning {total.reasoning_tokens}"
    )
    lines.append(
        f"cost ${report.cost_usd:.6f} (estimated at most ${report.estimate_usd:.4f}); "
        f"spent on this route's dry runs so far ${report.spent_usd:.4f}; took {report.minutes:.1f} minutes"
    )
    lines.append("RESULT: " + ("all calls came back with a good answer" if report.ok else "something did not work; read the table above"))
    return lines


# ---------------------------------------------------------------------------- the command line


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rps.dryrun", description="Send a few real calls through one route and show what came back.")
    parser.add_argument("--route", default="luna-sync", help=f"one of {', '.join(sorted(STANDARD_ROUTES))}")
    parser.add_argument("--calls", type=int, default=DEFAULT_CALLS, help=f"how many calls (1 to {MAX_CALLS}; default {DEFAULT_CALLS})")
    parser.add_argument("--cap", type=float, default=DEFAULT_CAP_USD, help="the test cap in dollars for this route's dry runs")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--image", help="a picture file to send with every call (routes that may see it)")
    parser.add_argument("--image-class", default="public_common", help="the class of the picture's origin (see the routing table)")
    parser.add_argument("--wait-minutes", type=float, default=DEFAULT_WAIT_MINUTES)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--spend-file", default=DEFAULT_SPEND_FILE)
    parser.add_argument("--resume", help="the handle file of a stopped dry run")
    args = parser.parse_args(argv)
    try:
        report = dry_run(
            args.route,
            args.calls,
            cap_usd=args.cap,
            max_output_tokens=args.max_output_tokens,
            image=args.image,
            image_class=args.image_class,
            wait_minutes=args.wait_minutes,
            poll_seconds=args.poll_seconds,
            spend_file=args.spend_file,
            resume=args.resume,
        )
    except ConfigError as error:
        print(f"[dryrun] {error}", file=sys.stderr)
        return 2
    if report.refused:
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
