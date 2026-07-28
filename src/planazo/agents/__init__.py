"""Planazo's agent composition roots — the Recommender and the Extractor.

`loop.py` holds the generic observe -> reason -> act -> verify tool-calling
loop; `event_agent.py` binds it to the Recommender's tool set and exposes
`run_once()` (CHEAP tier); `extractor.py` binds it to the Extractor's tool
set and exposes `extract_once()` (STRONG tier) alongside the byte-verbatim
`DELEGATION_BRIEF` constant; `cli.py` is the `planazo-agent` console
entrypoint that drives `run_once`.

Extractor symbols (`DELEGATION_BRIEF`, `extract_once`) deliberately are NOT
re-exported at the package level — callers import from
`planazo.agents.extractor` directly. Re-exporting them here would force
`extractor.py` to load at `import planazo.agents` time, which pulls in
`planazo.extraction.audit`; that module in turn imports through
`planazo.agents.loop` (which routes through this `__init__.py`), circular.
The lazy import inside `event_agent.run_once`'s `if user_id is not None:`
block is the deliberate seam that keeps the Recommender's static import
graph off the Extractor.
"""
