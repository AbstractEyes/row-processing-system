# row-processing-system

A controller that writes dataset rows through model APIs, checks them, hands them to review
agents, collects their verdicts and ships the accepted rows to a private Hugging Face dataset
repository. It runs on a Colab CPU runtime (and locally for tests) and can stop at any moment
(runtime lost, Ctrl+C, a stop file) and resume exactly where it left off, without repeating a
call or a row.

Version 0.3.0.dev0 (unreleased; the last release is 0.2.9). It has real writers for Anthropic (Message Batches), OpenAI (one call at a time;
the Batch API adapter is in the package, but this project does not use it), Gemini (inline
batches) and DeepSeek (one call at a time, off-peak), a one-call route for each of the first
three, the compiled rule pool with a prompt profile for each provider and its assessor
(`rps.rules`, `rps.assessor`), and a dry-run tool for trying one route with a handful of calls.
Everything is tested offline: with a fake writer, and with each adapter against the SDKs' own
types built from recorded-response-shaped data.

Licensed under the Apache License, Version 2.0 (see `LICENSE`).

## What changed in 0.3.0 (unreleased)

| Change | What happens now |
|---|---|
| A row a reviewer sent back can be written again, once | A new `repair` section of the configuration: `repair.enabled` (default `false`) and `repair.min_rows` (default 20, at least 1). Reviewers never edit a row; with `review.policy.fix_to_hand` on, a row with a `fix` verdict waits in the hand queue for the owner. With `repair.enabled` it is sent back instead, once, to the route that wrote it, with the reviewers' notes, so that the writer writes it again. The new row goes through the checker and a fresh first review like any row. With the step off (the default) nothing is sent back, no call is made for it and none of its events is recorded: rows move as they did before (the ledger lists the new purpose with nothing spent on it). See "The repair step" |
| A new purpose, `repair` | `repair` is a purpose with a cap of its own in a route's `purpose_caps`, beside `test`, `pricing`, `probe`, `rebound` and `production`. Every call of the repair step is charged to it, whatever the purpose of the task the row came from and whatever the call's attempt number: the call is made after the line's first one, so under the old rule (any attempt after the first is a rebound) it would have drawn on `rebound`, and a repair call that is made again after a passing failure is charged to `repair` too. A route with no cap for it, or a cap of nothing, gets no repair calls: its rows stay in the hand queue for the owner. When the step is on, the run notes such a route when it starts, in the words used for any purpose without a cap (`route NAME: no cap for purpose 'repair' (calls with that purpose are refused)`); with the step off nothing is said. A repair call the cap cannot hold yet waits, and one it could never hold is refused, like any call (see "Budgets") |
| Which rows are sent back | A row in the hand queue for the reason `fix_to_hand` gives ("a fix verdict: the row waits for the owner"), that the owner has not answered (the owner's decisions are read first, in the same pass), that was never sent back before, that is not itself the result of a repair, and that was written by a route that is switched on and has a repair cap. A row in the hand queue for any other reason (three reviewers without a settlement, no reviewer for it) is left to the owner. The step needs `review.policy.fix_to_hand`: without it no row waits with a fix verdict, and a configuration that turns the step on without it is noted when the run starts |
| Repairs go out in batches | Each route is looked at on its own. When it has at least `repair.min_rows` candidates, or fewer when the controller has nothing else to do for the route (no call of it in flight or waiting to go, no spec line of it due a call, and no row it wrote still to be checked; rows with the reviewers do not count), the controller records `repair_requested` (`row_id`, `call_id`, `route`) for each candidate of the route, in row-id order. The row leaves the hand queue (its state is `repairing`) and the call the event names is prepared in the next pass, like any call, under the route's repair cap. A batch route is sent its repairs together, and the last few rows of a job are not left waiting for a batch that will never fill |
| What the writer is told | The same spec line, through the same route, with the same picture and choices as the first call, followed by words of their own: an earlier row for this item was sent back by a reviewer; here are the notes; write the row again, fixing exactly those problems and keeping everything else that was right. The call's feedback (recorded in `call_prepared` and handed to the writer) is the notes of every `fix` verdict on the row, one item each, the notes alone and never a reviewer's id or family, then the row as it was written (its JSON without the labels the runner stamps on it, `row_id`, `writer` and `source_label`) as the last item. A fix verdict that came with no note gives its reason codes in the note's place. The words are the same for every prompt profile |
| The new row | A repair call is a call of the same line with the next attempt number that is free, and `repair_of` (the row) in its `call_prepared` and `call_returned` events. Each row it writes has an id of its own, made from the call like any row's, and is linked to the row it repairs by `repair_of` in the events and the state, never as a column of a stored or shipped row: an accepted new row ships exactly like any accepted row, and the row that was sent back never ships. The checker runs on the new row as usual. Its first review goes to a reviewer outside the writer's family, preferring one that did not review the row it replaces (any reviewer outside the family when no other is available). The row that was sent back is `repaired` as soon as a row the writer wrote for it passes the checker |
| Once only | A row is sent back once. A row that fails the checker after a repair is never written again, even where `max_attempts` would allow another attempt. When the repair comes to nothing the row that was sent back is in the hand queue again, for the owner, with a `row_to_hand` that says why: "the repair failed the checker" (every row the writer wrote was rejected or set aside by the checker), "the repair gave no row" (the writer set it aside), "the repair call failed (kind)" (a failure no retry mends), or "the repair was not made" (the step, or the route's repair cap, was switched off after the row was sent back and before its call). A repair call that failed for a passing reason, a provider error say, is made again with the same notes and row while the row has had fewer than `max_attempts` repair calls. A row that was written again and is sent back by a reviewer again goes to the hand queue and is never sent back again. The owner answers any of these rows as usual |
| Restart | The state is rebuilt from the events alone: `repair_requested`, `call_prepared` with `repair_of` and the `row_to_hand` that ends a repair that came to nothing are all it reads, never the configuration. The tests stop a run after every event of a repair (the requests, the calls, the new rows, their reviews, a repair that fails the checker, a row sent back a second time) and check that the resumed run ends in the same place with no row sent back twice and no call made twice |
| The hand queue and the status | A row in `hand/queue.jsonl` that a repair wrote has `repair_of`, the id of the row it replaces, and a row whose repair came to nothing has `repair`, the ids of the calls and rows of that repair. The progress line shows `N rows sent back to their writers` while repairs are out, and `format_status()` adds a line with the rows being written again and the rows written again. `ledger/summary.json` and `ledger/tasks.jsonl` list the purpose `repair` like any other |
| Version | `0.3.0.dev0`. The notebook's install line is unchanged |

## What changed in 0.2.9

| Change | What happens now |
|---|---|
| A reviewer's keep can be made to need a second opinion | `review.reviewers[].solo_keep` (default `true`). With `false`, a keep from that reviewer never accepts a row on its own: when the only keeps on a row come from such reviewers, the row is in question and goes on to a further reviewer, as a flagged keep does. Acceptance by keeps then needs at least one keep from a reviewer that may keep alone, so two keeps from reviewers that may not do not accept a row: a third reviewer is asked, and without one (or after three reviewers) the row goes to the hand queue, with the reason saying that every keep came from a reviewer that may not keep alone. A keep from a reviewer that may keep alone counts toward the two keeps of an in-question row as before; rejects, the audit and the other settings are unchanged. The trust is read from the configuration when the verdicts are settled, and a reviewer the configuration no longer names keeps the default. A configuration in which no enabled reviewer may keep alone is noted when the run starts, since no keep could then accept a row. Without the setting nothing changes |
| A fix verdict can go straight to the owner | `review.policy.fix_to_hand` (default `false`). With `true`, a row that has a `fix` verdict goes to the hand queue as soon as the verdict is read, with the reason "a fix verdict: the row waits for the owner", instead of to a further reviewer. Reviewers never edit a row, so a row that needs a fix can only be mended by the owner, and with only two reviewer families on duty a row with a fix verdict ends in the hand queue anyway, after one more review that cannot change that; the setting skips that review. The accept and discard rules come first and still win: a row that `keeps_to_accept` keeps accept, or `rejects_to_discard` rejects discard, is settled that way whatever fix it was also given. A row with no `fix` verdict is not touched by the setting. Without the setting nothing changes |
| Rows stuck for want of a reviewer are reopened | A row goes to the hand queue for want of a reviewer when none from another family than the writer's was available for a first review, or none from a family not yet used on the row for a further one. On each pass the controller now looks at those rows again. A row is reopened when both of these hold with the configuration as it is now: the review policy, applied again to the row's verdicts, still asks for a review of it (a first review, or a further one), and the configuration has a reviewer that can take that review (one was enabled or added). The controller then records `row_reopened` (`row_id`, `call_id`, `reason`) and the row waits for a review again, a first review if it never had one and a further review if it did, so the usual assignment picks it up and the row leaves the hand queue file. A row the current policy settles is never reopened, whoever is available: one it would now accept or discard, or hand to the owner (a row with a `fix` verdict when `fix_to_hand` is true, or a row that has had `max_reviewers` reviewers), stays in the queue for the owner. A row the owner has already resolved is never reopened (the owner's decisions are read before the rows are looked at), and a row sent to the hand queue for any other reason, for example three reviewers without a settlement, stays until the owner answers. The state is rebuilt from the events alone, so a restart gives the same result |
| The packets of a reviewer who was switched off are withdrawn | A packet addressed to a reviewer who is now `enabled: false`, or no longer in the configuration, and for whom no verdict file has been pushed, will not be answered. The controller records `packet_withdrawn` (`packet_id`, `reviewer_id`, `row_ids`), deletes the packet's files (`packet.json` and the pictures) from the state store, and each row of the packet waits for a review again, to go to a reviewer that is on. The reviewer that was switched off does not count among the reviewers, or the families, that reviewed the row. A reviewer whose verdict file exists is never withdrawn, even when the file answers only some of the rows. A verdict file that turns up later for a withdrawn packet is ignored and noted once in the run's problems. A run that stopped after recording a withdrawal and before deleting the files deletes them when the next run starts. An audit that was out with a switched-off reviewer is withdrawn the same way and is due again |
| A packet sent after a withdrawal has an id of its own | A packet's id comes from its reviewer, its rows and their rounds. A row that was taken back from a packet adds how many times that happened to its part of the id, so when a reviewer is switched on again and the same rows go to it, the new packet never has the id of the withdrawn one. A row that was never withdrawn keeps the id it has always had |

## What changed in 0.2.8

| Change | What happens now |
|---|---|
| Prompt profiles | A route may name the wording of the writer prompt it writes with: `"profile": "openai"`, `"gemini"` or `"deepseek"`, or `default` (the rule pool's own prompt, used when a route names none). Each profile is a folder of `src/rps/rules/profiles/`: its system template (compiled into `writer_system.md` beside it), its user-message layout (`task.tpl.txt`, with named fields in braces), its retry block (`retry.tpl.txt`, with `{check_messages}`) and, where it has them, a hint block (gemini), its own answer-format block (deepseek) and the row schema in the OpenAPI form with a description on every field (gemini; a described schema whose structure is not the row schema's stops the route at setup). The profiles were drafted with each provider's own model, then checked rule by rule against the default prompt, and the rules a draft dropped, narrowed or contradicted were put back in its own wording. The checker, the screen, the per-call choices and the row schema are shared by all profiles. A profile the rules do not have is a configuration error at setup |
| Calls and rows record the profile | `call_prepared` events and the writer label of every row carry `rules_version` and `rules_profile`. Review packets stay blind to both: `packet.json` carries the shared brief version, and only the controller's `packet_published` event keeps the profile. The Gemini explicit cache and GPT's `prompt_cache_key` are keyed on the profile's version |
| Versions | Each profile has its own version, a hash of the files all profiles share, the worked examples and its own files; `python -m rps.rules.compile` writes every profile's prompt and version and `--check` covers them. Default `70de2be6650a` (was `e48cccb70779`), openai `0f18e39f81fc`, gemini `6b478fb112ee`, deepseek `5084a2909f3c`. Compiled system prompts: default 21,844 bytes, openai 22,234, gemini 31,334, deepseek 22,581 |
| The default writer template | Back to the 0.2.6 wording, which a side-by-side comparison on the same items favored over 0.2.7's longer template, with three lines added: what the story before, after and cause records hold and how long each may be; that `mood feeling` takes a value of its own list; and that a record the item gives is copied as given |
| Unknown-question keys fit the item | Person keys only for an item that records a person; thing keys only for an item with a thing and no hint; `mood tone` is out of the rotation; `body gesture` is left out beside a recorded pose. The rotation runs over the keys that fit, with a stride coprime to their number |
| The screen also reads the subject | `screen_item` also sets aside a seeded item whose recorded thing name (or a listed synonym) never appears in its hint; hints that hold only catalogue metadata, and picture-form names, are left alone |
| New checks | `thought_hint_copy` (error): a thought sentence made mostly of one hint sentence's content words. `pasted_value` (error): a record value pasted into prose ("exactly 1", "count of 2", "child girl"). `caption_compresses_hint` (flag): a caption that compresses a one-sentence hint. `caption_no_verb` (flag): a caption of five words or more in which no sentence has a verb (a word-list test, so it only flags) |
| Notebook | The install line names `v0.2.8` |

## What changed in 0.2.7

| Change | What happens now |
|---|---|
| The writer template leads with the facts | Most rows that came back from review after passing the checks came back for details no fact gave (a default pose, light, materials, sizes, a held thing) or for shallower rows that used facts only the deep row records, while the prompt forbade most of it in the middle of seventy rules. The template (`src/rps/rules/templates/writer_system.tpl.md`) now opens with THE FIRST RULE: ONLY THE FACTS (the usual slips are named, with one right and one wrong sentence, and a word with no record or hint sentence behind it is deleted), then RANKS (each row of a series knows only its own records; the framing limits what shows) and THE RECORDS YOU ADD (what mood, intent and each story slot hold). Also stated now: the mood is claimed once, with its source in the same sentence; directions belong to the frame and never to a person; the hint is never followed in its order; a person tag whenever a person is recorded; no appraisal of a minor's face or body; a record that cannot be what it says (a misspelled word, a thing the hint never mentions) is skipped, class `other`, and never written around. Seven checks close the prompt (BEFORE YOU ANSWER). The compiled prompt grew from 21,229 to 25,682 bytes |
| The task text names what a row may not use | For a drawn series, one line for each shallower row lists the facts only a deeper row records. For every item, a line lists the habitual details it records nowhere (a pose or motion, an expression, light, weather, time of day, materials, sizes, camera angle, focus, a held thing, sound); a seeded row may still take one where its hint states it plainly. Each unknown-question key now says what it covers ("that row says nothing about the lens"), and no thought, wording or tag may answer it. The task text is not inside the rules version: a shipped row says which version of this package wrote it (`writer.package_version`) |
| The assessor checks what that review sent back | 20 new finding codes (15 errors, 5 flags) and 7 codes that were flags and now stop a row, in the table below. An error rejects the row and is sent back to the writer on the one retry as `code: message`; a flag puts the row in question for the reviewers and rejects nothing. The pool names the checks behind its rules (22, 59, 66, 68 and the new rules 83-93) |
| A garbled seed is set aside before any call | `rps.assessor.screen_item(item)` finds a seeded item whose record name (one word, four letters or more, in the item's deepest spec) is one edit away from a word its hint also uses at least as often (a record `medal` under a hint about a metal plate). Such an item cannot be written around, and writing it only costs a call and a rejected row. The controller now reads each spec line with the rule set's screen before the line's first call. A reason sets the line aside with no call: the event `line_screened` (`task_id`, `line`, `item_id`, `route`, `class` `other`, `reason`) is counted apart from `row_set_aside`, which is a writer's own skip or the assessor's set-aside of a written row. The line is finished (outcome `screened`, counted as `lines_screened` in the progress counts and `screened_lines` in the state counts, and listed by `State.screened_lines()`): it is never written, retried or reviewed. A line is read once, and never when it is due a retry; a screened line does not use up a free worker of a one-call route. The rule pool brings `rps.assessor.screen_item`; `rules.screen` (`"module:name"`, or `"none"`) names another or turns it off, `Controller(screen=...)` replaces it, and a rule set without one screens nothing. A screen that raises on a line lets the line through and is noted once in the run's problems |
| Rules version `e48cccb70779` (was `acc36e4d1592`) | The template above, in the compiled `writer_system.md` and its example rows (the format block and the schema are unchanged). The pool has a new section K (rules 83-93) and the review brief lists the five new flags among the pointers a reviewer gets |
| The Gemini OpenAPI form is built at setup only where it can be sent | 0.2.6 test-built the OpenAPI form of the row schema at setup on every Gemini route. A batch route (which can step down to the documented form) and a route whose `schema` option is `openapi` still do, so a schema that cannot be written so is found before the run; a one-call route with `plain` or `exact` no longer does, so a schema that cannot be written in that form (a recursive one) does not stop a route that never sends it |
| Notebook | The install line names `v0.2.7` |

The assessor's finding codes that are new or raised in 0.2.7. An error stops the row; a flag does not.

| Code | Kind | What it stops or marks |
|---|---|---|
| `rank_scope` | new error | a row of a series that uses a word (five letters or more) which only another rank of the series records |
| `added_record` | new error | on a seeded item, a record the writer added (scene setting, weather or time, form space or contrast, shot sharpness) that the hint does not support, or clear or sunny weather against a hint with clouds or rain |
| `relation_placement` | new error | a place word (beside, next to, behind, in front of) for two things whose only relation places neither (same colour as, smaller than, looking at, holding); items with no hint |
| `hint_name` | new error | an animal's name taken from the hint (a word before the animal's noun) when no record holds it |
| `caption_hint_copy` | new error | a caption that restates the hint's first sentence |
| `tag_color_missing` | new error | a recorded color that is in no tag |
| `tag_color_detached` | new error | a recorded color that is only a bare color tag, not written with its thing |
| `tag_person` | new error | a person is recorded and no tag names the person (1girl, 1boy, 1other, 2girls and the like) |
| `tag_solo` | new error | the tag `solo` when no person is recorded |
| `story_slot` | new error | a story before or after that is a moment word (instant, moment, before, after, later) and not an event |
| `story_cause` | new error | a story cause that is only a time (evening) and not a reason |
| `chain_kind` | new error | a chain answer that is not a value of its key's kind (a lens under `shot lens`, a weather under `scene weather`) |
| `spec_talk` | new error | text that talks about the records (a record key used as a phrase, such as "the dusk scene time", "that relation", "the record") |
| `stock_opening` | new error | a thought that opens with a stock phrase ("Check the cup:", "Compare X with Y:") |
| `check_repeat` | new error | a check thought whose content repeats an earlier thought |
| `hint_order` | new flag | thoughts that take three or more of the hint's sentences one after the other in the hint's order |
| `mood_weak_source` | new flag | a mood read from the shot angle or from a relation |
| `second_mood` | new flag | a second mood claimed in the thoughts beside the recorded feeling |
| `unrecorded_size` | new flag | a size word (narrow, towering, vast, tiny and the like) that no record or hint holds |
| `unrecorded_material` | new flag | a material word (brick, concrete, stone, wood, metal and the like) that no record or hint holds |
| `unrecorded_pose` | error, wider | a pose word (stands, sits, perched, leans, walks, jumps and the like) about a person or an animal, in any text of the row, when no record or hint gives it, or when only another rank records it (the older check read the caption and prompt for sitting words only) |
| `state_conflict` | flag, now error | a pose word that conflicts with the recorded pose, or a look up under an overhead shot |
| `unrecorded_light` | flag, now error | a kind of light (soft or warm light, a warm glow, cool tones) when no light record or hint gives light; a looser form of the older check stays a flag |
| `caption_hint_list` | flag, now error | a caption that lists the things the hint depicts |
| `mood_cause_differs` | flag, now error | a mood record read from one thing whose mood thoughts credit another |
| `position_as_place` | flag, now error | a position record (center, left third) written as a place in the scene |
| `relation_side` | flag, now error | a side that can flip a relation ("to his left") where the frame's side or the named thing is meant |
| `unknown_stated` | flag, now error | an unknown question that the row's own text answers |
| `mood_weak_evidence` | error, wider | now also a mood read from the shot framing (it already stopped a mood read from a count or a position) |
| `minor_fit` | error, wider | now also loose (except for hair), delicate, fragile, vulnerable, intimate, intimacy and alluring, said of a minor's clothes or body |

## What changed in 0.2.6

| Change | What happens now |
|---|---|
| The Gemini batch service takes the row schema in an OpenAPI form | The probe of 0.2.5 found the schema is the part the batch service refuses: the request without it was taken; with the plain schema in `response_json_schema` every item came back "3: Request contains an invalid argument." after running; with the schema in `response_schema` the batch was refused at creation with `Unknown name "additional_properties"` at the schema and at every nested object. The SDK's `Schema` type has `additionalProperties` and sends it even as `false`; the API's Schema object has 22 keywords and not that one. The new schema mode `openapi` (`rps.providers.schemas.gemini_schema`) writes the schema with only those keywords at every depth (`additionalProperties` and the other JSON Schema keywords outside the list are left out, `$ref` is written out in place, every `required` list is kept, a value list stays a list only on a text field where all its values are non-empty text and otherwise moves into the description). The documented batch form (`batch_form: documented`, and the step down after a refused batch) sends it in `response_schema` whatever the `schema` option says. The probe has a variant `openapi` (the request as sent with this form in `response_schema`). `plain` and `exact` are as they were. **Not yet run against the live service**: whether the batch service takes the `openapi` form is what the next probe shows |
| The explicit cache's 404 is a matter of the API version | `caches.create` answered `404 Not Found` with an empty body (the SDK prints `{'message': '', 'status': 'Not Found'}` when the body is not JSON), which is a path the service does not have. google-genai 3.0.0 sends a Gemini API client's requests to `v1` unless told otherwise, and Google's caching page says explicit caching is in Beta, "endpoints and SDK methods are available under `v1beta`", with `/v1beta/cachedContents` the only path in its REST reference. The model name form (`gemini-3.8-flash`, sent as `models/gemini-3.8-flash`) is the one its examples use, and the page names Gemini 3 and 2.5 models as having explicit caching, so neither the model nor the size is the cause. The new route option `api_version` (`auto`, `v1`, `v1beta`) sets the version for the whole route; `auto` leaves it to the SDK except that a route with `cache: true` uses `v1beta` for the cache, the batch that names it and every poll, and `cache: true` with `api_version: v1` is refused when the route is made. The probe has `--api-version`, uses `v1beta` for a whole run that makes a cache, and prints the version it used in its first line. **Not yet run against the live service** |
| Claude writers and Claude reviewers get the pictures of public datasets | The routing table now allows `public_common`, `coco` and `ffhq` for Claude, as for the other families; `deepfashion`, `hico` and `synthetic` stay closed to every outside provider. The Anthropic adapter had its own text-only rule ahead of the table, so a task with pictures for a Claude route, refused when it was added before, would have been taken and failed when its calls were built; it now asks the table like the other adapters and sends the picture first in the user message as a base64 image block. Review packets already followed the table by the reviewer's family, so Claude reviewers get those pictures too. This sends no picture by itself: no task has one until a picture run is started |
| Notebook | The install line names `v0.2.6` |

## What changed in 0.2.5

| Change | What happens now |
|---|---|
| A call a cap cannot hold yet waits | A route with 8 workers still held every call of its window at once: each call held its worst-case estimate from the moment it was prepared, so a cap filled with holds while little was spent, and the calls that did not fit were refused for the run. A call now has three answers. It goes when it fits. It waits (nothing is recorded, the line stays due and is asked again as finished calls release their holds) when it does not fit only because other calls' holds are in the way. It is refused for the rest of the run (`budget_refused`, as before) only when the money actually paid, with the call's own estimate on top, passes a cap, so it could never fit. See "Budgets" |
| A one-call route holds one call per worker | A one-call route prepares a call, and so holds its estimate, only when a worker is free to send it: the window is `workers` calls (it was four per worker) and is refilled as results land, because a finished call wakes the loop. A sync route never holds more than its workers' calls. Results are written in commits paced by `commit.seconds_per_commit`, so a quick call does not become a commit of its own |
| The progress line separates money spent from money held | It shows `$0.0412 spent` and `$0.0100 held for calls in flight` apart, so the two are never read as one, and names the cap that calls wait under (`waiting: 7 calls waiting for room under the rebound cap of deepseek-sync ($0.50)`). A `budget_refused` event also records the held part of each cap (`route_held`, `purpose_held`) |
| DeepSeek holds at the off-peak price | With the off-peak gate on (`offpeak_only`, the default), a call is held at the off-peak price, not the peak price that is double, since the route sends only off-peak. With the gate off the hold is the peak price, as before. The price of a finished call is still the tier it was sent in |
| DeepSeek is told the form of the answer | JSON mode makes the answer valid JSON and nothing more. A pricing run of the rule pool had DeepSeek write `depth` as 1, 2 or 3 and record keys of one word, and most of its answers could not be converted (39 of 59 calls with low thinking, 60 of 64 with thinking off). The rule set's format block (the rule pool's `json_format.md`, 2,051 bytes: the answer field by field, placeholders in angle brackets, no content) is now appended to the system message after the rules text, for DeepSeek only. A rule set without a block is sent as before; the other providers have a schema and ignore it. The block is inside the rules version. See "The writing rules are inputs" |
| The retry says what is wrong with each row | A wrapper with a row the rules cannot convert was written again with a sentence naming the rows. The rule pool's converter now explains each one (`rps.rules.form.explain_failures`, for example `'depth' is 2; it is one of the words simple, medium, deep (never a number)`), and the controller appends those lines to the advice of the retry, as many whole lines as fit in 1,500 characters, then `and N more`. A rule set brings the function as `explain`; without one the sentence stands alone. Every unusable-output error is held to the same 1,500 characters |
| The package version is on every row | `writer.package_version` sits beside the pool version and the rules version. The task text is not inside the rules version, so a shipped row now says which version of this package wrote its task text |
| Gemini batch: a probe and a second form | A live run of `flash-batch` had every request of a batch refused ("3: Request contains an invalid argument.") while the one-call endpoint took the same request, and the batch error names no field. The provider's pages do not say why: the batch pages show an inline request with the schema in `response_schema`, the request's metadata under the name `key` and a cache only by name, and mention nothing of `response_json_schema`, `thinking_config` or `custom_id`. **The cause is not confirmed until the probe has been run against the service once.** `python -m rps.geminiprobe --route flash-batch --batch` sends batches of one item of the real request, as the writer builds it, then the same request with one part left out or changed (the schema, the system instruction, the thinking setting, the metadata, the explicit cache, the field that carries the schema, the name of the metadata), then the documented form and a bare request; it prints Gemini's whole error for each and names the part the service refuses (see "Dry run"). The writer meanwhile has a second form of its request, the documented one: when every item of a batch comes back refused as an invalid argument, the same calls are sent again in that form, once for the run, and if that is refused too the route stops with the provider's whole message. The route options `batch_form` and `schema_field` choose the form directly (see "Gemini") |
| Rules version `acc36e4d1592` (was `56e0e2a3129d`) | The format block above and the converter's explanations. The task text names an off-list drawn genre as a topic and asks for a listed genre. Relation questions are asked only for items that hold a relation. A deep row needs a compare or change thought. The thought-length floor is softer (about 1,100 bytes only when the records give that much to say). No fit or shape words about a minor's clothes or body, moods are hedged and tags are booru tags only. The assessor checks these: errors for fit or shape words about a minor, a deep row with no compare or change thought, a flat mood, a chain or unknown question for a key the row holds as a record, and tags that are not booru tags; flags for the reviewer for a relation the item did not give, an unknown question answered by the row's own text, a relation read backwards or by side, and tags that name a mood, palette or composition |
| A British spelling no longer stops or flags a row | Records keep their spelling and the writer is asked to prefer US spelling in thoughts, wordings and tags, but a slip is never refused. `rps.rules.spelling.americanize_row` turns a stored row's prose and tags into US spelling for a later export step; it is not wired into a run now |
| Notebook | The install line names `v0.2.5` |

## What changed in 0.2.4

| Change | What happens now |
|---|---|
| A writer's answer is never stored as one row | The rule pool turns the writer's wrapper (`skip`, `reason`, `class`, `rows`) into stored rows, and gives the whole wrapper back unchanged when one of its rows cannot be converted (a record key of one word, a value that is not text, a missing field). The controller took that wrapper for a row, so the whole answer was stored as one row and the assessor judged the wrapper. An object with a skip flag and a list of rows is now never a row: the call is recorded as unusable output (billed, written again with the reason as advice). The reason names the rows that cannot be converted, found by handing each one alone to the rules' `normalize` function, and the failed call keeps those rows as written (`unconverted_rows`). A wrapper that skips is still a skip |
| A checker that cannot read a row rejects the row | An exception from an assess function ended the whole run. The rule pool's converter makes `spec["relation"]` a dict, not a list, when a record's key starts with `relation` and the row has no relations of its own, and the pool's checker raises on it. An exception from an assess function (row by row or for a call) is now an assessment that fails: the row is rejected with `the assessor could not check the row (ErrorType: message)`, written again with that line as advice, and noted once in the run's problems. A wrong number of assessments from a call-level function is still a mistake of the function and stops the run |
| Gemini gets the row schema without its value lists | A live run of the rule pool through `flash-batch` had every request refused with "3: Request contains an invalid argument." The route option `schema` now picks the form of the row schema that is sent: `plain` (the default) keeps the structure and moves each list of allowed values into the description of its field, `exact` sends the schema as it is, `none` sends no schema (JSON mode). The pool's schema has seven value lists, one with an empty string and one of integers, and Google's advice for a schema it refuses as too heavy includes fewer enum values; the batch error names no field, so the cause is not confirmed until `rps.geminiprobe` (below) has been run once. The assessor checks every value whatever the form |
| Gemini's batch status codes | A failed batch item carries a gRPC status code, not an HTTP one, and the adapter read only HTTP codes, so code 3 (`INVALID_ARGUMENT`) counted as a passing trouble of the provider's and every call was tried again. Invalid argument, not found, failed precondition and out of range now stop the route like HTTP 400; permission denied and unauthenticated are key errors; canceled is retried; the rest is the provider's trouble. A failure that names the explicit cache is still retried, since the cache is made again when it has lapsed |
| A refusal keeps its whole message | A failed call kept 200 or 300 characters of the provider's error. It keeps up to 1,500 now, in the item error of a Gemini batch (with its details), the error of a one-call request, a failed job's note, the reason a route was stopped and a rejected batch |
| `rps.geminiprobe` | `probe_gemini("flash-batch")` (or `python -m rps.geminiprobe --route flash-batch`) sends the writer's exact request (the rule pool's prompt and task text, the route's thinking level and token limit) as one `generate_content` call for each form of the schema, `exact`, `plain`, `none`, until one is taken. A refused request costs nothing and comes back with the provider's whole error text; the tool prints it and says which `schema` option to set. It refuses to start when the calls may cost more than a cap (`--cap`, $0.25) |
| The status names the cap that refused a call | The progress line says `waiting: 7 calls refused by the rebound cap of deepseek-sync ($0.50)` and `format_status()` adds a line for each cap with how much of it was spent or held by calls in flight and what each call was held at. See "Budgets" for why a cap fills with holds |
| DeepSeek's thinking can be set | The route's `effort` is sent as `reasoning_effort` when one is set (`none` turns the thinking off, `low`, `high`, `max`). With none set the request is unchanged: DeepSeek thinks by default, the thinking counts toward `max_tokens` and is billed as output. The defaults are not changed; the provider's figures are in "DeepSeek" below |
| Rules version `56e0e2a3129d` (was `f12a1185ef75`) | The writer prompt states hard length caps per depth (with a floor of 1,100 bytes of thoughts for deep rows), says relations live only in `relations`, and makes keeping the given records and the series nesting rules of their own. The assessor has a new module, `review_checks.py`, with checks for faults found in review: repeats and self-talk, invented detail, mood, required records, the hint, step labels and wordings (errors only where a script can decide the case precisely; the rest are flags for the reviewer). The pool has a matching section, the writer prompt matching lines and the review brief matching lines |
| Notebook | The install line names `v0.2.4` |

## What changed in 0.2.3

| Change | What happens now |
|---|---|
| A run that only waits for review now ends | With a dataset repository as the ship store, a run whose only work left was review never ended: it had nothing to do, yet it wrote the ledger again on every pass and a commit with nothing new in it went out every minute (the Hub skipped it). Now the run ends as idle within one commit interval (a minute) of its last change |
| A file is shipped when the store has sent it | The same cycle meant accepted-row files were committed but never recorded as shipped, and were written and committed again every interval. A write to a store that sends in batches is not a shipment. A file now counts as shipped when the ship store has sent it, on whichever pass that turns out to be, and the `shipped` event is written then (and the ledger counts as written). A commit that falls due in the middle of a pass sends what is waiting and leaves the rest of that pass's files for the next commit; each is recorded when its own commit goes out. A local folder sends at once, so nothing changes there |
| Each file is handed over once | The controller hands a file to the ship store once while its content stays the same, and each ledger file again only when its own text changes, so no commit carries content the repository already has. If a file is handed over again with new content while it waits, the new content replaces it. A row taken back by an audit before its file was sent is not sent. A run that starts again reads the two ledger files from the ship store and hands over only those that differ, so a restart with nothing changed ends at once and sends no commit |
| The end of a run | A run that ends for another reason (the time budget, a stop file, an interrupt) sends what the ship store still holds before it stops and records it, so the next run does not send it again |
| Stores say what they have not sent | `Store.is_pending(path)` is true while a write or delete of the path has not been sent. A local folder never has anything pending. The progress line shows `waiting: N files for the store's next commit` while files wait |
| Notebook | The install line names `v0.2.3` |

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
| screen | Before a line's first call, the rule set's screen reads the line's item; a reason sets the line aside, and no call is made for it (a retry is never screened) | `line_screened` |
| prepare | Each spec line becomes a call; a picture is checked against the routing table; the estimated cost is checked against the route and purpose budgets | `call_prepared`, or `budget_refused` |
| submit | Calls go to the provider as a batch or one by one; the provider's batch id is made durable before anything waits on it | `batch_submitted` |
| poll, collect | Finished batches (or finished calls) are read; the rules turn each answer into rows; every row is stamped with its writer label and source label | `call_returned`, `call_failed` |
| assess | The rule set's checker (or a function you give) checks the rows | `row_assessed`, `row_rejected`, `row_set_aside` |
| review | Rows are handed to reviewers in blind packets; verdicts come back and settle the rows. A packet whose reviewer was switched off is taken back, and a row that waited in the hand queue for want of a reviewer is reopened when one is available | `packet_published`, `packet_withdrawn`, `verdict_received`, `row_escalated`, `row_to_hand`, `row_reopened`, `row_accepted` |
| repair | With `repair.enabled`, a row in the hand queue for a fix verdict is sent back, once, to the route that wrote it, with the reviewers' notes and the row as it was written. The call that writes it again is an ordinary call of the line, charged to the purpose `repair`; the row it writes is checked and reviewed like any row, and the row that was sent back never ships. A repair that comes to nothing puts the row back in the hand queue. See "The repair step" | `repair_requested`, then `call_prepared` (with `repair_of`), `batch_submitted`, `call_returned`, `row_assessed`, and `row_to_hand` for a repair that came to nothing |
| ship | Accepted rows and the ledger are handed to the dataset repository, which sends them in its next commit; a file counts as shipped when the repository has sent it | `shipped` |

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
      geminiprobe.py   find what Gemini refuses in the writer's request (one call per form of the schema, or batches of one item)
      assess.py        the assessment hooks
      rules/           the compiled rule pool: writer prompt, row schema, answer format block, review brief, examples
      assessor/        the assessor for rows written under that pool
      review/          packets, verdict format, review policy, reviewer client and its CLI
      repair.py        the repair step as pure functions: which rows go back to their writer, and what it is told
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
looser bounds for `pip install "git+https://github.com/AbstractEyes/row-processing-system@v0.2.7"`
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
| `review` | Reviewers (each with an `id`, a `family`, `enabled` and `solo_keep`, see "Review"), packet size, brief version and the review policy. `brief_version` applies only when no writing rules are loaded; with rules, a packet carries their version |
| `repair` | The repair step: `enabled` (default `false`) and `min_rows` (default 20, a whole number of at least 1: how many candidates a route gathers before its repairs are sent; a smaller batch goes when the controller has nothing else to do for the route). A row a reviewer sent back with a fix verdict is written again, once, by its own writer. It needs `review.policy.fix_to_hand` and, on each route that repairs, a cap for the purpose `repair` in `purpose_caps`. See "The repair step". A reviewer's machine needs a version of the package that knows this section to read a configuration file that has it, since unknown keys are refused |
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
| `effort` | The provider's reasoning or thinking level (Anthropic `low` to `max`, OpenAI's `reasoning.effort`, Gemini `minimal`, `low`, `medium`, `high`, DeepSeek's `reasoning_effort`: `none`, `low`, `high`, `max`); left out, the provider's default applies |
| `profile` | The wording of the writer prompt the route writes with: `default` unless set, or a profile of the rules (`openai`, `gemini`, `deepseek` in the rule pool). See "The writing rules are inputs" |
| `budget_usd`, `purpose_caps` | The route total and one cap per purpose |
| `max_output_tokens` | The token limit of a call (a call cut off at it is retried once with a larger limit, see `truncation_factor`). A model that thinks before it answers spends the limit on the thinking too, so it has to cover both |
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

A write to a store that batches returns when the change is buffered, not when it is sent. The
controller therefore asks the ship store which of its files it still holds (`is_pending`): it hands
each file over once, and records a file as shipped (and the ledger as written) on the pass that
finds the store no longer holding it. A run whose only work left is review ends as idle once the
last change has gone out, within one commit interval; `controller.is_idle()` is false until then.

## Routes and adapters

| Route | Provider and model | Path | Pictures |
|---|---|---|---|
| `haiku-batch` | Anthropic `claude-haiku-5-5` | Message Batches | public common, COCO, FFHQ |
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
| The window | The controller prepares a call for such a route only when a worker is free to send it, so at most `workers` calls are prepared and unfinished at a time (32 at the default), and each call's cost is held in the budget when it is prepared. A list of hundreds of thousands of lines is never all in memory or all held in the budget's reserve, and a route never holds more than `workers` calls' worth of money. The loop refills the window each time a result lands (a finished call wakes it); results are written in commits paced by `commit.seconds_per_commit`, so a quick call does not become a commit of its own. A stop or crash loses at most the calls in flight, which come back as rebounds. A call that a cap cannot hold yet waits, its line stays due, and it goes when finished calls release their holds (see "Budgets") |
| A rate limit (HTTP 429) | The provider's own client retries first (`max_retries`, default 2), waiting as the provider asks. A call still turned away after that did not run and was not billed, so it is not a failed attempt: the worker waits and sends it again |
| A run of rate limits | Five turned-away calls in a row, with no call answered between them, pause the whole route for 60 seconds. A pause that follows at once is twice as long, up to 15 minutes; one answered call starts over. While it is paused the route is "closed for now": nothing more is prepared for it, the run waits (the run is never stopped for this) and the route opens by itself, each worker starting again a few seconds apart so they do not meet the limit together. Calls already on the wire when a pause began do not count towards the next one |
| One call that never gets through | A call turned away 8 times is handed up as a failed call (`provider_error`) and comes back as a rebound, so one call cannot hold the route for ever |

### Anthropic (`haiku-batch`, `haiku-sync`)

| Part | Form |
|---|---|
| Request | `model`, `max_tokens`, the rules as one `system` block marked for a cache (`cache_control` `ephemeral`, one hour), one user message, `output_config` with the row schema (`json_schema`) and the effort |
| Pictures | A call carries a picture only where the routing table allows its class for Claude: `public_common`, `coco` and `ffhq`, the publicly viewable pictures of public datasets. The picture goes first in the user message as a base64 image block (`image/jpeg`, `image/png`, `image/webp` or `image/gif`) and the text after it; the cost estimate adds 1,600 tokens for it, and its bytes count toward the size that splits a batch. A call with a picture of any other class (`deepfashion`, `hico`, `synthetic`), of an unknown class or of none is refused, as for every provider |
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
| Request | One `InlinedRequest` per call: the user turn (text and, where allowed, the picture as bytes), `response_mime_type` JSON with the row schema in `response_json_schema` (or `response_schema`, as the `schema_field` option says) in the form the `schema` option names, `thinking_level` from the effort, `media_resolution` for a picture, `max_output_tokens`, the rules as `system_instruction`, and `metadata.custom_id` |
| The row schema | Gemini turns a schema into a decoding grammar and refuses one it finds too heavy; the refusal of a batch item is a code and one sentence ("3: Request contains an invalid argument.") that names no field. The option `schema` sets the form that is sent: `plain` (the default) is the same structure with each list of allowed values (`enum`) moved into the description of its field, `exact` is the schema as the rules give it, `none` sends no schema and asks for JSON only, `openapi` is the schema as the `response_schema` field takes it: an OpenAPI Schema object, whose keywords are `type`, `format`, `title`, `description`, `nullable`, `enum`, `items`, `maxItems`, `minItems`, `properties`, `required`, `minProperties`, `maxProperties`, `minimum`, `maximum`, `minLength`, `maxLength`, `pattern`, `example`, `anyOf`, `propertyOrdering` and `default`, so `additionalProperties` (and every other JSON Schema keyword outside that list) is left out at every depth, a `$ref` is written out in place, every `required` list is kept, and a value list stays a list only on a text field where all its values are non-empty text (the others move into the description, as in `plain`). The SDK's own `Schema` type has `additionalProperties` and sends it even as `false`, and the batch service refuses it by name ("Unknown name "additional_properties""). `rps.geminiprobe` tries the forms against the real service and prints the provider's whole message for any it refuses (see "Dry run") |
| Batch | `batches.create` with inline requests, split under 19 MB per job (the limit is 20 MB); jobs that stay pending or running past 48 hours expire and their calls come back as rebounds |
| The form of a batch request | A live batch had every item refused as an invalid argument although the one-call endpoint took the same request, so a batch route has two forms. The first is the request above. The documented one follows the batch pages: the schema in `response_schema` in its `openapi` form (whatever the `schema` option says, unless that is `none`), the request's metadata under `key`, and no explicit cache (the rules go in the request). The option `batch_form` is `auto` (the default: the first form, and when every item of a batch comes back refused as an invalid argument, the same calls once more in the documented form, for the rest of the run; a handle records the form its batch was sent in), `documented` (always the documented form) or `full` (always the first form, never a second). When the documented form is refused too, the route stops with the provider's whole message and the command that finds the part, `python -m rps.geminiprobe --route NAME --batch` |
| Results | `dest.inlined_responses`, matched by the `custom_id` (or `key`) in each response's metadata |
| Answer | `STOP` is parsed; `MAX_TOKENS` is a truncation; the filter stops (`SAFETY`, `PROHIBITED_CONTENT`, `SPII`, `RECITATION`, `BLOCKLIST`, the picture ones) and a blocked prompt are recorded and not retried; thought parts are left out of the text |
| A failed item | A batch item that failed carries a gRPC status code (3 is `INVALID_ARGUMENT`) with a message and details, kept up to 1,500 characters. Invalid argument, not found, failed precondition and out of range stop the route like HTTP 400; permission denied and unauthenticated are key errors; canceled is retried; the rest is the provider's trouble and is retried. A failure that names the explicit cache is retried, since the cache is made again when it has lapsed |
| Usage | the prompt count minus cached tokens, cached tokens, candidates' tokens, thinking tokens (billed as output) |
| The explicit cache | With `cache: true` (batches only), the route's requests go to the `v1beta` API (see the next row), the rules are cached once per model and rules version and the batch's requests refer to the cache by name instead of repeating the rules. The cache name is recorded (`cache_created`) so a restart reuses it, and its life is extended while a batch that uses it is running. A failure to create the cache is an error, not a silent fallback. The storage is charged to the ledger: each note carries the cache's size in tokens (the provider's report, or a generous estimate marked `tokens_estimated` when it gives none) and the stretch of the cache's life it covers, and the controller writes a `cache_storage` event for it, tokens times hours times the storage rate per million tokens per hour (`cache_storage` in the price table: $0.50 through 2026-12-31, $1.00 from 2027-01-01). A new cache is charged from its creation to its expiry and one kept alive longer from its earlier expiry to the new one, since the provider bills the time-to-live. The charge goes to the purpose most of the calls that use the cache carry and counts against that purpose's cap and the route total. A route with `cache: true` refuses to start unless the price table has a `cache_storage` rate for its model. The one-time cost of making the cache is not stated on the provider's page and is not counted |
| After a crash | The batch's display name carries a digest of its calls, so a batch created just before a crash is found again |
| The version of the API | The SDK sends its requests to one version of the Gemini API, and which one depends on the SDK: google-genai 3.0.0 defaults to `v1`, the stable one. Explicit caching is a Beta feature available only in `v1beta` (Google's caching page: "Endpoints and SDK methods are available under `v1beta`"; its REST reference lists `/v1beta/cachedContents` and no `v1` path). A cache made through a client on `v1` is a request to a path that does not exist, which is what the live probe met: `404 Not Found` with an empty body, shown by the SDK as `{'message': '', 'status': 'Not Found'}` (the model name form, `gemini-3.8-flash` or `models/gemini-3.8-flash`, is the one Google's examples use, and the page lists Gemini 3 and 2.5 models as having explicit caching). The option `api_version` is `auto` (the default: the SDK's own version, except that a route with `cache: true` uses `v1beta` for everything it sends, so the cache, the batch that names it and every poll go to one version), `v1` or `v1beta`. `cache: true` with `api_version: v1` is refused when the route is made, with the reason in the message |
| Options | `schema` (`plain`, `exact`, `none` or `openapi`; default `plain`), `schema_field` (`response_json_schema`, the default, or `response_schema`), `batch_form` (`auto`, `documented` or `full`; default `auto`; batch routes only), `media_resolution` (`low`, `medium`, `high`; default `medium`), `cache` (default false), `cache_ttl_seconds` (default 21600), `api_version` (`auto`, `v1` or `v1beta`; default `auto`) |

### DeepSeek (`deepseek-sync`)

| Part | Form |
|---|---|
| Request | A chat completion in JSON mode (`response_format` `json_object`): the rules as the system message, followed by the rule set's format block when it has one (the rule pool's is the answer's form field by field, with placeholders and no content), the task text and, where allowed, a picture as an `image_url` in the user message. JSON mode makes the answer valid JSON and nothing more, so the format block is what says that `depth` is a word and a record key is two words; the word "json" has to be in the system text, which is checked before a run |
| Sending | There is no batch service: a pool of workers (the route's `workers`) sends one call each; every result is recorded as it lands, so a stop loses only the calls in flight, which a restart sends again as rebounds |
| Off-peak | Calls go out only outside Monday to Friday 01:00 to 04:00 and 06:00 to 10:00 UTC. In those hours the route is closed for now, calls already queued wait at the edge, and the run keeps waiting. Every result is marked `peak` or `offpeak` by the time it was sent, and the ledger prices it by that tier. While the gate is on, a call is held in the budget at the off-peak price (it can only be sent then); with the gate off it is held at the peak price, the dearer one |
| Answer | `stop` is parsed (blank content is a failed parse, sent again once); `length` is a truncation; `content_filter` is a block; "too busy" is retried |
| Usage | cache hit tokens as cache read, cache miss tokens as input, completion minus reasoning as output |
| Thinking | The models think before they answer unless told not to (effort `high`). The route's `effort` is sent as `reasoning_effort` when set: `none` turns the thinking off, `low`, `high` and `max` choose how long it goes on (the provider reads `minimal` as `low`, `medium` and `xhigh` as `high`); its changelog suggests `low` for simple tasks, `high` for everyday agent work and `max` for complex problems, and gives no token figures per level. There is no thinking budget: the only cap is `max_tokens`. With no `effort` set, nothing is sent and the provider's default applies |
| Token limit | `max_tokens` bounds everything the model generates, thinking included (the usage figure `completion_tokens` holds the reasoning tokens): 1 to 393,216. When a request leaves it out the provider uses 8K without thinking, 64K with it and 128K at effort `max`. A call that reaches the limit is billed in full and returns nothing (`truncated`, tried once more with the limit times `truncation_factor`), so a route's `max_output_tokens` has to cover the thinking and the answer |
| Price of thinking | One output rate for the model, off-peak $0.60 and peak $1.20 per million tokens; the page lists no other rate for reasoning tokens, so thinking is billed at the output rate, and turning it off takes it out of the bill |
| Cache | The provider caches a prompt prefix on its side, "best effort": a request can only hit a prefix an earlier request left behind, and the page's own example has the shared prefix stored after the first requests have completed. The calls a pool of workers sends together at the start of a run all find the cache empty, so about the first `workers` calls are billed as misses ($0.15 per million tokens against $0.003 for a hit). It happens once per run and costs cents |
| Switched off | The route is off unless the configuration says `enabled: true`. The key is `DEEPSEEK_API_KEY` |
| Options | `detail` (`low` or `high`; default `low`), `offpeak_only` (default true; false turns the gate off, for a small test) |

## Which pictures go where

One table decides what an adapter may see. The class of a picture is the class of the picture's
own origin (a narration written for a COCO picture is COCO class).

| Class | Claude | OpenAI | Gemini | DeepSeek |
|---|---|---|---|---|
| `public_common` | yes | yes | yes | yes |
| `coco` | yes | yes | yes | yes |
| `ffhq` | yes | yes | yes | yes |
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
| `normalize(text, spec, model)` | turns the writer's answer into the JSON list of rows; applied once, whatever the provider. An answer it hands back as the writer's wrapper (a skip flag and a list of rows) was not converted and is recorded as a failed call, never as a row; the failed call names the rows that could not be converted |
| `explain(text, spec, model)` | for an answer `normalize` could not convert: one line for each row that cannot be read, `<row id>: <what is wrong and the form it needs>`. The lines follow the sentence that names the rows, in the advice the retry is given, as many whole lines as fit in 1,500 characters (then `and N more`) |
| `json_format()`, or a file `json_format.md` | the form of the answer as a template, field by field, with placeholders and no content, for a provider that has no schema mode. Notes in HTML comments are left out. Only the DeepSeek writer sends it (appended to the system message after the rules text); the others have a schema and ignore it. A package brings it as `json_format()` (which wins) or the file; `make_rules(..., json_format=...)` takes it as text. It is part of the rules version (a package's own version covers it; a hash version includes it) |
| an assessor | checks the rows (see "Plugging in") |
| `screen(spec)` | reads a spec line before its first call and returns the reason to set the line aside, or `None` to send it on (see "Plugging in") |

With the package `rps.rules` all of these come from the rule pool, the assessor is `rps.assessor` and the
screen is `rps.assessor.screen_item`. `rules.assessor` and `rules.screen` (`"module:name"`, or `"none"`)
name different ones.

The rule pool also brings prompt profiles: other wordings of the writer prompt, one folder each under
`src/rps/rules/profiles/`, for the routes that name them (a route's `profile`). A profile replaces the
system prompt, the layout of the task text, the retry block and, where it has one, the answer-format
block or the described schema; the rules it states, the checker, the screen, the per-call choices and
the row schema stay the pool's. Routes on the same profile share one set of rules. A row records the
profile it was written with (`writer.rules_profile`) and that profile's version (`writer.rules_version`).

## Failures

Every failed call is recorded with a kind (`call_failed.kind`). A call that may be retried is sent
again while the line's attempts are under `max_attempts` (default 2, so one retry):

| Kind | Meaning | What happens |
|---|---|---|
| `truncated` | the answer stopped at the token limit | retried with the limit times `truncation_factor`, with no advice added |
| `invalid_output` | blank, cut off, not the expected JSON, or the writer's wrapper whose rows could not be turned into stored rows (the failure names those rows) | retried with the problem as advice; for a wrapper, the rule set's line for each row that cannot be read follows (up to 1,500 characters in all) |
| `provider_error` | rate limit, server error, timeout, dropped connection, a batch that failed | retried as a rebound. On a one-call route a call still rate-limited after the client's own retries is not recorded here: it waits and goes again (only one turned away 8 times in a row is) |
| `expired`, `canceled` | the provider let the request lapse or canceled it | retried as a rebound |
| `refused`, `blocked` | the model declined, or a safety filter stopped it | recorded; not retried on the same route |
| `bad_request` | the request is malformed (HTTP 400, a gRPC `INVALID_ARGUMENT` on a batch item, a schema the provider rejects) | not retried; stops the route for the run |
| `auth` | the key is rejected or the balance is spent | not retried; stops the route for the run |
| `unexpected` | a stop reason nothing here expects | not retried (an error line an adapter cannot classify is retried as a rebound) |
| `long_prompt` | the call's estimated prompt is over the provider's long-prompt threshold (`long_prompt_tokens` in the price table), where higher rates apply that the table does not model | refused before it is sent, recorded as failed with no cost; not retried |
| `batch_mismatch` | a batch taken up again after a crash held none of the group's calls | retried as a rebound; not billed |

The provider's own client already retries rate limits and server errors a few times
(`max_retries`) with backoff; what comes back after that is recorded as above, except that a
one-call route waits out a rate limit instead (see "One-call routes at production size"). A call that
failed after the provider billed for it (a cut-off or unusable answer) still has its usage recorded
and priced.

A call of the repair step (see "The repair step") follows the same table, with two differences. Its attempt
numbers are its own: a repair call is made again after a passing failure while the row has had fewer than
`max_attempts` repair calls, and it is charged to the purpose `repair`, never to `rebound`. A row it wrote that
the checker rejects is not written again, whatever `max_attempts` says: the row that was sent back returns to
the hand queue instead.

## Budgets

Every call has a purpose: `test`, `pricing`, `probe`, `rebound`, `production` or `repair`. A route has one
dollar cap per purpose plus a total. Before a call is submitted its estimated cost (every input
token at the highest input-side rate, output up to its limit, a route with price tiers at the
dearest tier it can be sent in) is checked against both its (route, purpose) cap and the route
total, and there are three answers:

| Answer | When | What happens |
|---|---|---|
| goes | the estimate fits under both caps, counting the money spent and the money held for calls in flight | the call is prepared, held and sent |
| waits | the estimate does not fit only because other calls' holds are in the way: the money actually paid would leave room for it | nothing is recorded; the line stays due and is asked again on the next pass, as finished calls release their holds. The progress line counts it as `waiting for room under <the cap>` |
| refused | the money actually paid, with the call's own estimate on top, passes a cap, so the call could not fit even with nothing in flight | the call is never sent and is recorded as `budget_refused` with the numbers; it is left alone for the rest of the run |

A refusal records the figures of both caps, with the held part of each:

    {"route": "luna-sync", "purpose": "pricing", "est_usd": 0.002, "reason": "purpose_cap",
     "purpose_spent": 0.004, "purpose_held": 0.0, "purpose_cap": 0.004,
     "route_spent": 0.004, "route_held": 0.0, "route_cap": 5.0}

A retry or re-submission after a failed or rejected call is always `rebound`, whatever the task's
purpose was, and draws only from the `rebound` cap. The one exception is a call of the repair step
(a row sent back to its writer, see "The repair step"): it is `repair` and draws only from the `repair`
cap, though it is made after the line's first call, and so is a repair call made again after a passing
failure. A purpose with no cap counts as a $0 cap, so its calls are refused; a route with no `repair` cap
is not sent any row to repair. A refused call is left alone for the rest of the run and asked again by the next
one, so raising a cap takes effect on the next run. The progress line and the status name the cap
that holds the calls back (`waiting: 7 calls waiting for room under the rebound cap of deepseek-sync
($0.50)`, or `calls refused by` for a refusal) and show money paid and money held apart
(`$0.0412 spent | $0.0100 held for calls in flight`), and `format_status()` adds a line for each cap
with how much of it was spent or held by calls in flight and what each call was held at.

A call in flight holds its estimate, not its price: the worst case, which prices the output at its
limit and a route with tiers at the dearest tier it can be sent in (DeepSeek with its off-peak gate on
at the off-peak price, since it sends only then; with the gate off at the peak price). The hold is
replaced by the real cost when the call ends, so a cap can be full of holds while the money spent is
a fraction of it, and a retry after an answer was cut off at the token limit is held at the larger
limit. A call that a cap cannot hold only because of other calls' holds does not fail: it waits for
them to end. A one-call route prepares, and so holds, a call only when a worker is free to send it,
so it never holds more than `workers` calls' worth. A cap meant for many retries wants room for
the holds of the calls that are in flight together, not for the cost of all of them.

`controller.format_status()` prints spend per
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
  writer saw, the rules version, and the package version (the version of this package that built the
  request; the task text is not inside the rules version, so this says which task text a row came from).
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

**Reviewer settings** (`review.reviewers`, a list with one object for each reviewer):

| Key | Meaning |
|---|---|
| `id` | The reviewer's name, used in the paths of its packets and verdict files (`packets/<id>/`, `review/<id>/`): letters, digits, `.`, `_` and `-`. Each id is used once |
| `family` | The model family the reviewer belongs to (`anthropic`, `openai`, `google`, `deepseek` and so on). The policy asks reviewers of different families for a row, and the family decides which pictures a packet carries (see "Which pictures go where") |
| `enabled` | `true` (the default) or `false`. A reviewer that is off is given no packet. A packet already addressed to it that it has not answered is withdrawn, and its rows go to reviewers that are on (see "Switching a reviewer off or on") |
| `solo_keep` | `true` (the default) or `false`. With `false`, a keep from this reviewer never accepts a row on its own: when the only keeps on a row come from such reviewers, the row is in question and goes on to a further reviewer, as a flagged keep does. At least one keep from a reviewer that may keep alone is needed to accept it (see the policy below) |

Any other key is refused, and the reviewer commands read the same configuration file, so a reviewer's
machine needs a version of the package that knows `solo_keep` to read a file that sets it. A configuration
whose enabled reviewers all have `solo_keep` false is noted when the run starts: no keep could accept a
row, so a row would be discarded by two rejects or end in the hand queue.

**Policy** (the protocol with the default settings; the `review.policy` keys follow it):

1. The first review goes to a reviewer from a family other than the row's writer family, taken in
   turn from all available reviewers.
2. A `keep` with no assessor flag accepts the row, unless it comes from a reviewer that may not keep
   alone (`solo_keep` false): that keep puts the row in question, as a flag does.
3. A `fix` or `reject`, or an assessor flag, puts the row in question. It goes to a further reviewer
   from a family not yet used on it. With `fix_to_hand` true, a `fix` sends the row to the hand queue at
   once instead (after the checks of item 4: a row that two keeps accept or two rejects discard is
   settled that way).
4. Two keeps accept a row (`keeps_to_accept`), when at least one of them comes from a reviewer that may
   keep alone; two rejects discard it (`rejects_to_discard`). A reject counts whoever gives it.
5. After three reviewers with no settlement (`max_reviewers`) the row goes to the hand queue
   (`row_escalated`, then `row_to_hand`). The same happens when no reviewer from an unused family is
   left, and when no reviewer from another family than the writer's is there for a first review; those
   two rows are looked at again (see "Switching a reviewer off or on").
6. A random sample of accepted rows (`audit_fraction`, default 10%, chosen by a hash of the row id so
   it is reproducible) goes to one more reviewer of another family. The audit is recorded and changes
   nothing unless it rejects, which puts the row back in question.

The policy settings (`review.policy`):

| Key | Default | Meaning |
|---|---|---|
| `keeps_to_accept` | 2 | How many keeps accept a row that is in question, at least one of them from a reviewer that may keep alone |
| `rejects_to_discard` | 2 | How many rejects discard a row |
| `max_reviewers` | 3 | After this many reviewers with no settlement the row goes to the hand queue |
| `first_keep_accepts` | `true` | A single keep from a reviewer that may keep alone accepts a row with no assessor flag. Off, every row needs `keeps_to_accept` keeps |
| `fix_to_hand` | `false` | With `true`, a row with a `fix` verdict goes to the hand queue as soon as the verdict is read, with the reason "a fix verdict: the row waits for the owner", instead of to a further reviewer. Reviewers never edit a row, so a fix can only be made by the owner (or, with `repair.enabled`, by the row's writer, see "The repair step"); with only two reviewer families on duty the further review ends in the hand queue anyway, and this skips it. The accept and discard rules (`keeps_to_accept`, `rejects_to_discard`) are checked first and still win. A row with no `fix` verdict is not touched |
| `audit_fraction` | 0.10 | The share of accepted rows that get an audit review |
| `audit_seed` | `"audit"` | Text hashed with the row id to choose the audit sample, so the sample is the same on every run |

Every verdict is kept with the reviewer's id and family.

**Switching a reviewer off or on.** The reviewers a run uses are the ones its configuration lists with
`enabled` true, so a reviewer is switched off by `"enabled": false` (or by leaving it out of the list)
in the configuration the next run starts with, and on again the same way. Two things follow, on each
pass of the controller:

| When | What the controller does |
|---|---|
| A packet is addressed to a reviewer who is off or no longer listed, and no verdict file has been pushed for it | It records `packet_withdrawn` (`packet_id`, `reviewer_id`, `row_ids`), deletes the packet's files from the state store, and puts each row of the packet back to waiting for its review: a row in its first review is awaiting a first review again, a row in a further review is in question again, and an audit is due again. The reviewer that was off is not counted among those that reviewed the row, so its family is free for the row. The rows are then given to reviewers that are on, in packets with ids of their own |
| A verdict file exists for the packet (even one that answers only some of its rows) | The packet is not withdrawn. The verdicts are read as usual and the controller waits for the rest |
| A verdict file turns up for a packet that was withdrawn | It is ignored: the rows were sent on. The run notes it once in its problems. The file stays where the reviewer put it, since the controller does not write under `review/` |
| A row is in the hand queue because no reviewer from another family than the writer's (a first review), or none from an unused family (a further review), was available, the review policy as it stands now still asks for a review of it, and the configuration has a reviewer that can take that review | It records `row_reopened` (`row_id`, `call_id`, `reason`). The row is awaiting a first review if it never had one, and in question for a further review if it did, so the usual assignment gives it to a reviewer, and it leaves the hand queue file |
| A row is in the hand queue for want of a reviewer, but the policy as it stands now settles it (it would accept or discard it, or hand it to the owner: a `fix` verdict with `fix_to_hand` true, or `max_reviewers` reviewers already) | It stays in the queue for the owner, whoever is available. A row beside it that the policy still asks a review for is reopened |
| A row is in the hand queue for another reason (three reviewers without a settlement), or the owner has answered it (`hand_resolved`) | It stays where it is. The owner's decisions are read before rows are looked at again |

With every reviewer off, rows wait (they are not sent to the hand queue) until a reviewer is on.

**The hand queue.** The controller lists rows waiting for the owner in `hand/queue.jsonl`, with the
row, the task's spec line, the assessor flags and every verdict. A row that a repair wrote has one more key,
`repair_of` (the id of the row it replaces), and a row whose repair came to nothing has `repair` (the
`calls` and `rows` of that repair); see "The repair step". The owner answers by adding a file
under `control/hand/` in the state store, one decision per line:

    {"row_id": "...", "decision": "accept"}
    {"row_id": "...", "decision": "discard"}
    {"row_id": "...", "decision": "replace", "row": {"thoughts": ["written by hand"]}}

A `replace` row is stamped with the human writer label and the row's source label. Decisions for
rows not in the queue are ignored; a decision that cannot be used is reported once. A row that is
in the queue only because no reviewer was available for it leaves the queue by itself, as a reopened
row, when a reviewer that can take it is switched on and the review policy still asks for a review
of it (see "Switching a reviewer off or on"); the decisions in `control/hand/` are read before that,
so an answer the owner has given wins.

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
verdicts (recorded as `packet_burned`), or with the whole packet when it is withdrawn (recorded as
`packet_withdrawn`). Reviewers delete their local copies when they push. A reviewer that holds a local copy
of a packet when it is withdrawn can still push its verdicts; the controller ignores that file.

## The repair step

A reviewer never edits a row, so a row with a `fix` verdict can only be mended by someone else. With
`review.policy.fix_to_hand` the row waits in the hand queue for the owner. The repair step lets its writer
mend it instead: the row is sent back, once, to the route that wrote it, with the reviewers' notes, and the
writer writes it again. The step is off unless the configuration turns it on:

```json
"review": {"policy": {"fix_to_hand": true}},
"repair": {"enabled": true, "min_rows": 20},
"routes": {"luna-sync": {"provider": "openai", "model": "gpt-6-luna", "kind": "api", "budget_usd": 50,
                         "purpose_caps": {"production": 40, "rebound": 8, "repair": 4}}}
```

| Setting | Meaning |
|---|---|
| `repair.enabled` | `false` unless set. Off, no row is sent back, and a fix verdict ends in the hand queue as it always did |
| `repair.min_rows` | 20 unless set; at least 1. How many candidates a route gathers before its repairs are sent. Repairs of a batch route go out together, so a batch is worth waiting for; when the controller has nothing else to do for the route, the candidates it has go, however few |
| `purpose_caps.repair` (a route's) | The most the route may spend on repair calls. A route with none, or a cap of nothing, is never sent a row to repair, and the run says so when it starts (with the step on) |
| `review.policy.fix_to_hand` | Has to be `true`: the step sends back the rows that wait in the hand queue for a fix verdict, and no row waits there for one otherwise. A configuration that turns the step on without it is noted when the run starts |

**Which rows, and when.** A candidate is a row in the hand queue for the reason `fix_to_hand` gives ("a fix
verdict: the row waits for the owner") that the owner has not answered, that was never sent back before, that
is not itself the result of a repair, and that was written by a route that is switched on, is not closed for the
run and has a repair cap. The owner's decisions are read first, in the same pass, so a row the owner answers is
never repaired. Each route is looked at on its own: with at least `repair.min_rows` candidates, or with fewer
when the controller has nothing else to do for the route (no call of it in flight or waiting to go, no spec line
of it due a call, no row it wrote still to be checked), the controller records `repair_requested` (`row_id`,
`call_id`, `route`) for every candidate of the route, in row-id order. The row leaves the hand queue file, and the
call the event names is prepared in the next pass. A run that stops between the events of a batch finds the rest
of the batch still in the hand queue and sends it with the next batch, or when the route has nothing else to do.

**What the writer is told.** The call is for the same spec line and goes through the same route, so the writer
gets the same task text, the same picture and the same choices as the first time, then this (a profile's retry
block is not used: that block is about failed checks):

    An earlier row for this item was sent back by a reviewer. The notes from the review are below. Write the row
    again: fix exactly the problems the notes name, and keep everything else in the earlier row that was right.
    Change nothing the notes do not mention, and add no fact the item does not give.

    The notes from the review:
    - <the notes of one fix verdict>
    - <the notes of another>

    The earlier row, as it was written:
    <the row's JSON, without row_id, writer and source_label>

    Answer in the form the task above asks for, with the problems fixed: the complete result, not a patch and
    not an explanation.

The call's `feedback`, as `call_prepared` records it and the writer's request carries it, is the notes of every
`fix` verdict on the row, one item each, then the row as written as the last item. The notes are the reviewers'
words alone: no reviewer's id or family, and nothing from a `keep` or `reject` verdict. A reviewer's note names
the problem and where it is and never holds corrected text (the reviewers' guide says so); a fix that came with
no note gives its reason codes in its place. The call is charged to the purpose `repair`, is the next attempt
number of the line that is free (2 for a line that was written once), and is recorded with `repair_of` set to the
row. A repair call is not one of the line's own attempts: a line's retries do not count it and cannot take its
number.

**What comes back.** The rows the writer answers with are new rows, each with an id of its own (made from the
call, like any row's), linked to the row that was sent back by `repair_of` in the state and the events, and
nowhere in a stored, packeted or shipped row. They are checked like any row, and a rejected one is not written
again. A row that passes goes to a first review by a reviewer outside the writer's family, preferring one that
did not review the row it replaces; an accepted new row ships in the same file as any accepted row of its task.
A new row that a reviewer sends back with a fix goes to the hand queue and is never sent back again (the file
lists it with `repair_of`).

| What happens to the repair | The row that was sent back | Recorded as |
|---|---|---|
| A row the writer wrote passes the checker | `repaired`. It never ships, and its line stands where the new rows stand | `row_assessed` (passed) |
| Every row the writer wrote is rejected or set aside by the checker, and none is written again | In the hand queue again: "the repair failed the checker: the row waits for the owner" | `row_rejected` with `retry` false, then `row_to_hand` |
| The writer answered with no row (a skip) | In the hand queue again: "the repair gave no row, the writer set it aside instead of writing it again: the row waits for the owner" | `row_set_aside`, `row_to_hand` |
| The call failed in a way no retry mends (a refusal, a block, a request the provider refuses, a picture that cannot be read) | In the hand queue again: "the repair call failed (kind): the row waits for the owner" | `call_failed`, `row_to_hand` |
| The call failed for a passing reason (a provider error, an expired batch, an answer cut off at the token limit) | The call is made again with the same notes and row, as the next attempt number that is free, while the row has had fewer than `max_attempts` repair calls (a cut-off answer gets the larger token limit). Charged to `repair` like the first | `call_failed`, then `call_prepared` with `retry_of` |
| The step, or the route's repair cap, is switched off after the row was sent back and before its call was made | In the hand queue again: "the repair was not made, the step or the route's repair cap is switched off: the row waits for the owner" | `row_to_hand` |
| A repair call the repair cap cannot hold yet | `repairing`: the call waits, as any call does, until calls in flight give back what they hold | nothing is recorded until it goes |
| A repair call the repair cap could never hold | `repairing`: the call is left alone for the rest of the run and asked again by the next one, so raising the cap takes effect then | `budget_refused` with `purpose` `repair` |

A row that came back to the hand queue after its repair is never sent back again, and the owner answers it as any
row in the queue. The progress line counts the rows being written again (`N rows sent back to their writers`) and
`format_status()` adds the line `rows sent back to their writers: N being written again, M written again`.

An item that holds several rows (a series) is asked for as a whole: the repair call carries the item's spec
line, and the writer may answer with every row of it. Each row of the answer is a new row linked to the row
that was sent back, and is checked and reviewed like any row; the rows the first answer gave that were not sent
back stay as they were. A repair that asks for one row of a series only is not built.

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

### Trying the row schema on Gemini

The dry run sends a tiny schema, so it cannot say whether Gemini takes the real one. `rps.geminiprobe`
does: it builds the request the Gemini writer prepares for a real spec line (the rule pool's system
prompt and the task text of its first worked example, or a spec item you pass as `item=`; the
route's thinking level and token limit) and sends it as one `generate_content` call for each form of
the row schema, `exact`, `plain` and `none`, in that order, stopping at the first one taken.

    python -m rps.geminiprobe --route flash-batch

```python
from rps.geminiprobe import probe_gemini
report = probe_gemini("flash-batch")           # report.accepted_mode, report.outcomes
```

A request Gemini refuses is not billed and comes back with the provider's whole error text, which a
batch item does not carry; the tool prints it. A request it takes is billed as any call, so the tool
refuses to start when the calls may cost more than `--cap` (default $0.25) and `--all` (every form, not
only up to the first taken) is opt-in. It ends by saying which `schema` option to set. A form that the
one-call endpoint takes is not proof for the batch service, but a form that is refused here is refused
there for the same reason. The exit code is 0 when a form was taken, 1 when every form was refused and 2
when the tool refused to start.

### Finding what the Gemini batch service refuses

The batch service validates a request apart from the one-call endpoint and reports a refusal only after
the batch has run, as a code and one sentence for each item. `probe_gemini_batch` sends the writer's
request as batches of one item, all created at once and polled until each has an answer, then cancels
the ones that have not. The variants, in the order sent:

| Variant | What differs from the request as the writer sends it |
|---|---|
| `as_sent` | nothing: the request exactly as the batch writer builds it for the route |
| `no_schema`, `no_system`, `no_thinking`, `no_metadata` | the schema, the system instruction (the rules), the thinking setting, or the metadata left out |
| `cache_off`, `cache_on` | the rules in the request, or named by an explicit cache. `cache_off` is tried when the route's `cache` is on; `cache_on` only with `--with-cache` on a route whose `cache` is off (the cache is made for the probe and deleted after) |
| `schema_field`, `metadata_key` | the schema (plain form) in `response_schema`, or the metadata named `key` |
| `openapi` | the schema in its OpenAPI form (no `additionalProperties`) in `response_schema`; the metadata and the rest as sent |
| `documented` | the form the batch pages show: the OpenAPI form in `response_schema`, the metadata named `key`, no cache |
| `bare` | only the task text and the token limit |

    python -m rps.geminiprobe --route flash-batch --batch
    python -m rps.geminiprobe --route flash-batch --batch --only as_sent,no_schema,documented --wait 600

```python
from rps.geminiprobe import probe_gemini_batch
report = probe_gemini_batch("flash-batch")      # report.ok, report.outcomes, report.verdict("no_schema")
```

For each variant it prints the provider's whole error (the code, the message and the details) or that
the request was taken. It ends in plain sentences: which part the service refuses, with the route option
that fixes it (the variants taken with only one part changed); or that two or more parts are refused
together; or that even the bare request is refused, so no part of the request is at fault and the model or
the key is not taken for inline batches; or that the request was taken as the writer sends it, so a refusal
in a run has its cause outside the request. `--api-version v1` or `v1beta` (or the route's `api_version`) chooses the version of the Gemini API the batches go to, and the first line printed says which one the run used, so two runs can be told apart; a run that makes a cache (`--with-cache`, or a route whose `cache` is on) goes to `v1beta`, the only version with caching, and refuses `v1`. `--compare-model MODEL` (repeatable) also sends the bare
request to another model, to tell a refusal that comes from the model from one that comes from a part of
the request. A refused item costs nothing; a taken one is billed at the batch rate, and `--cap` (default
$0.25) bounds what all the variants may cost (the tool refuses to start above it). The exit code is 0 when
the service took the request as the writer sends it, 1 when it did not and 2 when the tool refused to start.

## Resuming

| If the run stops here | Then the next run |
|---|---|
| before a batch id was recorded | asks the provider for batches matching the prepared calls and adopts every one it finds (`Writer.recover`, told when the calls were prepared and which batches are already recorded), else submits. OpenAI and Gemini find a batch by a digest of its calls; Anthropic by the batch's size and creation time, with a check at collection that it held the calls (see above) |
| after a submit that failed on the way back from the provider (same run) | looks for the batch the same way before it sends the calls again, so a batch the provider did take is adopted and not paid for twice |
| after `batch_submitted` | polls that batch; it never submits the same calls again |
| while a one-call route has calls in flight | records the calls that finished (they were recorded as they landed) and sends the ones that did not as rebounds. At most `workers` calls were in flight; the rest of the list had not been prepared yet and is simply taken up in its turn |
| while a one-call route is paused for a rate limit | starts with the route open again; the calls that were waiting out the pause come back as rebounds, like any call that was in flight, and the lines not yet prepared are taken up in their turn |
| after the tasks file changed (or is gone) | reads the file again on the next pass; tasks already recorded are ignored, a missing file in the state store means no tasks |
| between recording results and cleaning up | the results are durable first; a clean-up that did not happen (OpenAI's files) is left to the files' expiry |
| between writing a packet's files and announcing it | writes the same packet again (the id comes from its rows) and announces it once |
| between recording a packet's withdrawal and deleting its files | deletes the files of every withdrawn packet that is still in the state store, once when the run starts (the event comes first, so no row waits for a packet whose files are gone) |
| after rows were reopened or packets withdrawn | rebuilds the same state from the events alone: neither depends on the configuration of the run that recorded it |
| between two `repair_requested` events of a batch | the rows already sent back are `repairing` and their calls are prepared in the next pass; the rows not yet sent back are still in the hand queue, and go with the next batch or when the route has nothing else to do. No row is sent back twice |
| after `repair_requested` and before the repair call was prepared | prepares the call the event named (its id was recorded with the event), once |
| while a repair call is in flight | polls it. A call on a one-call route that the stop cut off fails for a passing reason, like any call in flight, and is made again as the next repair call (charged to `repair`) while the row has had fewer than `max_attempts` of them. A repair call is never made twice for the same attempt number |
| between a repair coming to nothing and the row's return to the hand queue | works it out again from the events (every row the writer wrote was rejected, the writer gave no row, or the call failed for good) and records the `row_to_hand`, once |
| while shipping | rebuilds the shipped files from the accepted rows and hands them over again; `shipped` is recorded only after the ship store has sent the files (for a dataset repository, after the commit has gone out). Files that were waiting for a commit when the run died are simply handed over again |
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
  the writer as `code: message` on the one retry; flags are bare codes for the reviewers. A function
  that raises does not stop the run: the row (every row of the call, for a call-level function) fails
  with the exception as its error line. The
  context carries `assignment`, the runner's choices for the call as plain data (for the rule pool
  `{"relation_part", "unknown_keys", "wordings"}`, the same values the task text told the writer).
- **Screen.** A function of one spec line (`ScreenFn`: the line's spec as the tasks file gave it) that returns
  the reason to set the line aside, or `None`/an empty string to send it on. The controller asks it once for a
  line that has no call yet, in the pass that would build the line's first call, and never for a line that is
  due a retry. A reason is recorded as `line_screened` (class `other`; the reason is kept to 300 characters)
  and the line is finished with the outcome `screened`: no call is built, prepared, submitted or paid for,
  and no row exists for it. Read it with `State.screened_lines()`, `lines_screened` in `job_progress()` and
  `screened_lines` in `counts()`; none of these is mixed with `lines_set_aside`, the lines whose writer
  skipped them or whose rows the assessor set aside. Pass `build_controller(config, screen=...)` to
  replace the rule set's own (the rule pool brings `rps.assessor.screen_item`), or name one in the
  configuration with `"rules": {"screen": "module:name"}` (`"none"` turns the pool's off). A screen that raises
  does not stop the run: the line goes to the writer and the failure is noted once in the run's problems.
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
- The repair step writes a whole item again. A row of a series (an item that holds several rows) that a
  reviewer sent back is repaired by asking the writer for the whole item once more, with the notes for the
  row; the rows of the first answer that were not sent back stay as they were, and every row of the new answer
  is checked and reviewed. Asking for one row of a series alone is not built. A row can be sent back once; a
  second round, or a repair done by a different route than the row's own, is not built either.
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
- Whether the Gemini batch service takes the `openapi` form of the row schema is not known: the probe of
  0.2.5 named the schema as the refused part and the rejected name, and this form leaves that name out, but
  it has not been run against the service. `python -m rps.geminiprobe --route flash-batch --batch --only
  as_sent,openapi,documented,no_schema` (and the same with `--api-version v1beta`, since the SDK defaults to
  `v1` and the first form's refusal has not been tried on the other version) shows it. Until then a route is
  best left on `batch_form: auto`.
- The explicit cache has not been made against the live service since it was moved to `v1beta`.
