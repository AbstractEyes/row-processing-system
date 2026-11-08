"""Find out which form of the row schema Gemini takes: one real call for each form, with the full error.

From a notebook (no argument parser needed):

    from rps.geminiprobe import probe_gemini
    report = probe_gemini("flash-batch")

From a terminal:

    python -m rps.geminiprobe --route flash-batch

Each call is the request the Gemini writer prepares for a real spec line: the rule pool's system
prompt, the task text, the row schema in one of its forms (see `rps.providers.schemas.gemini_schema`),
the route's thinking level and token limit. It is sent as one `generate_content` call, so a request
Gemini refuses comes back with its full error text (a batch item carries only a code and one sentence).
Gemini bills nothing for a request it refuses; one it takes is billed like any call, which is why the
forms are tried in order and the tool stops at the first one taken (`stop_at_first`), and why it refuses
to start when the estimate for the calls it may send passes `cap_usd`.

A form that the one-call endpoint takes is not proof that the batch service takes it, but a form that
is refused here is refused there for the same reason. The key is read from GEMINI_API_KEY and never printed.
"""

from __future__ import annotations

import argparse
import dataclasses
from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

from . import ids
from .config import ConfigError, RouteConfig, RulesConfig
from .keys import redact
from .ledger import PriceError, PriceTable, Usage
from .prompting import WriterRules, load_writer_rules, render_task_text
from .providers.base import CallSpec
from .providers.gemini_api import GeminiBatchWriter, parse_response
from .providers.schemas import GEMINI_SCHEMA_MODES
from .routes import standard_route
from .store.remote import describe_error

DEFAULT_CAP_USD = 0.25
DEFAULT_MAX_OUTPUT_TOKENS = 8192
ERROR_CHARS = 4000
# Most faithful first: the first form that is taken is the one to use.
PROBE_ORDER = ("exact", "plain", "none")


@dataclasses.dataclass
class ProbeOutcome:
    """What Gemini did with the request in one form of the schema."""

    mode: str
    accepted: bool
    schema_bytes: int
    error: str | None = None  # the full error text of a refused request
    finish: str | None = None  # how an accepted request ended (STOP, MAX_TOKENS, ...)
    usage: Usage = dataclasses.field(default_factory=Usage)
    cost_usd: float = 0.0
    answer_head: str | None = None  # the start of the answer of an accepted request


@dataclasses.dataclass
class ProbeReport:
    """The outcomes in the order tried. `refused` is set (and nothing was sent) when the tool refused to start."""

    route: str
    outcomes: list[ProbeOutcome] = dataclasses.field(default_factory=list)
    refused: str | None = None
    estimate_usd: float = 0.0

    @property
    def accepted_mode(self) -> str | None:
        """The first form Gemini took, or None."""
        return next((outcome.mode for outcome in self.outcomes if outcome.accepted), None)

    @property
    def ok(self) -> bool:
        return self.refused is None and self.accepted_mode is not None


def _error_text(error: BaseException) -> str:
    """Everything the SDK's error says, with any key-shaped text removed."""
    text = describe_error(error, ERROR_CHARS)
    extra = [
        f"{name}={redact(str(value))[:ERROR_CHARS]}"
        for name in ("status", "message", "details")
        if (value := getattr(error, name, None)) not in (None, "", [])
        and redact(str(value))[:80] not in text  # the SDK's text already holds it
    ]
    return text + (" | " + " ".join(extra) if extra else "")


