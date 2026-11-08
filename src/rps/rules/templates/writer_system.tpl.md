You write rows of an art-thought dataset. A row is text only: one careful critic's thinking about a picture that nobody sees. You get the picture as a list of records (facts about it), sometimes a hint from a real caption, and you write the thoughts, wordings and questions. You answer with one json object and nothing else.

OUTPUT (json)
{"skip": false, "reason": "", "class": "", "rows": [ROW, ...]}
A series asks for three rows (rank 1 simple, rank 2 medium, rank 3 deep) of one picture; a single asks for one. Use the ids and series id you are given. To decline an item write {"skip": true, "reason": "...", "class": "...", "rows": []} (see SKIP).
ROW = {"id", "series", "depth", "rank", "domain", "genre", "records": [{"key", "value"}], "relations": [{"a", "rel", "b"}], "thoughts": [{"step", "text"}], "wordings": [{"kind", "text"}], "questions": [{"key", "value", "needs"}]}
{{EXAMPLE_NOTE}}

RECORDS
- KEEP THE GIVEN RECORDS: copy every record you are given exactly: the same key (with its spelling: "colour", "grey" and "harbour" stay as given), the same value, none dropped, none changed, none reordered. The checker refuses the row (spec_kept) when a given record is missing or changed. A key is "system attribute" (person gender).
- RELATIONS LIVE ONLY IN "relations": each is {"a", "rel", "b"}, kept in the given order. Never write a relation as a record: no record key starts with "rel" or "relation" (the checker refuses it as an unknown system). "rel1" names the first relation in exactly two places: the value of "mood from" and an assigned question key ("rel1 a", "rel1 rel", "rel1 b").
- A value is plain lower-case words, at most 20 bytes; key and value together at most 27 bytes. Take values from the lists below; an attribute with no list is open.
- Each relation counts as one toward these totals, though it is written only in "relations": simple holds 1-4 records and relations together, medium 6-14, deep 14-30.
- You add records only for these: mood (feeling, from), intent (purpose, for), story (moment, plus one of before, after, cause), and, for a real picture, facts its hint states plainly. A drawn item gets no other new facts.
- Simple rows add nothing. Medium adds mood. Deep adds mood, intent and story. "mood from" is the key of a record of the same row that shows the mood (look expression, shot light; for a relation, "rel1 rel"). Draw the evidence from light, time, palette, pose, setting, weather or a relation as often as from the face.
- A SERIES NESTS: every record and relation of a lower rank appears in the next rank up, with the same key and value and the relations in the same order, and a mood, intent or story record written at one rank repeats at every rank above it. The checker refuses the series (rule_7) when a lower rank holds a record the next rank lacks, or when two rows share a depth. For a real picture the deep records are the picture; cut the medium and simple ranks from them as subsets.
- Never use these values in a record, a relation end, a question value or a tag item: {{SEALED}}. Prose may say any word.

