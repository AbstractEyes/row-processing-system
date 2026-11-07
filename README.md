# row-processing-system

A controller that writes dataset rows through model APIs, checks them, hands them to review
agents, collects their verdicts and ships the accepted rows to a private Hugging Face dataset
repository. It runs on a Colab CPU runtime (and locally for tests) and can stop at any moment
(runtime lost, Ctrl+C, a stop file) and resume exactly where it left off, without repeating a
call or a row.

Version 0.1.0. The provider adapters (`anthropic_api`, `openai_api`, `gemini_api`) are stubs
that raise `NotImplementedError`; everything else works and is tested offline with a fake
writer.

## How a row travels

| Step | What happens | Recorded as |
|---|---|---|
| plan | Task definitions (a batch of spec lines for one source and route) are read from the tasks file | `task_created` |
| prepare | Each spec line becomes a call; its estimated cost is checked against the route and purpose budgets first | `call_prepared`, or `budget_refused` |
| submit | Calls go to the provider as a batch or one by one; the provider's batch id is made durable before anything waits on it | `batch_submitted` |
| poll, collect | Finished batches are read; every returned row is stamped with its writer label and source label | `call_returned`, `call_failed` |
| assess | A pluggable function checks each row | `row_assessed`, `row_rejected`, `row_set_aside` |
| review | Rows are handed to reviewers in blind packets; verdicts come back and settle the rows | `packet_published`, `verdict_received`, `row_escalated`, `row_to_hand`, `row_accepted` |
| ship | Accepted rows and the ledger are written to the dataset repository | `shipped` |

The events are one JSON object per line (`ts`, `actor`, `kind`, `payload`). The controller's
in-memory state is nothing but a replay of those events, so any run can be rebuilt from the
files alone.

## Layout of the repository

    pyproject.toml, requirements.txt   package metadata and exact pins
    src/rps/
      config.py        Config: routes, budgets, sources, review policy, commit batching
      ids.py           deterministic task, call, row and packet ids
      events.py        the append-only event log and tolerant replay
      state.py         the state rebuilt from events, and the queries the controller asks
      layout.py        every path in the stores, and who may write it
      store/           Store interface; local folder, Hub dataset repo, Hub storage bucket
      provenance.py    writer and source labels stamped on every row
      ledger.py        usage records, the price table, per-route and per-purpose budgets
      providers/       the Writer interface, FakeWriter, and the stub adapters
      assess.py        the assessment hook
      review/          packets, verdict format, review policy, reviewer client and its CLI
      controller.py    the loop
      runtime.py       build the stores, writers and controller from a Config
      keys.py          API keys from environment variables only; Colab secret mapping
      images.py        shrink a picture to a source's image size
      tasks.py         task definitions and the tasks file
      data/prices.json the dated price table
    notebooks/controller_colab.ipynb   run the controller on a Colab CPU runtime
    tests/                             offline tests (no network, no keys)

## Install and test

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt     (Windows; use .venv/bin/python elsewhere)
    .venv\Scripts\python.exe -m pytest -q

`requirements.txt` holds the exact versions the tests ran against. `pyproject.toml` declares
looser bounds for `pip install "git+https://github.com/AbstractEyes/row-processing-system@v0.1.0"`.
The tests never touch the network: a fixture refuses socket connections and empties `HF_TOKEN`
and the provider keys.

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
is safe. Run it again and it resumes.

On Colab, open `notebooks/controller_colab.ipynb`. It maps the Colab secrets `CLAUDE_API`,
`OPENAI_API`, `GEMINI_API`, `DEEPSEEK_API` and `HF_TOKEN` to environment variables (printing only
the secret names), loads a configuration, runs the controller for a time budget and prints the
status. As shipped it is a dry run with a pretend provider on the runtime's disk.

### Stopping

Create the file named by `stop_file` (default `STOP` inside the work folder) or, from another
machine, the file `control/STOP` in the state store. The run finishes the step it is in and ends
cleanly. Remove the file to run again.

### The tasks file

A JSON list. Each task is a batch of spec lines for one source, written through one route for one
purpose:

```json
[
  {
    "source": "photos", "batch": "batch-0001", "route": "fast-batch", "purpose": "production",
    "lines": [
      {"line_id": "001", "spec": "what the writer is asked to do", "item_id": "img-001", "image": "img-001.jpg"}
    ]
  }
]
```

The file may grow between passes. A task already recorded is ignored. The same lines through another
route are a different task when given a `variant` (use the route name).

## Configuration

`Config` is loaded from a JSON file or a dict. It holds no keys, and a value that looks like a key
is refused. Unknown keys are refused too.

