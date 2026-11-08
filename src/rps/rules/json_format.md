ANSWER FORMAT (field by field; placeholders in angle brackets, no content)

Answer with one JSON object and nothing else:

{"skip": <true or false>, "reason": "<text>", "class": "<text>", "rows": [<ROW>, ...]}

ROW:
{"id": "<the id you were given>",
 "series": "<the series id you were given>",
 "depth": "<simple or medium or deep>",
 "rank": <1 or 2 or 3>,
 "domain": "<a domain from the lists>",
 "genre": "<a genre from the lists>",
 "records": [{"key": "<system> <attribute>", "value": "<text>"}, ...],
 "relations": [{"a": "<thing>", "rel": "<a relation from the lists>", "b": "<thing>"}, ...],
 "thoughts": [{"step": "<a step from the lists>", "text": "<one to four sentences>"}, ...],
 "wordings": [{"kind": "<caption or prompt or tags or critique>", "text": "<text>"}, ...],
 "questions": [{"key": "<system> <attribute>", "value": "<text>", "needs": "<record or chain or unknown>"}, ...]}

Rules for the fields:
- depth is a WORD: simple, medium or deep, in quotes. Never 1, 2 or 3.
- rank is the only number in a row: 1, 2 or 3, without quotes, as the task gives it.
- Every other field that is a word or a sentence takes a word or a sentence in quotes, never a digit in its place. A record value is text in quotes even when it is a number ("3").
- Every record key is two words, "<system> <attribute>". A one-word key such as "mood", "intent" or "story" is refused. The keys a depth adds are exactly:
  medium: "mood feeling", "mood from"
  deep: "mood feeling", "mood from", "intent purpose", "intent for", "story moment", and one of "story before", "story after", "story cause"
- A relation goes in "relations" only, never in "records".
- "wordings" is a list of {"kind", "text"} items, not an object. Simple rows carry caption; medium rows caption, prompt, tags; deep rows caption, prompt, tags, critique.
- A question's "needs" is one of the three words record, chain, unknown.

To decline an item, answer with exactly:
{"skip": true, "reason": "<one sentence>", "class": "<person or contradiction or ungrounded or other>", "rows": []}
