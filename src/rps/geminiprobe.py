"""Find out what Gemini refuses in the writer's request: one real call or one batch for each variant, with the full error.

From a notebook (no argument parser needed):

    from rps.geminiprobe import probe_gemini, probe_gemini_batch
    report = probe_gemini("flash-batch")         # the one-call endpoint, one call for each form of the schema
    report = probe_gemini_batch("flash-batch")   # the batch service, one batch of one item for each variant

From a terminal:

    python -m rps.geminiprobe --route flash-batch
    python -m rps.geminiprobe --route flash-batch --batch

Each call is the request the Gemini writer prepares for a real spec line: the rule pool's system
prompt, the task text, the row schema in one of its forms (see `rps.providers.schemas.gemini_schema`),
the route's thinking level and token limit. It is sent as one `generate_content` call, so a request
Gemini refuses comes back with its full error text (a batch item carries only a code and one sentence).
Gemini bills nothing for a request it refuses; one it takes is billed like any call, which is why the
forms are tried in order and the tool stops at the first one taken (`stop_at_first`), and why it refuses
to start when the estimate for the calls it may send passes `cap_usd`.

A form that the one-call endpoint takes is not proof that the batch service takes it, but a form that
is refused here is refused there for the same reason. The key is read from GEMINI_API_KEY and never printed.

The batch service validates a request apart from the one-call endpoint and reports a refusal only after the
batch has run, as a code and a sentence for each item. `probe_gemini_batch` therefore sends the request as
the batch writer builds it, then the same request with one part left out or changed (the schema, the system
instruction, the thinking setting, the metadata, the explicit cache, the field that carries the schema, the
name of the metadata, the OpenAPI form of the schema), then the documented form and the bare request, each as
a batch of one item. It waits for each batch, prints the provider's whole error for a refusal, cancels the
batches that have not answered, and says in plain sentences which part the service refuses. A refused item
costs nothing; a taken one is billed at the batch rate, which `cap_usd` bounds.

The batches go to one version of the Gemini API: the route's `api_version` option, or `--api-version`, or the
SDK's own choice (`v1` in google-genai 3.0.0). The explicit-cache variants need `v1beta`, the only version that
has caching, and the probe uses it for the whole run when one of them is asked for. The first line says which
version the run used, so two runs can be told apart.
"""

from __future__ import annotations

import argparse
import dataclasses
import time
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Callable, Sequence

from . import ids
from .config import ConfigError, RouteConfig, RulesConfig
from .keys import redact
from .ledger import PriceError, PriceTable, Usage
from .prompting import WriterRules, load_writer_rules, render_task_text
from .providers.base import CallSpec
from .providers.common import lookup
from .providers.gemini_api import API_VERSIONS, CACHE_API_VERSION, GeminiBatchWriter, api_version_of, parse_response
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
    call = _real_call(route, config, rules, item, text, stamp)
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


# ---------------------------------------------------------------------------- the batch service


