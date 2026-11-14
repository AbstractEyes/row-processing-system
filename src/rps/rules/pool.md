# Rule pool

The one list every compiled file is built from. Each rule is numbered once. Every rule binds the writer; the tag says who can check it:

- ASSESSOR: a script checks it (the code names what the check reports). A failing row is rejected with the error line, and the writer gets one retry.
- READER: only a reader can judge it. Reviewers see the rule in the review brief.
- WRITER: nothing checks it; the writer must simply do it (and the runner assigns what a single call cannot see).

"validator" is the dataset's row validator, called as a library. Its codes are `rule_N` (the validator's own rule numbers 0-8), `ids`, `specs`, `license`, `spec_kept`, `light_removed`, `source`, `series` and `missing`.

## A. Shape of a row

| # | Rule | Tag | Code |
|---|---|---|---|
| 1 | The writer's output is one json object: skip, reason, class, rows. A series is three rows (rank 1 simple, 2 medium, 3 deep) of one picture; a single is one row. Ids and series id are the ones given. | ASSESSOR | ids, series, missing |
| 2 | A row has id, series, depth, rank, domain, genre, records, relations, thoughts, wordings, questions. The runner adds held and source; a model never writes them. | ASSESSOR | rule_0 |
| 3 | A record value is plain lower-case words of at most 20 bytes; key and value together at most 27 bytes. | ASSESSOR | rule_2 |
| 4 | Record counts, a relation counting as one record: simple 1-4, medium 6-14, deep 14-30. | ASSESSOR | rule_3 |
| 5 | Thought counts: simple 1-2, medium 3-5, deep 6-10. Question counts: simple 1-2, medium 3-5, deep 5-8. | ASSESSOR | rule_3 |
| 6 | Thoughts fit the depth's frame in 32-byte slices (simple 8, medium 16, deep 64); a thought costs ceil((7 + len(step) + len(text)) / 29) slices. | ASSESSOR | rule_3 |
| 7 | A thought's step is one of the step kinds; its text is not empty. | ASSESSOR | rule_1 |
| 8 | Medium rows carry caption, prompt and tags; deep rows add the critique; a simple row carries one wording (the caption). | ASSESSOR | rule_5, wording_missing |
| 9 | Banned in thoughts and wordings: pixel(s), "the image file", "I can see", "as seen in the image", "in this photo I", "looking at the image", "the jpeg", "the png". | ASSESSOR | rule_1 |
| 10 | The row's license, source kind and seed id follow the item; the runner stamps them. | ASSESSOR | license, source |
| 11 | A series' ranks are simple, medium, deep, nested: each lower rank's records appear word for word in the next, relations keep their order (a relation counts as three records here), and interpretive records repeat verbatim upward. | ASSESSOR | rule_7, series |
| 12 | Every record of the given spec stays in the row, unchanged. A seeded spec is never adjusted. | ASSESSOR | spec_kept |

## B. Records and the closed lists

| # | Rule | Tag | Code |
|---|---|---|---|
| 13 | Values come from the closed lists wherever a list exists. | WRITER | |
| 14 | Sealed values never appear in a record, a relation end, a question value or a tag item. Prose may use any word. | ASSESSOR | rule_7 |
| 15 | Simple rows add no interpretive records; medium adds mood (feeling, from); deep adds mood, intent (purpose, for) and story (moment plus one of before, after, cause). | ASSESSOR | rule_3, rule_6 |
| 16 | "mood from" is the key of a record of the same row. | ASSESSOR | rule_6 |
| 17 | A seeded mood needs evidence in the records or the hint; with none, take the plainest honest reading (gentle, cozy, hopeful, wistful) and hedge it. | READER | |
| 18 | Mood evidence varies: light, time, palette, pose, gesture, state, setting or weather as often as the face; a relation, a position, a count, an angle, a framing or a name never carries a mood. | WRITER | |
| 19 | Facts without a key go in only as a relation end when a true relation carries it (within 6 relations); otherwise they stay out of the records and the text. | READER | |
| 20 | A key the hint never mentions is still written; silence is not contradiction. | READER | |

## C. Thoughts

