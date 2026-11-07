# row-processing-system

A controller that writes dataset rows through model APIs, checks them, hands them to review
agents, collects their verdicts and ships the accepted rows to a private Hugging Face dataset
repository. It runs on a Colab CPU runtime (and locally for tests) and can stop at any moment
(runtime lost, Ctrl+C, a stop file) and resume exactly where it left off, without repeating a
call or a row.

Version 0.2.2. It has real writers for Anthropic (Message Batches), OpenAI (one call at a time;
the Batch API adapter is in the package, but this project does not use it), Gemini (inline
batches) and DeepSeek (one call at a time, off-peak), a one-call route for each of the first
three, the compiled rule pool and its assessor (`rps.rules`, `rps.assessor`), and a dry-run tool
for trying one route with a handful of calls. Everything is tested offline: with a fake writer,
and with each adapter against the SDKs' own types built from recorded-response-shaped data.

Licensed under the Apache License, Version 2.0 (see `LICENSE`).

## What changed in 0.2.2

| Change | What happens now |
|---|---|
| GPT goes one call at a time | The route for GPT is `luna-sync` (OpenAI `gpt-6-luna`, route kind `api`): GPT calls go one at a time, never as a batch. The dry-run tool, the notebook and this document start from `luna-sync`. The OpenAI Batch API adapter and the `luna-batch` route are still in the package, unused |
| Production-size runs of one-call routes | A one-call route keeps up to `workers` calls in flight, 32 unless the route says otherwise (cap 2,500). The controller feeds it a window at a time, four calls per worker, so a list of hundreds of thousands of lines is never all in memory or all held in the budget's reserve, and a stop or crash loses at most one window. See "One-call routes at production size" |
| Rate limits make a route wait | A call the provider's client could not get past a rate limit (HTTP 429) did not run and was not billed, so it no longer costs an attempt: it waits and goes again. Five such answers in a row, with no call answered between them, pause the whole route for a minute (twice as long each time in a row, up to 15 minutes). While it is paused the route is "closed for now", the run waits and nothing is stopped; the route opens again by itself |
| Tasks from the state store | `tasks_file` may be `store:control/tasks.json` (or another state path). The controller reads the file through the state store on every pass, so tasks can be added from outside while a run goes on; a missing file means no tasks, and a file with a mistake is reported once while the run goes on with the tasks it already has. `rps.runtime.build_operator_store(config)` gives the writer's view of the store for putting the file there. A store behind a network is read at most every 15 seconds |
| A folder inside the bucket | `bucket` may be `owner/name/folder`. Every state path (events, packets and pictures, verdicts, the hand files, the stop file and the tasks file) then lives under that folder, for the controller, the operator store and the reviewer commands alike. Test runs and production use two folders of one bucket, `owner/name/test` and `owner/name/prod`, and share no file. A bucket with no folder keeps the layout at its top |
| Notebook | The install line names `v0.2.2`; the example real route is `luna-sync`; the tasks cell puts the tasks file into the state store; the bucket setting shows the folder form |

## What changed in 0.2.1

| Change | What happens now |
|---|---|
| Anthropic batches found again after a crash | A batch the provider took just before a crash is found by its size and creation time and adopted instead of being sent twice. A batch that turns out to hold none of the group's calls is found out when it ends, and the calls are sent again (failure kind `batch_mismatch`). The same search runs after a submit that failed on the way back from the provider, in the same run. See "Anthropic" below |
| Cache storage in the ledger | With Gemini's `cache: true`, the hours a cache is kept are charged: cached tokens times hours times the storage rate per million tokens per hour. It is a `cache_storage` event and a line of its own in `ledger/summary.json`, under the route and purpose, and it counts against the caps. The Gemini cache read rates in the price table are now the page's own |
| Long prompts | A call whose estimated prompt passes the provider's long-prompt threshold (Anthropic Haiku 5.5: 100,000 tokens; OpenAI Luna and Sol: 272,000) is refused before it is sent, as failure kind `long_prompt`. The thresholds are in `prices.json` |
| Packets carry the rules version | When rules are loaded (the rule pool, say) a packet's `brief_version` is their version, the one in `src/rps/rules/VERSION`, for reviewers to compare. `review.brief_version` applies only when no rules are loaded |
| Notebook | After the install cell Colab may ask to restart the session; the notebook now says to restart and go on from cell 2 |

## How a row travels

| Step | What happens | Recorded as |
|---|---|---|
| plan | Task definitions (a batch of spec lines for one source and route) are read from the tasks file | `task_created` |
| prepare | Each spec line becomes a call; a picture is checked against the routing table; the estimated cost is checked against the route and purpose budgets | `call_prepared`, or `budget_refused` |
| submit | Calls go to the provider as a batch or one by one; the provider's batch id is made durable before anything waits on it | `batch_submitted` |
| poll, collect | Finished batches (or finished calls) are read; the rules turn each answer into rows; every row is stamped with its writer label and source label | `call_returned`, `call_failed` |
| assess | The rule set's checker (or a function you give) checks the rows | `row_assessed`, `row_rejected`, `row_set_aside` |
| review | Rows are handed to reviewers in blind packets; verdicts come back and settle the rows | `packet_published`, `verdict_received`, `row_escalated`, `row_to_hand`, `row_accepted` |
| ship | Accepted rows and the ledger are written to the dataset repository | `shipped` |

The events are one JSON object per line (`ts`, `actor`, `kind`, `payload`). The controller's
in-memory state is nothing but a replay of those events, so any run can be rebuilt from the
files alone.