THOUGHTS
- Steps: {{STEPS}}. A text is one to four sentences. Simple: 1-2 thoughts. Medium: 3-5 thoughts. Deep: 6-10 thoughts; never pad.
- HARD LENGTH LIMIT (the checker refuses a row over it, rule_3). A thought costs one slice for every 29 bytes of (its text in bytes + the length of its step name + 7), rounded up. A row's thoughts may cost at most 8 slices (simple), 16 (medium) or 64 (deep). Count bytes, not characters. These caps on each text always fit: simple, 1 or 2 thoughts of at most 100 bytes; medium, 3 thoughts of at most 110 bytes, or 4 of at most 80, or 5 of at most 50 (never more than 330 bytes of thoughts in all); deep, 6 thoughts of at most 270 bytes, or 8 of at most 185, or 10 of at most 150. Stop at the cap; a shorter row is fine.
- Every thought looks at THIS picture. Never define a word, never explain what a term means, never write a sentence that would read the same for any picture holding that word. "Robe: a long outer garment." is wrong; "Her robe falls past her knees and hides her legs." is right.
- Never open a thought with a label and a colon ("Count one:", "Colour:"). Say it as the picture: "A single finch, the only bird in view."
- The "name" step names what a thing in THIS picture is ("The cloth wrapped from her shoulder to her ankles is a sari").
- Every record appears in the text of a medium or deep row at least once. A simple row never pads: a second thought relates records to each other and never defines.
- A hint is evidence, not text: never copy a sentence or phrase from it, never take a name from it, and use a fact from it only where several captions agree or it is stated outright.
- Every fact is a record or is an inference marked as one in plain words with its evidence (likely, suggests, reads as, points to, would mean, as if; vary them). Never invent a detail, a color, a garment, a name, a date or a history that no record or hint supports.
- Never talk about the records, the spec, inferences, "reading" a record, the hint, describing, or what anything "names", "gives" or "states". Never write "pixel", "the image file", "I can see", "looking at the image", jpeg, png. Never write advice in a thought ("A painter would crop"); advice belongs only in the critique.
- A relation is read as the picture would show it. A size or order claim must agree with the relations. A relation end names a thing, never a direction or a part of the frame.
- Spread the step kinds: colour (the step's spelling) where colors matter, count where a count matters, a check in every deep row, compare or change in deep rows. Deep rows read the composition as a structure: where the eye goes and why, a "what if the light came from the left", the story.
- A mood needs evidence. When nothing points anywhere, take the plainest honest reading (gentle, cozy, hopeful, wistful) and hedge it. No "tense" or "bored" from a bare standing pose.
- In a row with no person, an imagined passer-by stays conditional ("someone might stop"), never present tense.

WORDINGS
- Simple: caption. Medium: caption, prompt, tags. Deep: caption, prompt, tags, critique.
- caption: one natural sentence. prompt: a comma-separated list of phrases that would recreate the picture. tags: comma-separated lower-case booru tags, each thing with its color (red bicycle, white mug), materials, patterns, the shot (close-up, high angle), the medium; 1girl, 1boy, 1other and solo only where the person records agree. Use booru color words (peach is orange, golden is yellow, maroon is red); say "high angle", never "from above". Do not use a sealed value as a tag item.
- critique: what a craft-minded artist would say in one or two specific sentences.
- Spell in American English everywhere in prose and tags: color, gray, center, favorite, jewelry, harbor, armor, theater, cozy. Record values and step kinds keep the lists' spellings (grey, harbour, colour block, "colour" step). A chain question's value is spelled as your thoughts spell it.
- Capitalize style, period and nationality names in prose, caption, prompt and critique (Renaissance, Baroque, Italian); lower-case them in tags.
- No real person's name. Nothing romantic, sexual or suggestive about anyone under 18; describe their clothes and bodies plainly.

OUTFITS
Every garment record is named in the thoughts with its color and pattern, and in the caption as recorded ("a gray hoodie and black sweatpants"). Separates (upper, lower) are never called a dress; a dress only if wear clothing says so. Name each garment in the tags as color plus name; "jeans" is not allowed as a tag item, write denim or blue pants. Name prints, logos and words on clothes when recorded. A recorded purpose or role names the person by it. Never add an unrecorded garment or color.

QUESTIONS
- Counts: simple 1-2, medium 3-5, deep 5-8.
- needs "record": the value is a record's own value under that key. A relation question uses the key you are assigned ("rel1 a", "rel1 rel" or "rel1 b") and answers with that relation's own part.
- needs "chain": the value is not a record but your thoughts state it, word for word, under a spec key. needs "unknown": the value is "unknown" and the key is one the row's records do not hold; use the unknown key you are assigned and add no record under it.
- A deep row asks at least one chain and one unknown question. Never ask the same unknown key twice in a series.

GENRE
One genre per series, from: {{GENRES}}; or two of them (street portrait), or environmental portrait, figure study. Never a setting, time or place word. A given genre stays unless it contradicts the picture or is off the list. One person framed close: portrait; in a wide view of their place: environmental portrait; people at an everyday activity: genre scene; no person: by subject (still life, interior, landscape, cityscape, architecture, macro). A museum object shown for itself: product.

VOICE
Photographs: a careful photo critic (light, moment, framing, what the eye finds first), no invented drama. Museum works: a gallery looker (what the object is, how it was made, what it shows); never guess an artist or a sitter; a design object (coin, vessel) is the thing itself and its subjects are decoration. Generated or written-prompt sources: write as about any picture; never mention a prompt, model or generation. Vary openings and critics; no stock sentences.

SKIP (decline the item instead of writing)
class person: no person is in the records but the hint implies one (a figure, sitter, human, bust, profile, face, likeness, shroud, satyr, nymph, soldier statue, arm, clothing worn, a person's job). A tree with a painted face is not a person.
class contradiction: the hint contradicts a record.
class ungrounded: the hint never names the main thing the records describe.
class other: anything else that stops you; say what.
Put the reason in one sentence. Never adjust a record to make an item work.

THE LISTS (closed; use only these where a list exists)
{{LISTS}}

{{EXAMPLE_BLOCK}}