| Key | Meaning |
|---|---|
| `bucket` | The storage bucket for event logs, review packets, pictures and verdicts. When absent, the work folder is used |
| `data_repo` | The dataset repository for accepted rows and the ledger. When absent, the work folder is used |
| `work_dir` | Local folder for dry runs, the stop file and the default pictures folder |
| `routes` | One entry per way of getting rows written: `provider`, `model`, `kind` (api, batch, claude-code, codex, gemini-cli), `effort`, `budget_usd`, `purpose_caps`, `max_output_tokens`, `max_batch_calls` |
| `sources` | Per source: `image_size`, `license`, `dataset`, `images_dir`. A spec line with no license (its own or its source's) is refused |
| `commit` | How writes are batched, see below |
| `review` | Reviewers, families, packet size, brief version and the review policy |
| `price_table` | Path to a price table file (default: the dated table in the package) |
| `time_budget_seconds`, `poll_seconds`, `max_attempts` | Run limits (an attempt past the first is a rebound) |
| `stop_file`, `remote_stop_path` | Where a stop request is looked for |
| `wait_for_review` | Keep a run going, polling for verdicts, while any packet is out with a reviewer (default: a run ends when the controller has nothing left to do itself) |
| `tasks_file` | The tasks file the planner reads on every pass |

### Stores and who writes what

A run uses two stores. The state store holds what changes often and what must really disappear
(pictures); the ship store holds what is final.

| Path | Writer | Store |
|---|---|---|
| `events/controller-<run>.<part>.jsonl` | the controller run named in the path | state (bucket) |
| `packets/<reviewer>/<packet>/packet.json`, `images/` | the controller | state |
| `review/<reviewer>/<packet>.jsonl` | the reviewer named in the path | state |
| `hand/queue.jsonl` | the controller (rows waiting for the owner) | state |
| `control/STOP`, `control/hand/*.jsonl` | the operator | state |
| `accepted/<route>/<task>.jsonl` | the controller | ship (dataset repo) |
| `ledger/summary.json`, `ledger/tasks.jsonl` | the controller | ship |

Every file has exactly one writer. The stores wrap every write in a check of this table, so a
reviewer cannot write into the controller's files or another reviewer's, and each run gets its own
event files. Run one controller at a time per state store.

The dataset repository store is deliberately slow: at most one commit per `ship_seconds_per_commit`
(default 60 seconds), under 100 files per commit (default 99), at most 300 commit attempts an hour
(the Hub's limit is about 320, failed attempts included), with exponential backoff and `Retry-After`
on HTTP 429. Each commit passes the head it last saw, and a commit by another controller is
reported rather than overwritten. The bucket store batches by count and by age. It is not
transactional, so a failed operation is retried on its own; every operation is an overwrite or a
delete, so repeating one is harmless.

## Budgets

Every call has a purpose: `test`, `pricing`, `probe`, `rebound` or `production`. A route has one
dollar cap per purpose plus a total. Before a call is submitted its estimated cost (all input at the
full rate, output up to its limit) must fit under both its (route, purpose) cap and the route total,
counting what is already spent and what calls in flight may still cost. A call that does not fit is
refused, never sent, and recorded as `budget_refused` with the numbers:

    {"route": "fast-batch", "purpose": "pricing", "est_usd": 0.002, "reason": "purpose_cap",
     "purpose_spent": 0.004, "purpose_cap": 0.004, "route_spent": 0.004, "route_cap": 5.0, ...}

A retry or re-submission after a failed or rejected call is always `rebound`, whatever the task's
purpose was, and draws only from the `rebound` cap. A purpose with no cap counts as a $0 cap, so its
calls are refused. A refused call is left alone for the rest of the run and asked again by the next
one, so raising a cap takes effect on the next run. `controller.format_status()` prints spend per (route, purpose) against its cap, and
`ledger/summary.json` and `ledger/tasks.jsonl` record the same with token counts, attempts and
minutes. Prices come from `data/prices.json`, which carries its own date; each usage record keeps
the date of the table it was priced with.

## Labels

The runner stamps two labels on every row; a model's own value under a reserved key is dropped (and
the row is flagged for the reviewers):

- `writer`: provider, model, route, effort, date, task id, pool version and the size of the picture
  the writer saw.
- `source_label`: dataset, item id (seed or picture) and license.

A row the owner writes or rewrites by hand carries `provider: "human"`, `model: "phil"`,
`route: "hand"`. No code path stores or ships a row without a writer label.

## Review

Reviewers are agents or people who work through packets. Each packet is a folder addressed to one
reviewer: `packet.json` (the rows, the review brief version, the verdict schema) and the pictures.

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

## Resuming

| If the run stops here | Then the next run |
|---|---|
| before a batch id was recorded | asks the provider for batches matching the prepared calls and adopts one (`Writer.recover`), else submits |
| after `batch_submitted` | polls that batch; it never submits the same calls again |
| between writing a packet's files and announcing it | writes the same packet again (the id comes from its rows) and announces it once |
| while shipping | rebuilds the shipped files from the accepted rows; `shipped` is recorded only after the files are durable |
| in the middle of an event line | skips the torn last line, reports it, and carries on |

Replaying the same events twice gives the same state. The tests stop a run after every single event
and check that the resumed run ends in the same place with no repeated call or row.

## Plugging in

- **Assessor.** `rps.assess.AssessFn` takes a row and a context and returns an `Assessment`
  (`passed`, `flags`, `errors`, `set_aside`). Pass it to `build_controller(config, assess=...)`. The
  default accepts any row with content. A failed assessment retries the call once with the error
  lines as feedback.
- **Writers.** `rps.providers.base.Writer` has `prepare`, `submit`, `poll`, `results` and `recover`.
  Register an adapter in `rps/providers/__init__.py`. `FakeWriter` needs no network and can be told
  what to return, which is how the tests run.
- **Planner.** `build_controller(config, planner=fn)`, where `fn(state)` returns a list of
  `TaskSpec`, replaces the tasks file.

## Keys

Keys are read from environment variables only: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `DEEPSEEK_API_KEY` and `HF_TOKEN`. They are never printed, logged, written to a
file or put in the configuration; error text is scrubbed of anything shaped like a key.

## Left for later

- The three provider adapters (stubs).
- The real assessor, which is meant to arrive as a separate package plugging into `AssessFn`.
- The price table is seeded by hand and carries its date; check it before spending.
