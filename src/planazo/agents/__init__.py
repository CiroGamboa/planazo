"""Planazo's agent composition roots — the Recommender and the Extractor.

`loop.py` holds the generic observe -> reason -> act -> verify tool-calling
loop; `event_agent.py` binds it to the Recommender's tool set and exposes
`run_once()` (CHEAP tier); `extractor.py` binds it to the Extractor's tool
set and exposes `extract_once()` (STRONG tier) alongside the byte-verbatim
`DELEGATION_BRIEF` constant; `cli.py` is the `planazo-agent` console
entrypoint that drives `run_once`.
"""

from planazo.agents.extractor import DELEGATION_BRIEF, extract_once

__all__ = ["DELEGATION_BRIEF", "extract_once"]