| # | Rule | Tag | Code |
|---|---|---|---|
| 21 | Every thought looks at this picture. No gloss: no sentence that would read the same for any picture holding the word. | ASSESSOR | gloss (flag) |
| 22 | No thought opens with a label formula ("Count one:", "Colour:") or a stock opening ("Check the X:", "Compare X with Y:"). | ASSESSOR | label_opener, stock_opening |
| 23 | The name step names what a thing in this picture is. | READER | |
| 24 | A simple row never pads; a second thought relates records to each other. | READER | |
| 25 | Every record surfaces in the text of a medium or deep row at least once. | READER | |
| 26 | Every fact is a record or an inference marked in plain words with its evidence; no invented detail. | READER | |
| 27 | No meta-language: record, spec, inference, "read from", "an inference from the", "the mood is read from". | ASSESSOR | formula_word |
| 28 | The text never says what something "names", "gives" or "states", never "the spec" or "the records", never talks about describing or the headline word, never names spec categories. | ASSESSOR | spec_language |
| 29 | No advice in a thought ("A painter would ..."); advice only in the critique. | ASSESSOR | advice |
| 30 | A relation is read as the picture would show it; size and order claims agree with the relations. | READER | |
| 31 | A relation end names a thing, never a direction or a part of the frame. | ASSESSOR | relation_direction |
| 32 | Step kinds spread: color where colors matter, count where a count matters, a check in each deep row, compare or change in deep rows. | WRITER | |
| 33 | Hard length frame: a thought costs ceil((7 + step name + text bytes) / 29) slices; at most 8 slices simple, 16 medium, 64 deep. Never pad. | ASSESSOR | rule_3 |
| 34 | In a row with no person an imagined passer-by stays conditional. | ASSESSOR | passerby_present (flag) |
| 35 | A recorded role names the person by the role. | READER | |

## D. Wordings and spelling

| # | Rule | Tag | Code |
|---|---|---|---|
| 36 | Caption: one natural sentence. Prompt: comma-separated phrases that would recreate the picture. Tags: lower-case booru tags with colors as booru color words; 1girl, 1boy, 1other, solo only where the person records agree; "high angle", never "from above". | READER | |
| 37 | Critique: one or two specific, craft-based sentences. | READER | |
| 38 | Prose and tags prefer American spelling (color, gray, center, harbor); records keep theirs. British spelling never rejects or flags a row; `americanize_row` corrects it after writing and the report counts it. | WRITER | |
| 39 | Style, period and nationality names are capitalized in prose, caption, prompt and critique, lower-case in tags. | ASSESSOR | style_lowercase |
| 40 | No stock sentence repeated across rows; vary openings and critics. | ASSESSOR | near_duplicate (flag) |

## E. Outfits

| # | Rule | Tag | Code |
|---|---|---|---|
| 41 | Every garment record is named in the thoughts and the tags with its color and pattern. | ASSESSOR | garment_missing_text, garment_missing_tags |
| 42 | The caption names the outfit as recorded; separates are never called a dress. | ASSESSOR | dress_caption |
| 43 | "jeans" is not a tag item; write denim or blue pants. | ASSESSOR | jeans_tag, rule_7 |
| 44 | Prints, logos and words on clothes are named when recorded. | READER | |
| 45 | A person wears separates or a one-piece garment, never both; a lower garment only where the framing reaches the legs, shoes only where it reaches the feet. | WRITER | |
| 46 | No color or garment appears in the text that no record holds. | ASSESSOR | unrecorded_color, unrecorded_garment (flag) |

## F. Questions

| # | Rule | Tag | Code |
|---|---|---|---|
| 47 | A "record" question's value is the record's own value under that key. | ASSESSOR | rule_4 |
| 48 | A "chain" question's value is stated word for word in the thoughts and is not a record of that key. | ASSESSOR | rule_4 |
| 49 | An "unknown" question has the value "unknown" and a key the row's records do not hold. A deep row asks one chain and one unknown question. | ASSESSOR | rule_4 |
| 50 | The relation question uses the key the runner assigns ("rel1 a", "rel1 rel", "rel1 b"), only on a relation the spec holds; the second thing is asked on at most half of a batch's relation questions. | ASSESSOR | relation_key |
| 51 | The unknown key is the one the runner assigns; no unknown key is repeated within a series. | ASSESSOR | rule_4 |

