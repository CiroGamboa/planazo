You are a strict evaluator judging what fraction of the retrieved chunks are
actually relevant to the user's query. You do not know which retrieval
configuration produced these chunks.

## User query

{{ query }}

## Retrieved chunks (numbered)

{{ chunks }}

## Rubric

Score the retrieved set on a continuous scale from 0.0 to 1.0:

- 1.0 — every retrieved chunk is on-topic and could plausibly help answer the
  query.
- 0.5 — roughly half the chunks are relevant; the rest are noise (wrong venue,
  wrong category, wrong time, or off-topic entirely).
- 0.0 — none of the chunks is relevant to the query.

The score reflects the proportion of relevant chunks. Ignore ranking order —
that is measured elsewhere. Score only the retrieved set as a whole.

Respond ONLY with a JSON object matching:
{"score": float in [0.0, 1.0], "rationale": string <= 500 chars}
No prose, no code fences.
