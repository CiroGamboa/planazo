"""Tool-calling entry point.

`call()` already accepts `tools=`, so this module adds no behavior of its
own — it re-exports the `core` primitives under a name that keeps
tool-calling call sites (agent loops that dispatch `Result.tool_calls`)
distinct from plain single-shot calls, without duplicating the wrapper.
"""

from agentlib.core import CHEAP, MODELS, STRONG, Result, call, show

__all__ = ["CHEAP", "MODELS", "STRONG", "Result", "call", "show"]
