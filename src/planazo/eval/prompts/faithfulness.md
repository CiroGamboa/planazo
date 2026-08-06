You are a strict evaluator judging whether an answer is factually grounded in a
set of retrieved chunks. You do not know which retrieval configuration produced
the answer.

## Answer to score

{{ answer }}

## Retrieved chunks (numbered)

{{ chunks }}

## Rubric

Score the answer on a continuous scale from 0.0 to 1.0:

- 1.0 — every factual claim in the answer is directly supported by at least one
  of the retrieved chunks; no invented details.
- 0.5 — the answer is partially grounded: some claims are supported, others are
  extrapolations or embellishments beyond the chunks.
- 0.0 — the answer contradicts the chunks or invents facts (venues, times,
  names) that no chunk mentions.

Ignore stylistic quality, verbosity, and length. Score only faithfulness to the
chunks.

Respond ONLY with a JSON object matching:
{"score": float in [0.0, 1.0], "rationale": string <= 500 chars}
No prose, no code fences.