## Layout of the repository

    pyproject.toml, requirements.txt   package metadata and exact pins
    src/rps/
      config.py        Config: routes, budgets, sources, rules, review policy, commit batching
      ids.py           deterministic task, call, row and packet ids
      events.py        the append-only event log and tolerant replay
      state.py         the state rebuilt from events, and the queries the controller asks
      layout.py        every path in the stores, and who may write it
      store/           Store interface; local folder, Hub dataset repo, Hub storage bucket
      provenance.py    writer and source labels stamped on every row
      ledger.py        usage records, the price table, per-route and per-purpose budgets
      routing.py       which pictures may go to which provider (one table)
      failures.py      the kinds of failed call and what each one means
      prompting.py     the writing rules as inputs, and the text of one call
      providers/       the Writer interface, FakeWriter, the worker pool, the four adapters
      routes.py        the standard routes (haiku-batch, luna-sync, flash-batch, deepseek-sync, ...)
      dryrun.py        try one real route with a handful of calls
      assess.py        the assessment hooks
      rules/           the compiled rule pool: writer prompt, row schema, review brief, examples
      assessor/        the assessor for rows written under that pool
      review/          packets, verdict format, review policy, reviewer client and its CLI
      controller.py    the loop
      runtime.py       build the stores, writers and controller from a Config
      keys.py          API keys from environment variables only; Colab secret mapping
      images.py        shrink a picture to a source's image size
      tasks.py         task definitions and the tasks file (a local file, or one in the state store)
      data/prices.json the dated price table
    notebooks/controller_colab.ipynb   run the controller on a Colab CPU runtime
    reviewers/                         the review agents' guide (AGENTS.md; CLAUDE.md imports it) and TASK.md
    tools/                             helper scripts (the assessor's parity check)
    tests/                             offline tests (no network, no keys)

## Install and test

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt     (Windows; use .venv/bin/python elsewhere)
    .venv\Scripts\python.exe -m pytest -q

`requirements.txt` holds the exact versions the tests ran against. `pyproject.toml` declares
looser bounds for `pip install "git+https://github.com/AbstractEyes/row-processing-system@v0.2.2"`
(it also installs `thought-image-seeds`, which the assessor uses as a library). The tests never
touch the network: a fixture refuses socket connections and empties `HF_TOKEN` and the provider keys.

## Running it

In Python (a script, a notebook cell or a Colab cell):

```python
from rps.config import Config
from rps.runtime import build_controller

config = Config.load("config.json")            # or Config.from_dict({...})
controller = build_controller(config, create=True)   # creates the bucket and repo, private, if missing
summary = controller.run(time_budget_seconds=3600)
print(summary.reason)                           # idle, time_budget, stop_file, interrupt, ...
print(controller.format_status())
```

`run` prints progress as job size, lines done, time spent and time left, and ends when nothing
is left to do, the time budget is used up, a stop file appears, or you interrupt it. Interrupting
is safe. Run it again and it resumes. When a run ends (other than by an interrupt) it takes in
whatever calls have already finished, then tells each writer to stop.

On Colab, open `notebooks/controller_colab.ipynb`. It maps the Colab secrets `CLAUDE_API`,
`OPENAI_API`, `GEMINI_API`, `DEEPSEEK_API` and `HF_TOKEN` to environment variables (printing only
the secret names), can dry-run one real route, loads a configuration, runs the controller for a
time budget and prints the status. As shipped it is a dry run with a pretend provider on the
runtime's disk.

### Stopping

Create the file named by `stop_file` (default `STOP` inside the work folder) or, from another
machine, the file `control/STOP` in the state store (under the bucket's folder when `bucket` names
one, so a stop file in the `test` folder stops only the run that uses that folder). The run finishes
the step it is in and ends cleanly. Remove the file to run again.

### The tasks file

A JSON list. Each task is a batch of spec lines for one source, written through one route for one
purpose:

```json
[
  {
    "source": "photos", "batch": "batch-0001", "route": "luna-sync", "purpose": "production",
    "lines": [
      {"line_id": "001", "spec": "what the writer is asked to do", "item_id": "img-001",
       "image": "img-001.jpg", "image_class": "coco"}
    ]
  }
]
```

The file may grow between passes. A task already recorded is ignored. The same lines through another
route are a different task when given a `variant` (use the route name). With the rule pool, a
line's `spec` is the specs-file item (kind, series, ids, domain, genre, specs or spec, seed id,
hint, license) and the controller turns it into the user text of the call.

**Where the file lives.** `tasks_file` is a local path, or `store:<path>` for a file in the state
store, such as `store:control/tasks.json`. A file in the state store is read through the store on
every pass (at most every 15 seconds when the store is a bucket), so tasks can be added from outside
while a run goes on. A missing file in the state store means no tasks (a missing local file is
reported as a problem). A file that cannot be read, or a task that cannot be added (an unknown
route, say), is reported once and the run goes on with what it already has.
Only the operator writes under `control/`; `build_operator_store` gives that view of the store:

```python
import json
from rps.runtime import build_operator_store

operator = build_operator_store(config)            # the state store, as the operator sees it
operator.write_text("control/tasks.json", json.dumps(tasks))
operator.flush(force=True)                         # a bucket holds writes back until this
```

With a bucket folder (`owner/name/test`, say) the file is `test/control/tasks.json` in the bucket;
the operator store, the controller and the reviewer commands all take the folder from the same
`bucket` setting.

## Configuration

`Config` is loaded from a JSON file or a dict. It holds no keys, and a value that looks like a key
is refused. Unknown keys are refused too.

