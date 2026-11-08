ANSWER FORMAT (exact; the API has no schema)
Return one JSON object and nothing else. No markdown around it.

{"skip": false, "reason": "", "class": "", "rows": [ROW, ...]}

To decline an item, return exactly:
{"skip": true, "reason": "<one sentence>", "class": "person", "rows": []}
The class is one of: person, contradiction, ungrounded, other.

ROW:
{"id": "<given id>",
 "series": "<given series id>",
 "depth": "simple", "rank": 1,
 "domain": "<given domain>",
 "genre": "<a genre from the lists>",
 "records": [{"key": "<system> <attribute>", "value": "<text>"}, ...],
 "relations": [{"a": "<thing>", "rel": "<relation from the lists>", "b": "<thing>"}, ...],
 "thoughts": [{"step": "<step from the lists>", "text": "<one to four sentences>"}, ...],
 "wordings": [{"kind": "caption", "text": "<text>"}, ...],
 "questions": [{"key": "<system> <attribute>", "value": "<text>", "needs": "record"}, ...]}

Exact field rules:
- depth is a WORD in quotes: "simple", "medium" or "deep". Never 1, 2 or 3.
- rank is the only unquoted number in a row: 1, 2 or 3, exactly as the task gives it.
- Every other field that is a word or sentence is a quoted string. A record value is text in quotes even when it is a number ("3").
- Every record key is two words, "<system> <attribute>". A one-word key such as "mood", "intent" or "story" is refused.
- The keys a depth adds are exactly:
  medium: "mood feeling", "mood from"
  deep: "mood feeling", "mood from", "intent purpose", "intent for", "story moment", and one of "story before", "story after", "story cause"
- A relation goes in "relations" only, never in "records".
- "wordings" is a list of {"kind", "text"} objects, never an object. Simple rows carry caption; medium rows carry caption, prompt, tags; deep rows carry caption, prompt, tags, critique.
- A question's "needs" is one of the three words: record, chain, unknown.
- Use the given ids and series id exactly.
