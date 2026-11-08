# Reviewing rows

You review picture-description rows that other models wrote. You return a verdict for each row. You never write, edit or
rewrite a row.

## Your session

- Start in this folder. Read this file, then `../src/rps/rules/review_brief.md` (the rules you judge by). The packet's
  `brief_version` must equal `../src/rps/rules/VERSION`; if it does not, stop and say so.
- Your reviewer id is set by the person running the session (for example `codex-sol`, `gemini-argon`, `claude-sonnet`).
- Work one packet at a time (`<config>` is the run's configuration file, `work` your local folder):

| Step | Command |
|---|---|
| Get the next packet | `python -m rps.review pull --reviewer <id> --config <config> --into work` |
| See what is left | `python -m rps.review status --reviewer <id> --config <config> --into work` |
| Record a verdict | `python -m rps.review verdict --packet <packet> --row <row_id> --verdict keep\|fix\|reject --reasons <code,code> --notes "<text>" --into work` (`--replace` replaces an earlier verdict for the row) |
| Check your verdict file | `python -m rps.review check --packet <packet> --into work` |
| Send the verdicts | `python -m rps.review push --packet <packet> --config <config> --into work` (this also deletes the packet's pictures from this machine) |

## Judging a row

For each row in `packet.json`: read the row, read its task (the specification and, for rows that start from a real
picture, the hint text), and look at the picture when the row has one. A row marked `"image_withheld": true` has a
picture your model family may not see (the pictures of some datasets go to no outside reviewer): judge it from its text and its task, and never fault
it for something only the picture could show. Then decide:

| Verdict | Meaning |
|---|---|
| keep | The row is right as written. |
| fix | The row is sound but something specific is wrong. Give the reason codes and say where. |
| reject | The row should not exist: it invents, contradicts its picture or task, should have been skipped, or breaks a hard rule. |

When you are unsure, choose fix with a note, not keep: an uncertain row goes to another reviewer.

## Notes

- A note names the problem and where it is: "thought 3 defines 'chiaroscuro' instead of looking at the picture";
  "the caption says a dress, the task records a skirt and a blouse".
- A note never contains a rewritten row, a replacement sentence or a corrected value.

## Hard rules

- Verdicts only. Never change a row, a task or a picture; never create rows.
- Judge every row on its own and in the same way. Packets may contain test rows.
- Never copy rows, tasks, hints or pictures anywhere else. Never upload a picture. Pictures are deleted after `push`.
- Some pictures show people. Never try to identify anyone; never describe a person beyond what the row needs.
- Never open another reviewer's folder or verdict file.
- Stop when the person running the session says stop. Your verdict file lets the next session continue where you left off.
