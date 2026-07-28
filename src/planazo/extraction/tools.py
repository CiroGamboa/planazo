"""The Recommender-side LLM tool that delegates one extraction to the Extractor.

`build_dispatch_extraction(delegator_user_id)` returns one run's schema plus
the registry entry `run_loop` dispatches through. The inner LLM-callable
`dispatch_extraction(url)` is a nested closure over the validated
`delegator_user_id`: it is a captured free variable, so it appears in no
signature, `schema_for` cannot see it, and no tool-call argument can supply
it. A tool call that carries a `delegator_user_id` key anyway raises
`TypeError`, which `run_loop`'s dispatch turns into a `tool_failed` marker
rather than a silent scope override. This mirrors `memory/api.py`'s
closure-over-identity discipline; a `functools.partial` would leave the
bound keyword overridable by a caller passing the same key again.

`delegator_user_id` is validated once at build time via `MemoryScopeRequest`
(the same positive-int check `build_memory_tools` uses), so a bad identity
fails while composing the run instead of on the model's first turn.

The callable returns `ExtractionResult.model_dump(mode="json")` verbatim —
no flattening, no error re-wrapping. `extract_once`'s own return contract
is Pydantic-validated at the source, so the LLM sees the same taxonomy
branches the Extractor emits and decides how to talk to the user (Rule 4 —
errors are typed branches). This module is deliberately not top-imported
by `event_agent.py`: `event_agent.py` reaches it via a lazy import inside
`run_once`, so the Recommender's static import graph never touches
`planazo.agents.extractor` or `planazo.sources.instagram` (ADR 0005
§Trust boundary).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from planazo.agents.extractor import extract_once
from planazo.memory.models import MemoryScopeRequest
from tools.schema import schema_for


def build_dispatch_extraction(
    delegator_user_id: int,
    # Any: JSON Schema documents, as produced by schema_for.
) -> tuple[list[dict[str, Any]], dict[str, Callable[..., dict[str, object]]]]:
    """Build one run's `dispatch_extraction` schema + registry, bound to `delegator_user_id`.

    Raises `ValidationError` when `delegator_user_id` is not a positive
    integer — the Extractor requires a positive integer as its
    `delegator_user_id`, and the failure lands at composition time rather
    than on the model's first call.
    """
    owner = MemoryScopeRequest(user_id=delegator_user_id, scope="both").user_id

    def dispatch_extraction(url: str) -> dict[str, object]:
        """Delegate one Instagram-post extraction to the Extractor.

        Call this when the user asks about a specific Instagram post — the
        `url` is the post link. The Extractor fetches the post, decides
        which events (0..N) it announces, and returns a structured
        hand-off: `status` is `"ok"` with a populated `events` list on
        success (one entry per distinct event the post announces — a single
        post can carry multiple events, e.g. a curator carousel), `"error"`
        with a typed `error_type` on failure, and `"needs_clarification"`
        with a typed `error_type` when the post is ambiguous (missing date,
        out-of-metro venue, unseparable carousel). The post's caption text
        never crosses back — only the structured fields listed above.
        """
        result = extract_once(url, delegator_user_id=owner)
        return result.model_dump(mode="json")

    return [schema_for(dispatch_extraction)], {"dispatch_extraction": dispatch_extraction}