## G. Genre and domain

| # | Rule | Tag | Code |
|---|---|---|---|
| 52 | Genre is on the list (or two listed genres together), never food or underwater, never a setting, time or place word. | ASSESSOR | genre_off_list |
| 53 | One genre per series. | ASSESSOR | series_genre |
| 54 | A given genre stays unless it contradicts the picture or is off the list. | READER | |
| 55 | A seeded row's domain agrees with its medium record (a painting medium is not a photograph). | ASSESSOR | domain_medium (flag) |

## H. Sources, people and names

| # | Rule | Tag | Code |
|---|---|---|---|
| 56 | Hints are evidence, never text to copy; no names from a hint; facts only where captions agree or a narration says it outright. | ASSESSOR | hint_copy (flag) |
| 57 | Voice: photographs get a photo critic's voice; museum works a gallery looker's; generated or written-prompt sources are written as about any picture. | READER | |
| 58 | Real people's names stay out; a capitalized word is a place, a style or a period. | ASSESSOR | names (flag) |
| 59 | Nothing romantic, sexual or suggestive about anyone under 18; plain clothes and bodies, never appraised. | ASSESSOR | minor_fit (fit, appraisal and intimacy words); READER minor |
| 60 | No people in a seeded row whose records hold none: when the hint implies a person (human, figure, portrait, bust, profile, face, likeness, shroud, satyr, nymph, soldier statue, arm, a person's job) the item is skipped, class person. A drawn series may hold a person. | ASSESSOR | person_in_hint |
| 61 | Skip when the hint contradicts a record (class contradiction) or never names the main thing (class ungrounded). | READER | |
| 62 | An object whose decoration is a depicted person stays an object; a tree with a painted face is not a person. | READER | |

## I. Balance (assigned by the runner, never left to the model)

| # | Rule | Tag |
|---|---|---|
| 63 | The relation question key cycles a, rel, b over the batch. | WRITER |
| 64 | The unknown key rotates through a fixed list and skips keys the item's spec holds. | WRITER |
| 65 | Wordings come from the depth. | WRITER |

## J. Faults a reviewer found in rows that passed the first checks

| # | Rule | Tag | Code |
|---|---|---|---|
| 66 | No talk about the records beyond the first word filter: not "recorded", "named here", "specified", and no record value read aloud ("a count of 1"). A ship named X is fine. | ASSESSOR | self_talk, spec_talk (a record key used as a phrase) |
| 67 | No clause hedges the row's own claims ("this is an interpretation", "the scene does not show how"). | ASSESSOR | self_hedge (flag) |
| 68 | A thought adds something the earlier ones did not; the mood is claimed in one thought. | ASSESSOR | check_repeat (error); thought_repeat, mood_repeat, second_mood (flags); READER padding |
| 69 | Light, furniture, size, wear, shot, pose appear only when a record or the hint gives them. | ASSESSOR | unrecorded_thing, unrecorded_light, unrecorded_detail, unrecorded_shot, unrecorded_pose, state_conflict (flags); READER invented |
| 70 | A mood is never read from a relation, an angle, a framing, a count or a position; thin evidence (a name, a color, a state) is hedged; the mood thoughts credit the evidence the mood record names. | ASSESSOR | mood_weak_evidence (relation, angle, framing, count, position: error), mood_unhedged, mood_cause_differs (flags) |
| 71 | A medium row holds a mood record; a deep row holds mood, intent and a story with its moment and one of before, after or cause. | ASSESSOR | required_record, story_incomplete |
| 72 | A chain or unknown question key is a record key; a chain value is a short phrase (at most 20 bytes), never a sentence. | ASSESSOR | question_key, question_value |
| 73 | A year in the hint outside the period record contradicts it: the item is skipped (class contradiction). | ASSESSOR | hint_contradicts_record (sets the item aside) |
| 74 | A position record (center, left third) places a thing in the frame, never at a place in the scene. | ASSESSOR | position_as_place (flag) |
| 75 | A sentence restates the hint when three quarters of its content words come from one hint sentence; one fact with a critic's reading is fine. | ASSESSOR | hint_paraphrase (flag); READER hint_copy |
| 76 | A fact only one of several captions gives is not used as if they agreed. | ASSESSOR | single_caption_fact (flag); READER invented |
| 77 | A step does what its name says: count gives a number; compare, relate and change need two things, a relation or a change. | ASSESSOR | step_count (error), step_compare, step_relate, step_change (flags); READER |
| 78 | Tags use booru color words: never maroon, burgundy, crimson, scarlet, vermilion, peach or golden. | ASSESSOR | tag_color |
| 79 | A record value is not pasted as a tag (wide, layered depth). | ASSESSOR | tag_value (flag) |
| 80 | The caption names every recorded garment. | ASSESSOR | caption_garment |
| 81 | A style or period names the painting, never the scene (not "a Baroque garden"). | ASSESSOR | style_on_scene (flag) |
| 82 | The caption is one natural sentence, not the hint's list of depicted things. | ASSESSOR | caption_hint_list (flag); READER wording |

## K. The writer's template, rebuilt from the third review

| # | Rule | Tag | Code |
|---|---|---|---|
| 83 | Only the facts: a row's text holds its own records, what a real picture's hint states plainly, and the records the rules let it add; a pose, a held thing, light, weather, time, materials, sizes, motion, sound and other people or things appear only when a fact gives them. The task text lists the details the item records nowhere. | ASSESSOR | unrecorded_pose, state_conflict, unrecorded_light, relation_placement, added_record; unrecorded_size, unrecorded_material (flags); READER invented |
| 84 | A shallower row of a series uses no fact that only a deeper row records; for a drawn series the task text lists them row by row. | ASSESSOR | rank_scope |
| 85 | The framing limits what shows: a close-up or an extreme close shows no full-body pose, no feet and no wide view of the place. | READER | |
| 86 | Intent purpose and intent for are about the picture (why it was made, who it is for), never about the people or things in it. | READER | |
| 87 | Story moment agrees with the records; story before and after hold an event and story cause a reason, never a moment word or a time alone. | ASSESSOR | story_slot, story_cause |
| 88 | The mood is claimed in one thought, hedged, with the thing its mood from record holds named in the same sentence. | ASSESSOR | mood_cause_differs, mood_flat, mood_weak_evidence (angle, relation, framing, count, position) |
| 89 | Directions belong to the frame, never to a person ("to his left"); a relation that only matches colors places nothing beside anything. | ASSESSOR | relation_side, position_as_place, relation_placement |
| 90 | The hint's sentences are never followed in their order, the caption is never the hint's first sentence reworded or its list of things, and no name is taken from it. | ASSESSOR | caption_hint_copy, caption_hint_list, hint_name; hint_order (flag) |
| 91 | A chain answer answers its key; the unknown key is answered nowhere in the row, and the task text says what it covers. | ASSESSOR | chain_kind, unknown_stated |
| 92 | The tags carry a person tag whenever a person is recorded and keep each recorded color with its thing. | ASSESSOR | tag_person, tag_solo, tag_color_missing, tag_color_detached |
| 93 | A record that cannot be what it says (a misspelled word, a thing its hint never mentions) is skipped, class other, never written around. | ASSESSOR | screen_item (sets a seeded item aside before writing: a record name one edit from a hint word) |

## L. Rows written with the picture attached

A picture row is one whose writer was sent the picture (the writer label holds the picture's size). Everything else about a row is checked as for a text row.

| # | Rule | Tag | Code |
|---|---|---|---|
| 94 | With the picture attached, the picture is the item itself: where the records and the hint are silent, the picture decides. The writer keeps every given record; when the picture plainly shows one to be wrong, the item is skipped with class picture_conflict and the reason names the record. | WRITER | |
| 95 | The writer may add records for what the picture shows plainly (a setting, the light, the weather, the time of day, a thing's color, state or position, a pose), with values from the lists and inside the depth's totals; "mood from" may name such a record. | ASSESSOR | rule_3, rule_6, spec_kept; added_record (flag on a picture row) |
| 96 | Thoughts and wordings may state what the picture shows plainly even when no record and no hint gives it; on a picture row the checks for a detail no record gives only flag it, with the note to look for it in the picture. | ASSESSOR | unrecorded_pose, unrecorded_detail, unrecorded_shot, unrecorded_light, unrecorded_thing, unrecorded_size, unrecorded_material, unrecorded_color, unrecorded_garment, state_conflict, relation_placement, added_record (flags on a picture row) |
| 97 | A hint that reports what a classifier or another model suggests (for example about the light) is a suggestion, not a fact: the writer checks it against the picture, uses it where the picture agrees, says what the picture shows where it does not, and never writes that a model said so. When the picture shows its light plainly, the row records it (shot light) and a thought reads it. | WRITER | |
| 98 | A detail the picture does not show, or shows differently, is invented or contradicts the picture. A reviewer judges every claim of a picture row against the picture first; a record the writer added must be true of the picture. | READER | invented, contradicts_picture |
| 99 | A reviewer compares the row's light (its shot light record and any thought or wording about light, shadow, contrast or direction) with the picture: a row that follows a classifier's hint against the picture is wrong, one that departs from it because the picture shows otherwise is right; light the picture does not show is invented, light against the picture contradicts_picture. | READER | invented, contradicts_picture |
| 100 | A reviewer whose packet withholds the picture judges the row from its text, as a text row, and never faults it for something only the picture could show. | READER | |
| 101 | On a picture row the hint describes the same picture, so a sentence that says what a hint sentence says is expected: the checks for it only flag the sentence, for a reader to compare with the hint. A reader faults a sentence taken over from the hint (its wording, or its run of facts in the hint's order) or a name taken from it, never the same fact in the row's own sentence; a thought that only lists what is shown is padding. | ASSESSOR | thought_hint_copy, caption_hint_copy, caption_hint_list (flags on a picture row); READER hint_copy, padding |
| 102 | A thing is said to fill, occupy or dominate the frame only when nothing else shares it (the picture, the hint and the records show nothing beside it); otherwise the row says where it sits. Measured on rows written from pictures: one row in five made the claim, and readers with the picture kept such rows half as often. | WRITER | READER contradicts_picture, invented |
| 103 | A reviewer who saw the picture and whose configuration allows it may correct the light with a fix verdict, giving a value of the shot light list or none (the picture shows no light a listed value fits). The row is written again from the item with that record set as a given record (for none, with no shot light), and the repair text says so; the writer keeps the record exactly and makes every sentence agree with it, or, for none, records no shot light and states no light source or direction. When two reviewers give different values, no record is set and the writer is told both readings. | ASSESSOR | spec_kept, light_removed |
| 104 | A reviewer whose packet says may_amend sends a row back with fix and the light, in addition to the note, only when the row's light is wrong and the picture shows its light plainly; it never corrects what it cannot see (a withheld picture, a light the picture does not show plainly). A reviewer whose packet does not say may_amend writes the light into the note. | READER | |
| 105 | With the picture attached, a person who is not the picture's subject (a passer-by, a figure far in the background, a hand or an arm at the edge, a shadow, a statue, a printed or painted figure) does not make the writer skip: the row is written for its recorded things, describes no person and does not mention that one. The writer skips with class person only when a person is the subject of the picture. A text call is unchanged: the hint decides. | WRITER | |
| 106 | On a picture row whose records hold no person, a person who is not the picture's subject and whom the row leaves out is not a fault. The code person is for a row that describes a person its records do not hold; should_skip is for a picture whose subject is a person the records leave out. | READER | person, should_skip |
| 107 | On every row, with or without a picture: the form of a person tag is not a reader's to judge. `woman`, `man`, `person`, `male` or `female` in the tags where rule 36 names 1girl, 1boy, 1other, with or without solo, and 1girl or 1boy for an adult, never send a row back; the person words of the tags are made uniform after writing, when the dataset is built. A reader faults a person tag only when it disagrees with the records: the wrong gender, the wrong number of people, or a person the records do not hold. | READER | wording, person |
| 108 | The rows of a writer family may be reviewed under a chain of stages: one of the listed first reviewers, then a challenger who is shown the row and the first review, then, only when the two disagree about keeping the row, an arbiter who is shown the row and both reviews, then, only when the arbiter marks its verdict unsure, an overseer who is shown the row and the three reviews, then the owner. The controller asks each stage of a reviewer the chain names and shows it the earlier reviews of the row without who gave them. | ASSESSOR | settle_chain, review.chain |
| 109 | Under a chain the chain decides, never the policy's counts or an assessor flag. Two keeps accept the row and two rejects discard it; any other agreement sends it back to its writer with the notes of both reviews. A disagreement with no arbiter goes to the owner as contested. A sure verdict of the arbiter or the overseer decides: keep accepts, reject discards, fix sends the row back with that reviewer's note alone, because the earlier notes may hold claims it found wrong. A row still unsure after the last stage goes to the owner. No audit sample is drawn from a row a chain settled. | ASSESSOR | settle_chain, writer_verdicts |
| 110 | A verdict of an arbiter or an overseer may carry unsure (true), the reviewer's best verdict marked as unable to settle the row. The mark is refused by the review client on any other row and dropped, with a noted problem, by the controller (the verdict then counts as sure). | ASSESSOR | unsure |
| 111 | A reviewer shown earlier reviews of the row judges the row itself against the picture, the records and the hint, checks each fault an earlier review names and says which of them hold, does not defer to an earlier review, and does not look for who wrote it. | READER | |
| 112 | A reviewer at the arbiter or overseer stage marks its verdict unsure only when the deciding claim cannot be told from the picture at its size, or the rule sheet does not settle it; a hard row it can decide is decided. | READER | |
| 113 | With the picture attached, text the picture shows that can be read (a sign, a label, a screen, a page, lettering on clothing or on a vehicle, an inscription) is read by the row: one thought with the step say gives the words in double quotes, exactly as written (the same spelling and letter case, " / " between lines), and the caption and the prompt quote them; the tags quote nothing. A hint may quote or list text the picture shows, and it helps with spelling only: a word is quoted only when the writer can read it in the picture, in the form the picture shows, and a hinted word that cannot be found in the picture is never quoted. A word cut off by the frame or by something in front of it is given only as far as it shows and is said to be cut off. A text longer than the length limit of the thoughts allows is quoted by its main words, and the row says that more follows. Text too small or too blurred to read at the picture's size is called unreadable and never guessed. A picture that shows no text gets no say thought for text; nothing else about the step say changes, and a text call is unchanged. | WRITER | |
| 114 | A person's name is never quoted from the picture: a signature, a photo credit or a name tag is described as such, without the name. Brand names and place names may be quoted. | WRITER | READER real_name |
| 115 | A reviewer compares every quoted text with the picture, letter for letter: a quoted text that differs is contradicts_picture, one the picture does not show, or does not show readably, is invented, also when the hint lists it (a hint is no evidence that text can be read at the picture's size), and a quoted person's name is real_name. A word that is cut off is quoted only as far as it shows. A row that leaves out text that is plainly readable and is the picture's main subject goes back with fix and wording; other text left out does not send a row back by itself. A say thought that gives the picture's text is not padding, and words the picture shows are not hint_copy, also when the hint quotes or lists them. | READER | contradicts_picture, invented, real_name, wording |
| 116 | On a picture row the words inside double quotes are the picture's text, not the row's prose: the checks that read the prose (details no record gives, talk about the records, moods, names, style names, repeats, step kinds) read the row without them, and the checks for hint copying leave out, in the row and in the hint, the words both of them quote. Sizes, counts, questions and tags are checked on the row as written, and so are fit and shape words about a minor. `americanize_row` leaves the quoted words of a picture row as written. A text row is checked exactly as before. | ASSESSOR | the codes of the checks, unchanged; rule_1 (the phrase check reads the prose) |
| 117 | Shown text is quoted in plain straight double quotes, never curly ones, and the row's own sentence keeps its punctuation outside them: "STOP", then the comma, never "STOP,". A mark that is part of the shown text stays inside the quotes. | WRITER | ASSESSOR quote_comma |
| 118 | On a picture row the curly quotation marks of the thoughts, the caption, the prompt and the critique are made straight when the row is stored (“ and ” become ", ‘ and ’ become '); the tags are never touched, and a text row keeps its characters. A quoted text whose last character before the closing mark is a comma is refused, and the writer may write the row again; a period there is not refused, since shown text can end with one ("LOW CLEARANCE 9 FT."). | ASSESSOR | quote_comma |

## Open

Version 2 covers rows written with the picture attached (photo sources sent to routes that accept images) beside text items. Rules for rows written from several pictures are not yet written.
