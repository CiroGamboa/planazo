# Seed events generation prompt

This is the exact prompt that `scripts/generate_events_seed.py` passes to the
OpenCode Zen `strong` model when producing `data/eval/events_seed.jsonl`.

The generator's role is reproducibility: the committed JSONL is the source of
truth for the eval harness. LLM output is not perfectly deterministic even at
`temperature=0`, so a re-run will emit similar-but-not-identical rows. Any
regeneration should be reviewed before overwriting the committed file — the
questions in `data/eval/questions.jsonl` reference specific event ids that
must remain stable.

## Role brief

You are a data generator for a Barcelona-events discovery product. You emit
realistic, plausible Barcelona events in JSON Lines format — one JSON object
per line, no comments, no wrapping array — that fit the schema below. Every
row must be independently valid JSON parseable by `json.loads`.

## Output schema

Each line is a JSON object with these fields (every field required unless
marked optional):

- `id` (int): a unique positive integer, 1..N assigned in row order.
- `source` (string): `"seed"` for every row.
- `source_url` (string): `"seed://event/<slug>"` — a unique synthetic URL per
  row. The slug is lowercase, hyphenated, and derived from the title +
  ordinal.
- `title` (string): the event's public-facing name, 5..80 chars.
- `start_utc` (ISO-8601 UTC string, `"YYYY-MM-DDTHH:MM:SS+00:00"`): the
  event's start time, spread across the next 30 days from today. Include a
  mix of weeknight and weekend times.
- `end_utc` (ISO-8601 UTC string): strictly after `start_utc`. Typical
  durations 1..6 hours.
- `category` (string): one of `{"tech", "cultural", "music", "networking",
  "sports", "other"}`. These are Planazo's canonical event categories.
- `city` (string): `"Barcelona"` for every row.
- `price_cents` (int, ≥0): the ticket price in cents. `0` means free.
- `confidence` (float, 0..1): `0.9` for every row.
- `event_index_in_post` (int, ≥0): use the same value as `id - 1`.
- `venue_name` (string): a real Barcelona venue drawn from the curated list
  below (or `null` when the event is outdoor / venueless).
- `venue_address` (string): the venue's street address including its
  neighborhood (e.g. `"Nou de la Rambla 113, Poble Sec"`). Neighborhood MUST
  appear inside `venue_address` since there is no `neighborhood` column on
  the event row.
- `tags` (array of strings): 2..5 short lowercase tags — some genre-shaped
  (e.g. `"flamenco"`, `"techno"`, `"cinema"`, `"tapas"`, `"contemporary"`),
  some thematic (e.g. `"family-friendly"`, `"kid-safe"`, `"outdoor"`,
  `"nightlife"`, `"free"`).
- `description` (string, 40..250 chars): one or two sentences of natural
  language describing the event. This is the highest-signal field for
  retrieval — vary vocabulary deliberately: use `"affordable"` for one
  cheap event and `"cheap"` for another; `"kid-safe"` for one family event
  and `"family-friendly"` for another; `"acronym-heavy"` for a few
  ("OBC concert", "FIB pre-party", "MACBA opening", "BCN Film Fest").

## Curated venue pool

Draw the `venue_name` field from this list. Repeat venues are welcome (a
"same-venue same-night" pair is required — see Diversity below), but the
pool as a whole must show at least 15 distinct venues across the corpus.

- Sala Apolo (Poble Sec)
- Razzmatazz (Poble Nou)
- Bikini (Les Corts)
- Palau de la Música (Ciutat Vella)
- Filmoteca de Catalunya (El Raval)
- Antic Teatre (El Born)
- Marula Café (Gòtic)
- Harlem Jazz Club (Gòtic)
- Jamboree (Ciutat Vella)
- Sidecar (Gòtic)
- Moog (El Raval)
- Nitsa (Poble Sec)
- La Paloma (El Raval)
- El Molino (Poble Sec)
- Palau Robert (Eixample)
- MACBA (El Raval)
- CCCB (El Raval)
- Palau Dalmases (El Born)
- Sant Antoni Market (Sant Antoni)
- Camp Nou (Les Corts)
- Barceloneta Beach (Barceloneta)
- Parc de la Ciutadella (El Born)
- Poblenou Beach (Poble Nou)
- Casa Batlló (Eixample)

## Curated neighborhoods pool

Include at least 10 distinct neighborhoods across the corpus (repeats are
fine). Each event's `venue_address` must include the neighborhood string:

Gràcia, El Born, Sant Antoni, Poble Nou, Poble Sec, Eixample, Ciutat Vella,
Sants, Sarrià, Gòtic, El Raval, Barceloneta, Horta, Nou Barris, Sant Andreu,
Les Corts.

## Diversity requirements

- **~120 rows total.**
- **All 6 `category` values represented**, with an approximate balance:
  music 30, cultural 25, tech 15, sports 15, networking 15, other 20.
- **At least 8 distinct themes present via `tags`** — music, food_and_drink,
  cinema, theater, family, sports, cultural, nightlife.
- **At least 15 distinct venues.**
- **At least 10 distinct neighborhoods** (each present in `venue_address`).
- **Times spread across the next 30 days**, with weeknight + weekend mix.
- **At least 20 free events** (`price_cents == 0`).
- **At least 20 paid events**, price cents varied 500..15000.
- **Two events at the same venue on the same night** (near-duplicate noise).
- **Two events with acronyms only in the description** — e.g. `"OBC concert"`,
  `"FIB pre-party"`.
- **Semantic-mismatch vocabulary variation** — some cheap events use
  `"affordable"`; some family events use `"kid-safe"`.

## Reminders

- Emit JSON Lines only — no wrapping array, no markdown fences, no comments.
- Every row must be parseable by `json.loads` and validate against Planazo's
  `Event` Pydantic model.
- `id` is 1..N in row order; `event_index_in_post` is `id - 1`.
- The corpus is fixture data for a retrieval eval — flat, homogeneous rows
  flatten the metrics. Vary vocabulary and structure deliberately.
