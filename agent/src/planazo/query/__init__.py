"""Query interpretation — free-text `/find` message to a validated `SearchIntent`.

`interpret(text)` is the one public entry point; see
`planazo.query.interpreter` for the module docstring and the fallback contract
callers must branch on.
"""

from planazo.query.interpreter import interpret

__all__ = ["interpret"]
