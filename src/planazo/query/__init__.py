"""Query interpretation — free-text `/find` message to a validated `SearchIntent`.

`interpret(text)` is the one public entry point; see
`planazo.query.interpreter` for the module docstring and the fallback contract
callers must branch on.
"""

from planazo.query.interpreter import interpret
from planazo.query.models import SearchIntent, SearchOrigin, with_search_origin

__all__ = ["SearchIntent", "SearchOrigin", "interpret", "with_search_origin"]
