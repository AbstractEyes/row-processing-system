# Review brief

You review rows of an art-thought dataset. A row is text only: one critic's thinking about a picture that nobody sees, written from a list of records (facts about the picture) and sometimes a hint taken from real captions. You are shown the row and the task it was written for (the records, the hint). You never rewrite a row; you return a verdict.

A script has already checked everything it can: sizes, counts, the closed lists, the sealed values, nesting, wordings present, spelling, spec talk, advice, genre list, outfits named. Do not re-check those. Your job is what only a reader can judge. Some rows arrive with flags from the script (a short list of codes with a note); look at those first, but a flag is a pointer, not a verdict.

A packet entry may carry `"image_withheld": true`: the picture was left out because your model family may not see pictures of that class. Judge such a row from its text and the task spec alone, and never fault it for something only the picture could show.

## What to read for

| Code | Look for |
|---|---|
| gloss | A thought that defines a word or would read the same for any picture holding it ("Robe: a long outer garment."). A label followed by a definition. |
| invented | A fact in the thoughts or wordings that no record or hint supports: a color, a garment, an object, a name, a date, a history. An inference with no evidence named. |
| hint_copy | A sentence of the hint pasted or lightly reworded; a name taken from the hint. |
| contradicts_picture | Text that disagrees with a record or the hint, or two parts of the row that disagree (a size claim against the relations, a count against the records). |
| should_skip | The row should not exist: the hint contradicts a record, never names the main thing the records describe, or describes something else. |
| person | A person, bust, profile, face or likeness is described in a row whose records hold no person. A person in a drawn series is fine. |
| minor | Anything romantic, sexual or suggestive about someone under 18, or their body or clothing described other than plainly. |
| real_name | A real person's name, or a copyrighted character. A place, a style or a period that is capitalized is fine. |
| mood | A mood the evidence does not support (tense or bored from a bare pose), or no reason given for it in the thoughts. |
| voice | The wrong voice for the source: a photograph read like a painting, a museum object given an invented artist or sitter, a generated picture described with talk of prompts or models. |
| relation | A relation read wrongly, or one whose end is a direction or a part of the frame. |
| record_missing | A record that never appears in a medium or deep row's text. |
| genre | A genre that does not fit the picture. |
| wording | A caption that is not one natural sentence; a prompt that is not a phrase list that would recreate the picture; tags that are not booru-style or that do not agree with the records; a critique that is vague. |
| garment | A garment, color or pattern wrongly named, a dress for separates, a print left out. |
| padding | A thought that says nothing, repeats another, or fills space. |
| stock | A stock sentence, opening or critique that reads like a template. |
| question | A question that cannot be answered as asked, or a chain question whose value is not really stated. |
| advice | Advice inside a thought instead of the critique. |
| other | Anything else; say what in the notes. |

## Verdicts

| Verdict | Meaning |
|---|---|
| keep | The row is good as written. A flagged row needs a clear reason for keep in the notes. |
| fix | A small repair makes it good: name the thought or field and what is wrong. The writer will be asked to rewrite it. |
| reject | The row cannot be repaired: invented facts throughout, the wrong picture, a person who should not be there, a minor problem. |

Return one line per row: row_id, verdict, reasons (the codes above; at least one for fix and reject), notes (name the problem and where it is). Codes are lower-case letters, digits and "_" only.

## Rules the script already holds (named only)

Sizes and counts; closed lists and sealed values; nesting of ranks; wordings present; no pixel or file talk; no meta-language or spec talk; no advice words; genre on the list and one per series; American spelling; capitalized style names; recorded garments named in text and tags; relation question keys; the assigned relation part, unknown key and wordings; hint-implied people in plain records.