| Key | Meaning |
|---|---|
| `bucket` | The storage bucket for event logs, review packets, pictures and verdicts, written `owner/name`. It may carry a folder, `owner/name/folder` (one or more plain names after the bucket id, none of them named like a directory of the store: `events`, `packets`, `review`, `accepted`, `ledger`, `hand`, `control`). Every state path then lives under that folder, for the controller, the operator store and the reviewer commands alike, and nothing outside it is listed, read, written or deleted. Use `owner/name/test` for test runs and `owner/name/prod` for production and they share no file. The bucket is created (private) when missing; a folder needs no creating. The reviewer commands read the same setting from their config file, so it must name the same folder as the run. When absent, the work folder is used |
| `data_repo` | The dataset repository for accepted rows and the ledger. When absent, the work folder is used |
| `work_dir` | Local folder for dry runs, the stop file and the default pictures folder |
| `routes` | One entry per way of getting rows written, see below |
| `sources` | Per source: `image_size`, `license`, `dataset`, `images_dir`, `image_class`. A spec line with no license (its own or its source's) is refused |
| `rules` | The writing rules, see "The writing rules are inputs" |
| `commit` | How writes are batched, see below |
| `review` | Reviewers, families, packet size, brief version and the review policy. `brief_version` applies only when no writing rules are loaded; with rules, a packet carries their version |
| `price_table` | Path to a price table file (default: the dated table in the package) |
| `time_budget_seconds`, `poll_seconds`, `max_attempts` | Run limits (an attempt past the first is a rebound) |
| `stop_file`, `remote_stop_path` | Where a stop request is looked for |
| `wait_for_review` | Keep a run going, polling for verdicts, while any packet is out with a reviewer (default: a run ends when the controller has nothing left to do itself) |
| `tasks_file` | The tasks file the planner reads on every pass: a local path, or `store:<path>` for a file in the state store (for example `store:control/tasks.json`, under the bucket's folder when there is one). A missing file in the state store means no tasks. See "The tasks file" |

A route has these keys:

| Key | Meaning |
|---|---|
| `provider`, `model` | `anthropic`, `openai`, `gemini` or `deepseek` (or `fake` for tests), and the provider's model name |
| `kind` | `batch` (the provider's batch service) or `api` (one call at a time; this is how GPT is used here). `claude-code`, `codex` and `gemini-cli` are accepted by the configuration but have no adapter yet |
| `effort` | The provider's reasoning or thinking level (Anthropic `low` to `max`, OpenAI's `reasoning.effort`, Gemini `minimal`, `low`, `medium`, `high`); left out, the provider's default applies |
| `budget_usd`, `purpose_caps` | The route total and one cap per purpose |
| `max_output_tokens` | The token limit of a call (a call cut off at it is retried once with a larger limit, see `truncation_factor`) |
| `max_batch_calls` | The most calls in one provider batch (each adapter also splits by its provider's size limits) |
| `enabled` | `true` or `false`. Left out, a route is on, except DeepSeek, which stays off until a configuration says `true` |
| `max_retries`, `timeout_seconds` | Passed to the provider's client, whose own retries handle rate limits and server errors |
| `workers` | How many calls a one-call route (`kind` `api`) keeps in flight: 32 unless set, cap 2,500; a batch route ignores it. The controller prepares calls for the route four per worker at a time. See "One-call routes at production size" |
| `truncation_factor` | How much larger the token limit is on the one retry that follows a cut-off answer (default 2) |
| `options` | Settings of one provider (numbers, text or true/false), listed with each adapter; an unknown option is refused |

### Stores and who writes what

A run uses two stores. The state store holds what changes often and what must really disappear
(pictures); the ship store holds what is final.

| Path | Writer | Store |
|---|---|---|
| `events/controller-<run>.<part>.jsonl` | the controller run named in the path | state (bucket) |
| `packets/<reviewer>/<packet>/packet.json`, `images/` | the controller | state |
| `review/<reviewer>/<packet>.jsonl` | the reviewer named in the path | state |
| `hand/queue.jsonl` | the controller (rows waiting for the owner) | state |
| `control/STOP`, `control/hand/*.jsonl`, `control/tasks.json` (when `tasks_file` is `store:control/tasks.json`) | the operator | state |
| `accepted/<route>/<task>.jsonl` | the controller | ship (dataset repo) |
| `ledger/summary.json`, `ledger/tasks.jsonl` | the controller | ship |

Every file has exactly one writer. The stores wrap every write in a check of this table, so a
reviewer cannot write into the controller's files or another reviewer's, and each run gets its own
event files. Run one controller at a time per state store.

The paths above are the same whatever the bucket setting; with a folder (`bucket` as
`owner/name/folder`) the bucket holds them under `folder/`, so `events/...` is `folder/events/...`
in the bucket and two folders of one bucket are two separate state stores. A store's paths cannot
lead out of its folder (a path with `..` is refused), and a listing is asked of the bucket under the
folder only.

The dataset repository store is deliberately slow: at most one commit per `ship_seconds_per_commit`
(default 60 seconds), under 100 files per commit (default 99), at most 300 commit attempts an hour
(the Hub's limit is about 320, failed attempts included), with exponential backoff and `Retry-After`
on HTTP 429. Each commit passes the head it last saw, and a commit by another controller is
reported rather than overwritten. The bucket store batches by count and by age. It is not
transactional, so a failed operation is retried on its own; every operation is an overwrite or a
delete, so repeating one is harmless.

## Routes and adapters

| Route | Provider and model | Path | Pictures |
|---|---|---|---|
| `haiku-batch` | Anthropic `claude-haiku-5-5` | Message Batches | never |
| `luna-sync` | OpenAI `gpt-6-luna` | `/v1/responses`, one call at a time (route kind `api`): how GPT is used here, at any size | public common, COCO, FFHQ |
| `luna-batch` | OpenAI `gpt-6-luna` | Batch API on `/v1/responses` (in the package; this project does not use it) | public common, COCO, FFHQ |
| `flash-batch` | Gemini `gemini-3.8-flash` | batch mode, inline requests | public common, COCO, FFHQ |
| `deepseek-sync` | DeepSeek `deepseek-flash` | chat completions, one call at a time, off-peak | public common, COCO, FFHQ |
| `haiku-sync`, `flash-sync` | as above | one call at a time (route kind `api`), for small tests | as above |

`rps.routes.standard_route(name)` gives the route configuration for any of these. Every adapter
reads its key from its environment variable only and never prints it; a route whose key is missing
(or that is switched off) is closed for the run: the controller leaves its calls alone, says why
once, and ends idle when nothing else can be done. A route that is closed only for a while (DeepSeek
in its peak hours) keeps the run waiting. A request the provider calls malformed (HTTP 400) or a
key it rejects (401, 403, a spent balance) stops that route for the rest of the run
(`route_halted`); the next run starts with the route open again.

All four adapters send the same things: the writing rules as the system text, the call's task
text, the row schema as the answer format where the provider has one, and a picture only where the
routing table allows it. None sends `temperature`, `top_p`, `top_k`, a prefill or a forced tool.

### One-call routes at production size (`luna-sync` and the other `api` routes)

A route of kind `api` sends one call at a time from a pool of worker threads, each result recorded
as it lands. GPT is used this way here.

| Part | What happens |
|---|---|
| Calls at once | `workers` calls are in flight at once: 32 unless the route says otherwise, at most 2,500 |
| Why 32 | The entry tier of `gpt-6-luna` allows 5,000 requests and 2,000,000 tokens a minute. A call of the rule pool counts at most about 12,000 tokens against that (its prompt plus the output limit it asks for), so the token limit binds first, at about 160 calls a minute. If a call takes 20 to 40 seconds (an assumption), 32 workers send 50 to 100 calls a minute and leave room for retries. It is an estimate: the `minutes` of each call in the ledger shows the real time. Raise `workers` for a higher tier or for slower calls; lower it if the route keeps pausing |
| The window | The controller prepares calls for such a route four per worker at a time (128 at 32 workers) and refills when a quarter of that is free. A list of hundreds of thousands of lines is never all in memory or all held in the budget's reserve, and a stop or crash loses at most one window, whose calls come back as rebounds |
| A rate limit (HTTP 429) | The provider's own client retries first (`max_retries`, default 2), waiting as the provider asks. A call still turned away after that did not run and was not billed, so it is not a failed attempt: the worker waits and sends it again |
| A run of rate limits | Five turned-away calls in a row, with no call answered between them, pause the whole route for 60 seconds. A pause that follows at once is twice as long, up to 15 minutes; one answered call starts over. While it is paused the route is "closed for now": nothing more is prepared for it, the run waits (the run is never stopped for this) and the route opens by itself, each worker starting again a few seconds apart so they do not meet the limit together. Calls already on the wire when a pause began do not count towards the next one |
| One call that never gets through | A call turned away 8 times is handed up as a failed call (`provider_error`) and comes back as a rebound, so one call cannot hold the route for ever |

### Anthropic (`haiku-batch`, `haiku-sync`)

| Part | Form |
|---|---|
| Request | `model`, `max_tokens`, the rules as one `system` block marked for a cache (`cache_control` `ephemeral`, one hour), one user message of text, `output_config` with the row schema (`json_schema`) and the effort |
| Text only | A call that carries a picture is refused, whatever its class |
| Batch | `messages.batches.create`; results from `messages.batches.results`, matched by `custom_id` in any order. At most 100,000 requests and 240 MB per batch (the limit is 256 MB). A batch can expire after 24 hours; its unfinished calls come back as `expired` and are sent again as rebounds |
| Answer | `end_turn` (and `stop_sequence`) is parsed; `max_tokens` is a truncation; `refusal` is recorded and not retried on the route; any other stop is a failure that is not retried |
| Usage | input, cache write and cache read tokens as given; output minus thinking tokens, thinking apart |
| After a crash | A batch cannot be searched for what it holds, so one the provider took just before the controller recorded its id is found by size and time. The provider's list of batches (newest first) is read back to the moment the calls were prepared, less 2 minutes for the difference between the two clocks (at most 1,000 batches), and the newest batch that no group has recorded whose request counts add up to the group's size is adopted; the last group of a split goes first, since the groups were sent in order. The adopted batch is marked in its handle. A stranger's batch of the same size can be taken by mistake, and it is found out only when it ends: if its results hold none of the group's calls, those calls fail as `batch_mismatch` (retryable, not billed) and are sent again, and the stranger's batch stays recorded so it is never taken twice. With no candidate the calls are sent again |
| Options | `cache_ttl` (`5m` or `1h`, default `1h`) |

### OpenAI (`luna-sync`; the batch route `luna-batch` is in the package and not used here)

| Part | Form |
|---|---|
| Request | A `/v1/responses` body: `instructions` (the rules), one user message of `input_text` and, where allowed, `input_image` (a data URL with `detail`), a strict JSON schema as the text format, `reasoning.effort`, `max_output_tokens`, `store: false`, and the rules version as `prompt_cache_key` |
| One call at a time | `luna-sync` sends each call with `responses.create` from a pool of workers (see "One-call routes at production size"). The answer, the usage and the failures are read as for a batch result. A rate-limited call (HTTP 429, or a response that failed with the code `rate_limit_exceeded`) is sent again after the wait, not recorded as a failed attempt |
| Batch (`luna-batch`) | A JSONL file uploaded with `purpose="batch"` and an expiry, then `batches.create`. Up to 50,000 lines and 190 MB per file (the schema's property order is kept in the file, because it shapes the answer) |
| Results | The output file and the error file, matched by `custom_id`; a request that expired appears in the error file as `batch_expired` and is sent again as a rebound; the finished requests of an expired batch are billed and returned |
| Clean-up | After the results are durably recorded, the input, output and error files are deleted (the input holds the pictures); the expiry is the backstop |
| Answer | `completed` is parsed; `incomplete` for `max_output_tokens` is a truncation, for a content filter a block; a `refusal` part is recorded and not retried; a failed response is retried unless the prompt itself was refused |
| Usage | input minus cached and cache-write tokens, cache write, cache read, output minus reasoning, reasoning |
| After a crash | The batch carries a digest of its calls in its metadata, so a batch created just before a crash is found again |
| Options | `detail` (`low`, `high`, `original`, `auto`; default `auto`), `file_expiry_days` (default 7) |

### Gemini (`flash-batch`, `flash-sync`)

| Part | Form |
|---|---|
| Request | One `InlinedRequest` per call: the user turn (text and, where allowed, the picture as bytes), `response_mime_type` JSON with `response_json_schema`, `thinking_level` from the effort, `media_resolution` for a picture, `max_output_tokens`, the rules as `system_instruction`, and `metadata.custom_id` |
| Batch | `batches.create` with inline requests, split under 19 MB per job (the limit is 20 MB); jobs that stay pending or running past 48 hours expire and their calls come back as rebounds |
| Results | `dest.inlined_responses`, matched by the `custom_id` in each response's metadata |
| Answer | `STOP` is parsed; `MAX_TOKENS` is a truncation; the filter stops (`SAFETY`, `PROHIBITED_CONTENT`, `SPII`, `RECITATION`, `BLOCKLIST`, the picture ones) and a blocked prompt are recorded and not retried; thought parts are left out of the text |
| Usage | the prompt count minus cached tokens, cached tokens, candidates' tokens, thinking tokens (billed as output) |
| The explicit cache | With `cache: true` (batches only), the rules are cached once per model and rules version and the batch's requests refer to the cache by name instead of repeating the rules. The cache name is recorded (`cache_created`) so a restart reuses it, and its life is extended while a batch that uses it is running. A failure to create the cache is an error, not a silent fallback. The storage is charged to the ledger: each note carries the cache's size in tokens (the provider's report, or a generous estimate marked `tokens_estimated` when it gives none) and the stretch of the cache's life it covers, and the controller writes a `cache_storage` event for it, tokens times hours times the storage rate per million tokens per hour (`cache_storage` in the price table: $0.50 through 2026-12-31, $1.00 from 2027-01-01). A new cache is charged from its creation to its expiry and one kept alive longer from its earlier expiry to the new one, since the provider bills the time-to-live. The charge goes to the purpose most of the calls that use the cache carry and counts against that purpose's cap and the route total. A route with `cache: true` refuses to start unless the price table has a `cache_storage` rate for its model. The one-time cost of making the cache is not stated on the provider's page and is not counted |
| After a crash | The batch's display name carries a digest of its calls, so a batch created just before a crash is found again |
| Options | `media_resolution` (`low`, `medium`, `high`; default `medium`), `cache` (default false), `cache_ttl_seconds` (default 21600) |

### DeepSeek (`deepseek-sync`)

| Part | Form |
|---|---|
| Request | A chat completion in JSON mode (`response_format` `json_object`): the rules as the system message (JSON mode needs the word "json", which is checked before a run, and an example row in them), the task text and, where allowed, a picture as an `image_url` in the user message |
| Sending | There is no batch service: a pool of workers (the route's `workers`) sends one call each; every result is recorded as it lands, so a stop loses only the calls in flight, which a restart sends again as rebounds |
| Off-peak | Calls go out only outside Monday to Friday 01:00 to 04:00 and 06:00 to 10:00 UTC. In those hours the route is closed for now, calls already queued wait at the edge, and the run keeps waiting. Every result is marked `peak` or `offpeak` by the time it was sent, and the ledger prices it by that tier |
| Answer | `stop` is parsed (blank content is a failed parse, sent again once); `length` is a truncation; `content_filter` is a block; "too busy" is retried |
| Usage | cache hit tokens as cache read, cache miss tokens as input, completion minus reasoning as output |
| Switched off | The route is off unless the configuration says `enabled: true`. The key is `DEEPSEEK_API_KEY` |
| Options | `detail` (`low` or `high`; default `low`), `offpeak_only` (default true; false turns the gate off, for a small test) |

## Which pictures go where

One table decides what an adapter may see. The class of a picture is the class of the picture's
own origin (a narration written for a COCO picture is COCO class).

| Class | Claude | OpenAI | Gemini | DeepSeek |
|---|---|---|---|---|
| `public_common` | no | yes | yes | yes |
| `coco` | no | yes | yes | yes |
| `ffhq` | no | yes | yes | yes |
| `deepfashion` | no | no | no | no |
| `hico` | no | no | no | no |
| `synthetic` (no picture) | no | no | no | no |

A source names its class (`sources.<name>.image_class`) and a spec line can override it. A task
whose pictures may not go to its route is refused when it is added, before anything is recorded or
paid for; the check is made again when each call is built and once more inside each adapter, so no
picture reaches a provider by another path. A class that is not in the table, a route that is not
recognized, and a picture with no class are all refused. Text is always allowed.

Review packets follow the same table, by the reviewer's model family (`review.reviewers[].family`:
`anthropic`, `openai`, `google`, `deepseek`). A row whose picture the reviewer's family may not see
goes into the packet without it and with `image_withheld: true`, and the reviewer judges it from its
text; a family the table does not know gets no pictures at all.

## The writing rules are inputs

The system prompt, the row schema and a version are never part of an adapter. Name them in the
configuration, with two files or a package:

```json
"rules": {"package": "rps.rules"}
"rules": {"system_file": "writer_system.md", "schema_file": "row_schema.json", "version": "2026-11-07a"}
```

or pass `rules=make_rules(system, schema, version)` to `build_controller`. The version is recorded
on every row (`writer.rules_version`), is the key of the providers' prompt caches and is the
`brief_version` of every review packet (a reviewer compares it with the version in
`src/rps/rules/VERSION`); left out it is a hash of the two files, so changed rules always have a new
version. A route that sends the rules
to a model refuses to start without them, and each switched-on route checks the rules before the
run starts (a schema the provider would reject fails there, not on every request).

A rule set may also bring:

| Part | Used for |
|---|---|
| `task_text(spec, ordinal[, choices])` | the user text of a call, from the spec line and its place in the batch |
| `assign(spec, ordinal)` | the runner's per-call choices (which part of a relation to ask about, which unknown-question keys, which wordings) |
| `normalize(text, spec, model)` | turns the writer's answer into the JSON list of rows; applied once, whatever the provider |
| an assessor | checks the rows (see "Plugging in") |

With the package `rps.rules` all four come from the rule pool and the assessor is `rps.assessor`.
`rules.assessor` (`"module:name"`, or `"none"`) names a different one.

## Failures

Every failed call is recorded with a kind (`call_failed.kind`). A call that may be retried is sent
again while the line's attempts are under `max_attempts` (default 2, so one retry):

| Kind | Meaning | What happens |
|---|---|---|
| `truncated` | the answer stopped at the token limit | retried with the limit times `truncation_factor`, with no advice added |
| `invalid_output` | blank, cut off or not the expected JSON | retried with the problem as advice |
| `provider_error` | rate limit, server error, timeout, dropped connection, a batch that failed | retried as a rebound. On a one-call route a call still rate-limited after the client's own retries is not recorded here: it waits and goes again (only one turned away 8 times in a row is) |
| `expired`, `canceled` | the provider let the request lapse or canceled it | retried as a rebound |
| `refused`, `blocked` | the model declined, or a safety filter stopped it | recorded; not retried on the same route |
| `bad_request` | the request is malformed (HTTP 400, a schema the provider rejects) | not retried; stops the route for the run |
| `auth` | the key is rejected or the balance is spent | not retried; stops the route for the run |
| `unexpected` | a stop reason nothing here expects | not retried (an error line an adapter cannot classify is retried as a rebound) |
| `long_prompt` | the call's estimated prompt is over the provider's long-prompt threshold (`long_prompt_tokens` in the price table), where higher rates apply that the table does not model | refused before it is sent, recorded as failed with no cost; not retried |
| `batch_mismatch` | a batch taken up again after a crash held none of the group's calls | retried as a rebound; not billed |

The provider's own client already retries rate limits and server errors a few times
(`max_retries`) with backoff; what comes back after that is recorded as above, except that a
one-call route waits out a rate limit instead (see "One-call routes at production size"). A call that
failed after the provider billed for it (a cut-off or unusable answer) still has its usage recorded
and priced.

## Budgets

Every call has a purpose: `test`, `pricing`, `probe`, `rebound` or `production`. A route has one
dollar cap per purpose plus a total. Before a call is submitted its estimated cost (every input
token at the highest input-side rate, output up to its limit, a route with price tiers at its
dearest tier) must fit under both its (route, purpose) cap and the route total, counting what is
already spent and what calls in flight may still cost. A call that does not fit is refused, never
sent, and recorded as `budget_refused` with the numbers:

    {"route": "luna-sync", "purpose": "pricing", "est_usd": 0.002, "reason": "purpose_cap",
     "purpose_spent": 0.004, "purpose_cap": 0.004, "route_spent": 0.004, "route_cap": 5.0, ...}

A retry or re-submission after a failed or rejected call is always `rebound`, whatever the task's
purpose was, and draws only from the `rebound` cap. A purpose with no cap counts as a $0 cap, so its
calls are refused. A refused call is left alone for the rest of the run and asked again by the next
one, so raising a cap takes effect on the next run. `controller.format_status()` prints spend per
(route, purpose) against its cap and the state of each route, and `ledger/summary.json` and
`ledger/tasks.jsonl` record the same with token counts, attempts and minutes. A provider-side cache
that is billed for the time it is kept (Gemini's, with `cache: true`) adds lines of its own: every
`cache_storage` charge is listed under its route in `ledger/summary.json` (`storage`, with the purpose,
cache name, tokens, hours, rate and dollars of each) and the route and each purpose show their
`storage_usd`. Storage is part of `spent_usd`, so it counts against the caps like any other spend,
and `format_status()` adds a line for it under the table.

### Prices

`data/prices.json` carries its own date, and every model's entry names its source page and the date
it was checked. A model has a synchronous rate card and, where the provider publishes one, a batch
card (without one, the table's batch discount applies). A model can have rates that change on a
date (`until`, then another card) and rates that depend on a tier (DeepSeek's `peak` and `offpeak`
hours). Where a provider's pages do not say whether the batch discount and a cache price stack, the
table carries the higher rate until the pricing test settles it. Cache rates left out are taken as
the plain input rate. Each usage record keeps the date of the table
it was priced with, and the controller notes a table that is old or a price that is about to change.

Two prices sit beside the token rates:

| Key | Meaning | In the packaged table |
|---|---|---|
| `cache_storage` (in a rate card) | Dollars per million cached tokens per hour, for a provider that keeps a cache for you and bills the time it is kept. A batch discount does not apply to it. It follows the card's dates like the other rates | `gemini-3.8-flash`: 0.50 through 2026-12-31, 1.00 from 2027-01-01, on the sync and the batch card (read from the page on 2026-11-07); no other model has one |
| `long_prompt_tokens` (in a model entry) | Input tokens above which the provider bills higher long-prompt rates that the table does not model. A call whose estimated input passes it is refused before it is sent (`long_prompt`) | `claude-haiku-5-5`: 100,000 (cache reads and writes count); `gpt-6-luna` and `gpt-6.1-sol`: 272,000 input tokens. The pages list none for Sonnet 5.5, Gemini 3.8 Flash or DeepSeek |

## Labels

The runner stamps two labels on every row; a model's own value under a reserved key is dropped (and
the row is flagged for the reviewers):

- `writer`: provider, model, route, effort, date, task id, pool version, the size of the picture the
  writer saw, and the rules version.
- `source_label`: dataset, item id (seed or picture) and license.

A row the owner writes or rewrites by hand carries `provider: "human"`, `model: "phil"`,
`route: "hand"`. No code path stores or ships a row without a writer label.

## Review

Reviewers are agents or people who work through packets. Each packet is a folder addressed to one
reviewer: `packet.json` (the rows, the review brief version, the verdict schema) and the pictures its
reviewer's family may see (see "Which pictures go where"). The brief version is the version of the
writing rules when rules are loaded (with the rule pool, the version in `src/rps/rules/VERSION`, which
a reviewer compares with the brief it holds); only without rules does the configured
`review.brief_version` apply.

**Blind review.** A packet holds a review view of each row, never the stored row: the writer label is
removed and the row's own `source.model` is blanked. Beside the view goes the task's spec line (the
spec, plus the seed id and hint text for seeded items). The controller keeps the row to writer
mapping in its own events and matches verdicts back by row id.

**Verdicts.** A reviewer returns verdicts only, one JSON line per row, never an edited row:
`{"row_id": ..., "verdict": "keep" | "fix" | "reject", "reasons": ["short_code"], "notes": "..."}`.
`fix` and `reject` need at least one reason code. Notes should name the problem and where it is
(which field, which thought) and are refused over 2000 characters.

**Policy** (the `review.policy` settings, defaults shown):

1. The first review goes to a reviewer from a family other than the row's writer family, taken in
   turn from all available reviewers.
2. A `keep` with no assessor flag accepts the row.
3. A `fix` or `reject`, or an assessor flag, puts the row in question. It goes to a further reviewer
   from a family not yet used on it.
4. Two keeps accept a row (`keeps_to_accept`); two rejects discard it (`rejects_to_discard`).
5. After three reviewers with no settlement (`max_reviewers`) the row goes to the hand queue
   (`row_escalated`, then `row_to_hand`). The same happens when no reviewer from an unused family is
   left.
6. A random sample of accepted rows (`audit_fraction`, default 10%, chosen by a hash of the row id so
   it is reproducible) goes to one more reviewer of another family. The audit is recorded and changes
   nothing unless it rejects, which puts the row back in question.

Every verdict is kept with the reviewer's id and family.

**The hand queue.** The controller lists rows waiting for the owner in `hand/queue.jsonl`, with the
row, the task's spec line, the assessor flags and every verdict. The owner answers by adding a file
under `control/hand/` in the state store, one decision per line:

    {"row_id": "...", "decision": "accept"}
    {"row_id": "...", "decision": "discard"}
    {"row_id": "...", "decision": "replace", "row": {"thoughts": ["written by hand"]}}

A `replace` row is stamped with the human writer label and the row's source label. Decisions for
rows not in the queue are ignored; a decision that cannot be used is reported once.

**The reviewer client.** For an agent session or a person:

    python -m rps.review pull    --reviewer ID --config PATH --into DIR
    python -m rps.review status  --reviewer ID --config PATH --into DIR
    python -m rps.review verdict --packet P --row R --verdict keep|fix|reject --reasons a,b --notes TEXT --into DIR
    python -m rps.review check   --packet P --into DIR
    python -m rps.review push    --packet P --config PATH --into DIR

`pull` copies the reviewer's next packet into a local folder. `verdict` validates and appends to the
reviewer's own verdict file (`--replace` overrides an earlier verdict for a row). `check` validates a
verdict file, including a hand-written one. `push` uploads the verdict file and only then deletes the
packet's local pictures. A reviewer who stops can start again: it resumes from its own verdict file.
Exit codes: 0 done, 1 invalid input, 2 refused (for example pushing a packet that still has rows
without verdicts), 3 the store or configuration failed.

In Python, the same client runs a packet with a function or a command line per row
(`ReviewerClient.run_packet`).

The bucket copy of a packet's pictures is deleted by the controller once it has read that packet's
verdicts (recorded as `packet_burned`). Reviewers delete their local copies when they push.

## Dry run

A dry run sends a few real calls through one route to show that it works: the key, the model name,
the request form, the batch (for a batch route), the answer, the usage and the price.

    python -m rps.dryrun --route luna-sync --calls 5

or, in a notebook (no argument parser needed):

```python
from rps.dryrun import dry_run
report = dry_run("luna-sync", calls=5)         # report.ok, report.outputs, report.usage, report.cost_usd
```

Without a route name the tool tries `luna-sync`.

The calls carry a tiny plumbing prompt and a tiny answer schema, never the writing rules, and are
charged to purpose `test`. It prints the parsed answers, the token counts per bucket and the cost
next to the estimate. It refuses, before sending anything, when the estimate plus what earlier dry
runs of the route spent (kept in `state_local/dryrun_spend.json`) would pass the test cap
(`--cap`, default $0.50), when the route is closed (no key, switched off), and when more than 25
calls are asked for. A batch's id is written to `state_local/dryrun_<route>_<time>.json` before the
wait, so a stopped wait can be picked up with `--resume FILE` (nothing is sent again). It waits up to
25 hours for a batch (`--wait-minutes`), checking every 20 seconds (`--poll-seconds`).
`--image FILE --image-class CLASS` sends a picture with every call to a route the routing table
allows it for. For `deepseek-sync` the tool switches the route on and its off-peak gate off, since it
is a small test. The exit code is 0 when every call came back with a good answer, 1 when something
did not work and 2 when the tool refused to start.

## Resuming

| If the run stops here | Then the next run |
|---|---|
| before a batch id was recorded | asks the provider for batches matching the prepared calls and adopts every one it finds (`Writer.recover`, told when the calls were prepared and which batches are already recorded), else submits. OpenAI and Gemini find a batch by a digest of its calls; Anthropic by the batch's size and creation time, with a check at collection that it held the calls (see above) |
| after a submit that failed on the way back from the provider (same run) | looks for the batch the same way before it sends the calls again, so a batch the provider did take is adopted and not paid for twice |
| after `batch_submitted` | polls that batch; it never submits the same calls again |
| while a one-call route has calls in flight | records the calls that finished (they were recorded as they landed) and sends the ones that did not as rebounds. At most one window (four calls per worker) was in flight; the rest of the list had not been prepared yet and is simply taken up in its turn |
| while a one-call route is paused for a rate limit | starts with the route open again; the calls that were waiting out the pause come back as rebounds, like any call that was in flight, and the lines not yet prepared are taken up in their turn |
| after the tasks file changed (or is gone) | reads the file again on the next pass; tasks already recorded are ignored, a missing file in the state store means no tasks |
| between recording results and cleaning up | the results are durable first; a clean-up that did not happen (OpenAI's files) is left to the files' expiry |
| between writing a packet's files and announcing it | writes the same packet again (the id comes from its rows) and announces it once |
| while shipping | rebuilds the shipped files from the accepted rows; `shipped` is recorded only after the files are durable |
| in the middle of an event line | skips the torn last line, reports it, and carries on |

Replaying the same events twice gives the same state. The tests stop a run after every single event
and check that the resumed run ends in the same place with no repeated call or row.

## Plugging in

- **Assessor.** `rps.assess.AssessFn` takes a row and a context and returns an `Assessment`
  (`passed`, `flags`, `errors`, `set_aside`). `AssessCallFn` takes all the rows one call returned and
  the call's context and returns one `Assessment` per row, in the same order, so a check that
  compares the rows of a call (a series' nesting, one genre for the whole series) can run; give an
  error to every row and the whole call is written again. Pass either to
  `build_controller(config, assess=..., assess_call=...)`; the call-level function wins when both
  are given. Without either, the rule set's own assessor is used (the rule pool brings
  `rps.assessor.Assessor`), else a function that accepts any row with content. Error lines go back to
  the writer as `code: message` on the one retry; flags are bare codes for the reviewers. The
  context carries `assignment`, the runner's choices for the call as plain data (for the rule pool
  `{"relation_part", "unknown_keys", "wordings"}`, the same values the task text told the writer).
- **Writers.** `rps.providers.base.Writer` has `prepare`, `submit`, `poll`, `results` and `recover`,
  and optional `accepting`, `split`, `release`, `close`, `check_rules`, `restore` and `drain_notes`.
  `recover(requests, *, since, recorded)` is told the earliest moment the requests were prepared and the
  handle ids the controller already holds. A writer that keeps a provider-side cache that is billed by
  the hour sets `bills_cache_storage` and notes each cache (`drain_notes`, kind `cache_created`) with
  its `tokens` and the stretch of its life it covers (`created` or `extended_from`, and `expires`); the
  controller charges the storage. A writer that sends one call at a time can build on
  `rps.providers.pool.PoolWriter`. Register an adapter in `rps/providers/__init__.py`. `FakeWriter`
  needs no network and can be told what to return, which is how the tests run.
- **Planner.** `build_controller(config, planner=fn)`, where `fn(state)` returns a list of
  `TaskSpec`, replaces the tasks file.

## Keys

Keys are read from environment variables only: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `DEEPSEEK_API_KEY` and `HF_TOKEN`. They are never printed, logged, written to a
file or put in the configuration; error text is scrubbed of anything shaped like a key.

## Left for later

- The calls in flight on a one-call route are fixed by `workers`. Nothing raises or lowers the number
  by itself from the provider's rate-limit headers; the pause after a run of rate limits is the only
  brake. The default of 32 comes from the entry tier's limits and an assumed call time, not from
  a measurement: size it by the `minutes` of each call in the ledger once real runs exist.
- The code `rate_limit_exceeded` marks a failed OpenAI response as a rate limit, and a 429 marks any
  provider's error as one; a rate limit that a provider reports in some other form is recorded as an
  ordinary provider error and retried as a rebound.
- Routes of kind `claude-code`, `codex` and `gemini-cli` (the configuration accepts them; there is
  no adapter).
- Gemini batches from a JSONL file (only inline requests are built).
- The pricing test that settles whether batch discounts stack with cache prices, and the probe
  phase that compares effort and picture-resolution levels (the defaults are for dry runs).
- A near-duplicate index kept across runs for the assessor (`Assessor(index=...)` takes one).
- The one-time cost of making a Gemini cache is not stated on the provider's page and is not counted.
  A cache made just before a crash, before its note was recorded, is not found again: it is made
  anew, and the first one is paid for until it expires.
- An Anthropic batch found again by size and time that turns out to be a stranger's is found out
  only when it ends (up to 24 hours), because the provider shows what a batch holds only then.
- Two lost Anthropic batches of the same size are matched to their groups in the order the groups
  were sent; if that guess is wrong the check at collection sends the calls again.
- The prices of a prompt over a long-prompt threshold are not modeled: such a call is refused.
