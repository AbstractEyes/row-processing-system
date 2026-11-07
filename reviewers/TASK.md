# Task: review rows for the row processing system

## What you are doing

A controller writes dataset rows through several model APIs. Each row describes a picture in short thinking steps, then
gives a caption, a prompt, tags and sometimes a critique. Before a row is accepted, reviewers from different model families
read it. You are one of them. You never write rows.

## For the person starting the session

| Step | What to do |
|---|---|
| 1 | Open this repository's `reviewers` folder as the workspace of the agent (Antigravity, Codex or Claude Code), with the repository installed in the active environment (`pip install -e .` once) and a token scoped to the review bucket alone in the environment (`HF_TOKEN`) |
| 2 | Say: "Read TASK.md and AGENTS.md. Your reviewer id is `<id>`. Review the next packet." |
| 3 | Watch the session; stop it whenever you like. A later session continues from its verdict file |

## For the reviewer

1. Read AGENTS.md (the process and the hard rules) and the review brief it names (the rules you judge by).
2. Pull one packet, judge every row in it, record one verdict per row, check the verdict file, push.
3. Report back in one line: the packet id and how many rows you kept, marked for fixing and rejected.
4. Ask for the next packet only when told to.

## What a good review looks like

| Good | Not good |
|---|---|
| One verdict per row, each from reading that row, its task and its picture | "All rows look fine" |
| Reason codes from the brief's list, and a note that says where the problem is | A note that rewrites the row |
| fix when unsure | keep when unsure |
| Reporting a picture that does not match its row | Ignoring the picture |
