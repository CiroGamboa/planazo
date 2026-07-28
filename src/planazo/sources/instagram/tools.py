"""The LLM-facing Instagram tool, bound to one `InstagramSource` per run.

`build_fetch_instagram_post(source)` returns a `(schema, callable)` pair the
Extractor's tool registry consumes: `schema` is what `agentlib.tools.call`
sees; `callable` is what `run_loop`'s registry dispatches. The `source`
argument is a captured free variable of the inner `fetch_instagram_post`, so
it never appears in the LLM-visible schema — the model sees exactly one
parameter (`url`) and cannot supply an alternative source.

The wrapper's job is a shape flatten: `InstagramSource.fetch_post` returns
either a `RawPost` or a typed error dict; the LLM feed-back path serializes
tool return values with `json.dumps`, which cannot serialize a `BaseModel`.
So a happy return goes through `RawPost.model_dump(mode="json")` and the
already-JSON-safe error dict passes through unchanged, giving the loop one
shape (`dict[str, object]`) either way.

Mirrors `memory/api.py::build_memory_tools`'s dependency-injection-by-closure
discipline: identity/source bound at build time, invisible to the LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from planazo.sources.instagram.adapter import InstagramSource
from planazo.sources.models import RawPost
from tools.schema import schema_for


def build_fetch_instagram_post(
    source: InstagramSource,
) -> tuple[dict[str, Any], Callable[..., dict[str, object]]]:
    """Build the `fetch_instagram_post` tool bound to `source`.

    Returns `(schema, callable)`: `schema` is what `agentlib.tools.call`
    receives in its `tools=` argument; `callable` is what `run_loop`'s
    registry dispatches by name. `source` is captured by closure — the
    LLM's schema has only `url` as a parameter and no tool-call argument
    can override the bound source.
    """

    def fetch_instagram_post(url: str) -> dict[str, object]:
        """Fetch one Instagram post by URL.

        Returns a JSON-serializable dict — on success the post's fields
        (source, permalink, caption, posted_at, author_handle, media list),
        on failure a typed error dict with `error_type`, `message`, and
        `url` keys. Never raises.
        """
        result = source.fetch_post(url)
        if isinstance(result, RawPost):
            return result.model_dump(mode="json")
        return result

    schema = schema_for(fetch_instagram_post)
    return schema, fetch_instagram_post