# The states in which a batch job has nothing more to do.
_JOB_DONE = frozenset(
    {"JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
)
DEFAULT_WAIT_SECONDS = 300.0
DEFAULT_POLL_SECONDS = 6.0
DEFAULT_CACHE_SECONDS = 600
SAY_WAITING_SECONDS = 30.0


@dataclasses.dataclass
class BatchOutcome:
    """What the batch service did with one variant of the request, sent as a batch of one."""

    key: str  # a stable name, see BATCH_VARIANTS ("bare:<model>" for the bare request on another model)
    title: str
    detail: str  # what differs from the request as the writer sends it
    verdict: str = "undecided"  # "taken", "refused" or "undecided" (nothing seen before the wait ran out)
    state: str | None = None  # the last state of the job that was seen
    error: str | None = None  # the full error of a refusal (or the last error seen while waiting)
    finish: str | None = None
    usage: Usage = dataclasses.field(default_factory=Usage)
    cost_usd: float = 0.0
    job: str | None = None
    seconds: float = 0.0


@dataclasses.dataclass
class BatchProbeReport:
    """The outcomes in the order sent. `refused` is set (and nothing was sent) when the tool refused to start."""

    route: str
    outcomes: list[BatchOutcome] = dataclasses.field(default_factory=list)
    refused: str | None = None
    estimate_usd: float = 0.0

    def verdict(self, key: str) -> str | None:
        return next((outcome.verdict for outcome in self.outcomes if outcome.key == key), None)

    def keys_with(self, verdict: str) -> list[str]:
        return [outcome.key for outcome in self.outcomes if outcome.verdict == verdict]

    @property
    def ok(self) -> bool:
        """True when the batch service took the request as the writer sends it."""
        return self.refused is None and self.verdict("as_sent") == "taken"


# The variants, in the order they are sent: key -> (title, what differs from the request as sent).
BATCH_VARIANTS = {
    "as_sent": ("as the writer sends it", "the request exactly as the batch writer builds it for this route"),
    "no_schema": ("without the schema", "no schema (response_mime_type stays application/json)"),
    "no_system": ("without the system instruction", "no system instruction: the rules are not sent"),
    "no_thinking": ("without the thinking setting", "no thinking_config"),
    "no_metadata": ("without the metadata", "no metadata on the request"),
    "cache_off": ("without the explicit cache", "the rules sent in the request, not by cache name"),
    "cache_on": ("with the explicit cache", "the rules in a cache made for this probe (deleted after), the request naming it"),
    "schema_field": ("schema in response_schema", "the schema in response_schema, the field the batch page shows"),
    "metadata_key": ("metadata key named key", "metadata {key: ...}, the name the batch page uses, not custom_id"),
    "openapi": ("schema in the OpenAPI form", "the schema without additionalProperties, in response_schema; the rest as sent"),
    "documented": ("the documented form", "response_schema (OpenAPI form), metadata key, no explicit cache: what the writer falls back to"),
    "bare": ("bare request", "only the task text and the token limit: no rules, schema, thinking setting, metadata or cache"),
}
# What it means for a run when the request is taken with only this part changed.
_ADVICE = {
    "no_schema": 'the schema is the part the service refuses: set the route option "schema" to "none" (or try "schema_field": "response_schema")',
    "no_system": "the system instruction is the part the service refuses; the rules cannot be left out, so this needs another model or route",
    "no_thinking": 'the thinking setting is the part the service refuses: set the route "effort" to null (no thinking level is sent)',
    "no_metadata": "the metadata is the part the service refuses; the writer matches answers by it, so this needs a change in the writer",
    "cache_off": 'the explicit cache is the part the service refuses: set the route option "cache" to false',
    "cache_on": 'the explicit cache is needed: set the route option "cache" to true',
    "schema_field": 'the field is the part: set the route option "schema_field" to "response_schema"',
    "metadata_key": "the name of the metadata is the part; the documented form of the writer sends it as key",
    "openapi": 'the schema is taken in the OpenAPI form in response_schema: set the route options "schema" to "openapi" and "schema_field" to "response_schema"',
    "documented": 'set the route option "batch_form" to "documented" (the writer steps down to that form by itself after one refused batch)',
}
_SINGLE_PARTS = (
    "no_schema",
    "no_system",
    "no_thinking",
    "no_metadata",
    "cache_off",
    "cache_on",
    "schema_field",
    "metadata_key",
    "openapi",
)
# With the route's cache on, the request as sent has the cache, and so do the variants that change one other part.
_WITH_ROUTE_CACHE = frozenset({"as_sent", "no_schema", "no_thinking", "no_metadata", "schema_field", "metadata_key", "openapi"})


def _enum_name(value: Any) -> str:
    raw = getattr(value, "name", None) or getattr(value, "value", None) or value
    return str(raw).split(".")[-1] if raw is not None else ""


def _item_error_text(error: Any) -> str:
    """The code, the message and the details of a refused batch item, whole."""
    code = lookup(error, "code")
    message = str(lookup(error, "message") or "").strip()
    text = f"{code}: {message}" if code else (message or "the request failed")
    details = [str(item) for item in (lookup(error, "details") or []) if item]
    if details:
        text += " | details: " + "; ".join(details)
    return redact(text)[:ERROR_CHARS]


def _real_call(route: str, config: RouteConfig, rules: WriterRules, item: Any, text: str, stamp: str) -> CallSpec:
    """The call the probes build their request from: the rule pool's task text for a real spec item."""
    task = ids.task_id("probe", stamp, {"001": text})
    return CallSpec(
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


def _job_config(display_name: str) -> Any:
    from google.genai import types

    return types.CreateBatchJobConfig(display_name=display_name)


def _variant(key: str, sent: Any, documented: Any) -> Any:
    """The request of one variant, made from the request as sent and the one in the documented form."""
    from google.genai import types

    def with_config(request: Any, **changes: Any) -> Any:
        return request.model_copy(update={"config": request.config.model_copy(update=changes)})

    if key in ("as_sent", "cache_off", "cache_on"):
        return sent  # the cache, where there is one, is added when the batch is created
    if key == "no_schema":
        return with_config(sent, response_json_schema=None, response_schema=None)
    if key == "no_system":
        return with_config(sent, system_instruction=None)
    if key == "no_thinking":
        return with_config(sent, thinking_config=None)
    if key == "no_metadata":
        return sent.model_copy(update={"metadata": None})
    if key == "schema_field":
        return with_config(sent, response_json_schema=None, response_schema=sent.config.response_json_schema)
    if key == "metadata_key":
        return sent.model_copy(update={"metadata": {"key": (sent.metadata or {}).get("custom_id", "probe")}})
    if key == "openapi":
        return with_config(sent, response_json_schema=None, response_schema=documented.config.response_schema)
    if key == "documented":
        return documented
    if key == "bare":
        return types.InlinedRequest(
            contents=sent.contents, config=types.GenerateContentConfig(max_output_tokens=sent.config.max_output_tokens)
        )
    raise ValueError(f"unknown variant {key!r}")


def _decide(outcome: BatchOutcome, job: Any, model: str, prices: PriceTable, today: date) -> None:
    """Read what the service did with the one item of a finished batch."""
    items = lookup(job, "dest", "inlined_responses") or []
    answer = items[0] if items else None
    error = lookup(answer, "error") if answer is not None else None
    response = lookup(answer, "response") if answer is not None else None
    if error is not None and response is None:
        outcome.verdict = "refused"
        outcome.error = _item_error_text(error)
    elif response is not None:
        result = parse_response("probe", response)
        outcome.verdict = "taken"
        outcome.finish = result.stop_reason or ("ended early: " + (result.error or ""))
        outcome.usage = result.usage
        outcome.cost_usd = round(prices.cost(model, result.usage, batch=True, on=today), 8)
    elif lookup(job, "error") is not None:
        outcome.verdict = "refused"
        outcome.error = "the job failed: " + _item_error_text(lookup(job, "error"))
    else:
        outcome.verdict = "refused"
        outcome.error = f"the job ended ({outcome.state}) without an answer for the item"


def probe_gemini_batch(
    route: str = "flash-batch",
    *,
    item: Any = None,
    keys: Sequence[str] | None = None,
    try_cache: bool = False,
    compare_models: Sequence[str] = (),
    api_version: str | None = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    cap_usd: float = DEFAULT_CAP_USD,
    route_config: RouteConfig | None = None,
    rules: WriterRules | None = None,
    client: Any | None = None,
    prices: PriceTable | None = None,
    printer: Callable[[str], Any] = print,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BatchProbeReport:
    """Send the writer's request to the batch service as batches of one item, whole and with one part changed.

    The one-call endpoint takes a request the batch service may refuse, and the service says no more than a
    code and one sentence per item, after the batch has run. So each variant is a batch of its own, all created
    at once, polled until each has an answer or `wait_seconds` is up, and cancelled when it has not. A refused
    item costs nothing; a taken one is billed at the batch rate, and `cap_usd` bounds what all of them may cost.
    The variants are `BATCH_VARIANTS`, in that order: the request as sent, then with one part left out or
    changed, then the documented form and the bare request; `keys` picks some of them. `try_cache` also tries
    the request with an explicit cache when the route has none (a cache is made for it and deleted after).
    Each name in `compare_models` gets the bare request too, to tell a refusal that comes from the model from
    one that comes from a part of the request (it is priced as the route's model, an upper bound for a cheaper one).
    `api_version` ("v1" or "v1beta"; the route's option, else the SDK's own, when omitted) is the version of the API
    the batches go to; a run that makes a cache uses "v1beta" unless told otherwise, and refuses "v1".
    """

    def refuse(reason: str, **extra: Any) -> BatchProbeReport:
        printer(f"[geminiprobe] refused: {reason}")
        return BatchProbeReport(route=route, refused=reason, **extra)

    unknown = [key for key in (keys or ()) if key not in BATCH_VARIANTS]
    if unknown:
        return refuse(f"the variants to try are some of {list(BATCH_VARIANTS)}, not {unknown}")
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
    call = _real_call(route, config, rules, item, text, stamp)
    today: date = now().astimezone(timezone.utc).date()
    route_cache = bool(config.options.get("cache"))
    if keys:
        wanted = list(dict.fromkeys(keys))
    else:
        wanted = [
            key
            for key in BATCH_VARIANTS
            if key not in ("cache_off", "cache_on") or (key == "cache_off" and route_cache) or (key == "cache_on" and try_cache and not route_cache)
        ]
    makes_cache = route_cache or "cache_on" in wanted
    version = api_version or str(config.options.get("api_version", "auto"))
    if version not in API_VERSIONS:
        return refuse(f"the API version is one of {list(API_VERSIONS)}, not {version!r}")
    if makes_cache and version == "v1":
        return refuse(f"an explicit cache exists only in the {CACHE_API_VERSION} API, and the API version is v1; use {CACHE_API_VERSION}")
    if makes_cache and version == "auto":
        version = CACHE_API_VERSION
    options = {**config.options, "cache_ttl_seconds": DEFAULT_CACHE_SECONDS}
    if version != "auto":
        options["api_version"] = version
    full = GeminiBatchWriter(route, dataclasses.replace(config, options={**options, "batch_form": "full"}), client=client)
    documented = GeminiBatchWriter(
        route, dataclasses.replace(config, options={**options, "cache": False, "batch_form": "documented"}), client=client
    )
    try:
        full.check_rules(rules)
        [sent] = full.prepare([call])
        [shaped] = documented.prepare([call])
    except (ConfigError, ValueError) as error:
        return refuse(f"the request could not be built: {error}")
    acceptance = full.accepting()
    if not acceptance.ok:
        return refuse(f"route {route} is closed: {acceptance.reason}")

    plan: list[tuple[str, str, str, str, Any, bool]] = []  # key, title, detail, model, request, whether it names the cache
    for key in wanted:
        title, detail = BATCH_VARIANTS[key]
        names_cache = key == "cache_on" or (route_cache and key in _WITH_ROUTE_CACHE)
        plan.append((key, title, detail, config.model, _variant(key, sent.payload["request"], shaped.payload["request"]), names_cache))
    for model in compare_models:
        bare = _variant("bare", sent.payload["request"], shaped.payload["request"])
        plan.append((f"bare:{model}", f"bare request on {model}", f"the bare request on the model {model}", model, bare, False))
    estimate = sum(
        prices.estimate(config.model, sent.est_input_tokens, sent.max_output_tokens, batch=True, on=today) for _ in plan
    )
    if estimate > cap_usd + 1e-9:
        return refuse(f"the batches may cost up to ${estimate:.4f}, over the ${cap_usd:.2f} cap", estimate_usd=estimate)

    report = BatchProbeReport(route=route, estimate_usd=estimate)
    service = full._client()
    sent_to = version if version != "auto" else f"{api_version_of(service) or 'the SDK default'} (the SDK default)"
    printer(
        f"[geminiprobe] {route} ({config.model}): {len(plan)} batch(es) of one item, {len(text)} characters of task text, "
        f"{len(rules.system)} of rules; API version {sent_to}; at most ${estimate:.4f} if every one is taken; "
        f"waiting up to {wait_seconds:g} s"
    )
    cache_name: str | None = None
    pending: dict[str, BatchOutcome] = {}
    started: dict[str, float] = {}
    try:
        for index, (key, title, detail, model, request, names_cache) in enumerate(plan, 1):
            outcome = BatchOutcome(key=key, title=title, detail=detail)
            report.outcomes.append(outcome)
            try:
                if names_cache:
                    if cache_name is None:
                        cache_name = full._ensure_cache(config.model, rules.version, rules.system)
                    request = GeminiBatchWriter._with_cache(request, cache_name)
            except Exception as error:  # noqa: BLE001 - whatever the SDK raises is the answer
                outcome.verdict, outcome.state = "refused", "not created"
                outcome.error = "the cache could not be made: " + _error_text(error)
                for line in render_batch_outcome(outcome):
                    printer(line)
                continue
            try:
                job = service.batches.create(model=model, src=[request], config=_job_config(f"rps-probe-{stamp}-{index}"))
            except Exception as error:  # noqa: BLE001
                outcome.verdict, outcome.state = "refused", "not created"
                outcome.error = _error_text(error)
                for line in render_batch_outcome(outcome):
                    printer(line)
                continue
            outcome.job = str(lookup(job, "name") or "") or None
            outcome.state = _enum_name(lookup(job, "state")) or None
            pending[key] = outcome
            started[key] = clock()
        deadline = clock() + wait_seconds
        said = clock()
        while pending:
            for key, outcome in list(pending.items()):
                try:
                    job = service.batches.get(name=outcome.job)
                except Exception as error:  # noqa: BLE001 - asked again at the next round
                    outcome.error = "the last look at the job failed: " + _error_text(error)
                    continue
                outcome.state = _enum_name(lookup(job, "state")) or outcome.state
                if outcome.state in _JOB_DONE:
                    outcome.seconds = round(clock() - started[key], 1)
                    outcome.error = None
                    _decide(outcome, job, config.model, prices, today)
                    del pending[key]
                    for line in render_batch_outcome(outcome):
                        printer(line)
            if not pending or clock() >= deadline:
                break
            if clock() - said >= SAY_WAITING_SECONDS:
                said = clock()
                states = Counter(outcome.state or "unknown" for outcome in pending.values())
                printer(f"[geminiprobe] still waiting for {len(pending)} batch(es): " + ", ".join(f"{count} {state}" for state, count in sorted(states.items())))
            sleep(poll_seconds)
    finally:
        for key, outcome in list(pending.items()):
            outcome.seconds = round(clock() - started.get(key, clock()), 1)
            try:
                service.batches.cancel(name=outcome.job)
            except Exception:  # noqa: BLE001 - a batch that cannot be cancelled ends by itself
                pass
            for line in render_batch_outcome(outcome):
                printer(line)
        if cache_name is not None:
            try:
                service.caches.delete(name=cache_name)
            except Exception:  # noqa: BLE001 - the cache expires by itself within minutes
                pass
        writer_close([("batch", full, None), ("batch", documented, None)])
    for line in render_batch_summary(report):
        printer(line)
    return report


def render_batch_outcome(outcome: BatchOutcome) -> list[str]:
    head = f"[geminiprobe] batch {outcome.title!r}: "
    if outcome.verdict == "refused":
        return [head + f"REFUSED ({outcome.state}, {outcome.seconds:g} s)", f"    {outcome.error}"]
    if outcome.verdict == "taken":
        usage = outcome.usage
        return [
            head
            + f"taken ({outcome.finish}, {outcome.seconds:g} s); tokens in {usage.input_tokens}, out {usage.output_tokens}, "
            f"thinking {usage.reasoning_tokens}; cost ${outcome.cost_usd:.6f}"
        ]
    lines = [
        head
        + f"no answer after {outcome.seconds:g} s (state {outcome.state}); the batch was cancelled. "
        "A refused item comes back within seconds, so nothing was refused: it was probably taken. Wait longer to see."
    ]
    if outcome.error:
        lines.append(f"    {outcome.error}")
    return lines


def render_batch_summary(report: BatchProbeReport) -> list[str]:
    """What the outcomes say, in plain sentences, naming the part the service refuses when they show it."""
    taken, refused = report.keys_with("taken"), report.keys_with("refused")
    as_sent = report.verdict("as_sent")
    if as_sent == "taken":
        return [
            "RESULT: the batch service took the request as the writer sends it. Nothing in the request is refused; "
            "if a run is refused all the same, the cause is outside the request (the model, the key, the time of day)."
        ]
    if as_sent == "undecided":
        return [
            "RESULT: the request as sent was neither taken nor refused within the wait. A refused item comes back within seconds, "
            "so it was probably taken; run again with a longer --wait to see."
        ]
    if as_sent is None:
        return [
            "RESULT: the request as sent was not among the variants tried. "
            f"Taken: {', '.join(taken) or 'none'}. Refused: {', '.join(refused) or 'none'}. The error texts above are the provider's own."
        ]
    lines = ["RESULT: the batch service refused the request as the writer sends it."]
    single = [key for key in _SINGLE_PARTS if key in taken]
    for key in single:
        lines.append(f"  With only this changed it was taken ({BATCH_VARIANTS[key][0]}): {_ADVICE[key]}.")
    if "documented" in taken:
        lines.append(f"  In the documented form it was taken{' too' if single else ''}: {_ADVICE['documented']}.")
    if "bare" in taken and not single and "documented" not in taken:
        lines.append(
            "  The bare request was taken but changing one part did not help: two or more parts are refused together. "
            "Read the errors above; the variants can be sent again one at a time (--only)."
        )
    if "bare" in refused:
        elsewhere = [key.split(":", 1)[1] for key in taken if key.startswith("bare:")]
        lines.append(
            "  Even the bare request was refused: no part of the request is at fault. The batch service does not take this model "
            "(or this key) for inline batches."
            + (f" It took the bare request on {', '.join(elsewhere)}." if elsewhere else "")
            + " Use the one-call route (flash-sync) or another model for now."
        )
    if len(lines) == 1:
        lines.append("  No variant that could name the cause was taken: read the error texts above, which are the provider's own.")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rps.geminiprobe",
        description="Try the writer's Gemini request: in each form of the row schema as one call, or with --batch as batches of one item.",
    )
    parser.add_argument("--route", default="flash-batch", help="a Gemini route: flash-batch or flash-sync")
    parser.add_argument("--all", action="store_true", help="try every form of the schema even after one is taken")
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--cap", type=float, default=DEFAULT_CAP_USD, help="refuse to start when the calls may cost more, in dollars")
    parser.add_argument("--batch", action="store_true", help="send the request to the batch service, whole and with one part changed")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_SECONDS, help="with --batch: seconds to wait for the answers")
    parser.add_argument("--only", default=None, help=f"with --batch: some of the variants, comma-separated: {','.join(BATCH_VARIANTS)}")
    parser.add_argument("--with-cache", action="store_true", help="with --batch: also try an explicit cache (one is made and deleted)")
    parser.add_argument("--compare-model", action="append", default=[], help="with --batch: also send the bare request to this model (repeatable)")
    parser.add_argument(
        "--api-version",
        choices=("v1", "v1beta"),
        default=None,
        help="with --batch: the version of the Gemini API the batches go to (default: the route's option, else the SDK's own)",
    )
    args = parser.parse_args(argv)
    if args.api_version and not args.batch:
        parser.error("--api-version goes with --batch")
    if args.batch:
        keys = [part.strip() for part in args.only.split(",") if part.strip()] if args.only else None
        batch_report = probe_gemini_batch(
            args.route,
            keys=keys,
            try_cache=args.with_cache,
            compare_models=args.compare_model,
            api_version=args.api_version,
            wait_seconds=args.wait,
            max_output_tokens=args.max_output_tokens,
            cap_usd=args.cap,
        )
        if batch_report.refused:
            return 2
        return 0 if batch_report.ok else 1
    report = probe_gemini(args.route, stop_at_first=not args.all, max_output_tokens=args.max_output_tokens, cap_usd=args.cap)
    if report.refused:
        return 2
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