def probe_gemini(
    route: str = "flash-batch",
    *,
    item: Any = None,
    modes: Sequence[str] = PROBE_ORDER,
    stop_at_first: bool = True,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    cap_usd: float = DEFAULT_CAP_USD,
    route_config: RouteConfig | None = None,
    rules: WriterRules | None = None,
    client: Any | None = None,
    prices: PriceTable | None = None,
    printer: Callable[[str], Any] = print,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ProbeReport:
    """Send the writer's request in each form of the schema as one call, and report what Gemini said.

    `item` is a spec item as the tasks file holds it (the first worked example of the rule pool when
    omitted); `rules` are the writing rules (the rule pool when omitted). `client` replaces the SDK
    client (the tests use it). The report holds one outcome per form tried.
    """

    def refuse(reason: str, **extra: Any) -> ProbeReport:
        printer(f"[geminiprobe] refused: {reason}")
        return ProbeReport(route=route, refused=reason, **extra)

    unknown = [mode for mode in modes if mode not in GEMINI_SCHEMA_MODES]
    if unknown or not modes:
        return refuse(f"the forms to try are some of {list(GEMINI_SCHEMA_MODES)}, not {list(modes)}")
    try:
        config = route_config or standard_route(route, max_output_tokens=max_output_tokens)
    except ConfigError as error:
        return refuse(str(error))
    if config.provider != "gemini":
        return refuse(f"route {route!r} is a {config.provider} route; this tool tries Gemini routes")
    prices = prices or PriceTable.default()
    try:
        prices.price(config.model)
    except PriceError as error:
        return refuse(str(error))
    rules = rules or load_writer_rules(RulesConfig(package="rps.rules"))
    if item is None:
        from . import rules as pool

        item = pool.examples()[0]["item"]
    text = render_task_text(item, ordinal=0, rules=rules)
    stamp = now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    task = ids.task_id("probe", stamp, {"001": text})
    call = CallSpec(
        call_id=ids.call_id(task, "001", 1),
        task_id=task,
        line="001",
        attempt=1,
        route=route,
        provider=config.provider,
        model=config.model,
        kind=config.kind,
        effort=config.effort,
        max_output_tokens=config.max_output_tokens,
        spec=item,
        pool_version="probe",
        purpose="test",
        task_text=text,
        rules=rules,
    )
    today: date = now().astimezone(timezone.utc).date()
    prepared: list[tuple[str, GeminiBatchWriter, Any]] = []
    estimate = 0.0
    for mode in modes:
        writer = GeminiBatchWriter(route, dataclasses.replace(config, options={**config.options, "schema": mode}), client=client)
        try:
            writer.check_rules(rules)
            [request] = writer.prepare([call])
        except (ConfigError, ValueError) as error:
            return refuse(f"the request could not be built in the form {mode!r}: {error}")
        prepared.append((mode, writer, request))
        estimate += prices.estimate(config.model, request.est_input_tokens, request.max_output_tokens, batch=False, on=today)
    if estimate > cap_usd + 1e-9:
        return refuse(f"the calls may cost up to ${estimate:.4f}, over the ${cap_usd:.2f} cap", estimate_usd=estimate)
    first_writer = prepared[0][1]
    acceptance = first_writer.accepting()
    if not acceptance.ok:
        return refuse(f"route {route} is closed: {acceptance.reason}", estimate_usd=estimate)

    report = ProbeReport(route=route, estimate_usd=estimate)
    printer(
        f"[geminiprobe] {route} ({config.model}): {len(prepared)} form(s) to try, {len(text)} characters of task text, "
        f"{len(rules.system)} of rules; at most ${estimate:.4f} if every one is taken"
    )
    for mode, writer, request in prepared:
        inlined = request.payload["request"]
        schema = inlined.config.response_json_schema
        schema_bytes = len(ids.canonical_json(schema).encode("utf-8")) if schema is not None else 0
        try:
            response = writer._client().models.generate_content(
                model=request.payload["model"], contents=inlined.contents, config=inlined.config
            )
        except Exception as error:  # noqa: BLE001 - whatever the SDK raises is the answer
            outcome = ProbeOutcome(mode=mode, accepted=False, schema_bytes=schema_bytes, error=_error_text(error))
        else:
            result = parse_response(request.call_id, response, max_tokens=request.max_output_tokens)
            outcome = ProbeOutcome(
                mode=mode,
                accepted=True,
                schema_bytes=schema_bytes,
                finish=result.stop_reason or ("ended early: " + (result.error or "")),
                usage=result.usage,
                cost_usd=round(prices.cost(config.model, result.usage, batch=False, on=today), 8),
                answer_head=(result.output or "")[:600] or None,
            )
        report.outcomes.append(outcome)
        for line in render_outcome(outcome):
            printer(line)
        if outcome.accepted and stop_at_first:
            break
    writer_close(prepared)
    for line in render_summary(report):
        printer(line)
    return report


def writer_close(prepared: Sequence[tuple[str, GeminiBatchWriter, Any]]) -> None:
    for _mode, writer, _request in prepared:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - nothing to clean up that matters here
            pass


def render_outcome(outcome: ProbeOutcome) -> list[str]:
    head = f"[geminiprobe] schema form {outcome.mode!r} ({outcome.schema_bytes} bytes of schema): "
    if not outcome.accepted:
        return [head + "REFUSED", f"    {outcome.error}"]
    usage = outcome.usage
    lines = [
        head + f"taken ({outcome.finish}); tokens in {usage.input_tokens}, out {usage.output_tokens}, thinking {usage.reasoning_tokens}; cost ${outcome.cost_usd:.6f}"
    ]
    if outcome.answer_head:
        lines.append(f"    answer starts: {outcome.answer_head}")
    return lines


def render_summary(report: ProbeReport) -> list[str]:
    taken = report.accepted_mode
    if taken is None:
        return [
            "RESULT: Gemini refused every form tried; the error texts above are the provider's own. "
            "If they do not name the cause, the request is refused for something other than the schema."
        ]
    refused = [outcome.mode for outcome in report.outcomes if not outcome.accepted]
    sentence = f'RESULT: Gemini took the schema in the form "{taken}"'
    if refused:
        sentence += f' and refused {", ".join(repr(mode) for mode in refused)}'
    return [sentence + f'. Set the route option "schema" to "{taken}" (options: {{"schema": "{taken}"}}) if it is not the default.']


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rps.geminiprobe", description="Try the writer's Gemini request in each form of the row schema.")
    parser.add_argument("--route", default="flash-batch", help="a Gemini route: flash-batch or flash-sync")
    parser.add_argument("--all", action="store_true", help="try every form even after one is taken")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--cap", type=float, default=DEFAULT_CAP_USD, help="refuse to start when the calls may cost more, in dollars")
    args = parser.parse_args(argv)
    report = probe_gemini(args.route, stop_at_first=not args.all, max_output_tokens=args.max_output_tokens, cap_usd=args.cap)
    if report.refused:
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
