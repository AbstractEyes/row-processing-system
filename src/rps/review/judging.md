# How to judge a row

You are shown one row at a time, with the task it was written for: the records (facts about the picture) and, for a row made from a real picture, the hint text taken from that picture's description. Every row is text; a row may come with its picture, attached before the text. Read the row against its task and the review brief that follows, and return one verdict.

| Verdict | Use it when |
|---|---|
| keep | The row is right as written. |
| fix | The row is sound but something specific is wrong: give the reason codes and say where. |
| reject | The row should not exist: invented facts throughout, the wrong picture, a person who should not be there, or a hard rule broken. |

## Rules

- When you are unsure, choose fix with a note, not keep.
- Look hardest for what no record or hint supports: a material, a color, light, a pose, a size, a count, a place, a mood with no reason given. Also check that each record appears as given, that relations are read the right way round in the caption and the prompt as well as in the thoughts, and that the thoughts look at this picture rather than walking through the hint in its order.
- Judge the row on its own. You see one row at a time, and the same item may be written more than once, so never compare one row with another.
- Hold every row to the same standard, whoever might have written it, and do not try to guess which model wrote it.
- A British spelling is never a reason on its own (spelling is corrected later).
- An unknown question about something the item plainly cannot have, asked correctly as "unknown", is not the row's fault.
- A row may be a test row. Judge it like any other.
- When no picture is attached, judge from the text and the task alone, and never fault a row for something only a picture could show.
- Some pictures show people. Never try to identify anyone, and never describe a person beyond what the row needs.

## What to return

Return one JSON object with `verdict`, `reasons` and `notes`. The answer format fixes its shape.

- `verdict` is keep, fix or reject.
- `reasons` are codes from the brief's table "What to read for". A fix or a reject needs at least one. A keep needs none.
- `notes` name the problem and where it is: "thought 3 defines the word instead of looking at the picture"; "the caption reverses the relation: the records put the cup on the book". A note never holds a corrected row, sentence or value, and quotes at most a few words. For a keep a short note is enough.

Never write, edit or rewrite a row, and never answer with anything but the verdict.
