"""Natural-language `/find` query interpreter.

Translates a user's free-text message into a validated `SearchIntent` via one
Zen `call()` on the CHEAP model with a single function-call tool
(`_record_search_intent`). Public surface is deliberately narrow: `interpret`
plus `TOOL_SCHEMA`, and nothing else. The interpreter is not a registered
tool — the Recommender's loop never calls it; the bot's `/find` handler (M6)
will be its only caller.

`_record_search_intent`'s signature is what `schema_for` reflects into
`TOOL_SCHEMA`; its body unpacks the wire arguments into a `SearchIntent`. The
wire shape mirrors the CSV/sentinel convention `confirm_and_create_calendar_event`
and `search_events` already use for optional arguments: `categories` travels
as a comma-separated string, `radius_km=-1.0` and `budget_cents=-1` (or
omitting them) mean "the user did not specify". `0` is a legitimate
`budget_cents` value (free events only), which is why the "unspecified"
sentinel is negative.

`_record_search_intent.__doc__` and `_SYSTEM_PROMPT` are both interpolated
with `get_args(EventCategory)` at module load, so a category added to that
literal without re-running the format call fails the docstring/prompt/schema
consistency tests instead of silently drifting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, get_args

from agentlib.core import CHEAP
from agentlib.tools import call
from planazo.query.models import EventCategory, SearchIntent
from tools.schema import schema_for

_CATEGORY_LIST = ", ".join(get_args(EventCategory))


def _now() -> datetime:
    """Return the current UTC instant.

    Wrapped in a helper so tests can monkeypatch it to freeze the fallback
    window without freezing global time.
    """
    return datetime.now(UTC)


def _record_search_intent(
    start_utc: str,
    end_utc: str,
    city: str,
    categories: str = "",
    radius_km: float = -1.0,
    budget_cents: int = -1,
    limit: int = -1,
) -> SearchIntent:
    """Record the interpreted `/find` query as a structured SearchIntent.

    Call this exactly once per user message, filling every field you can
    infer and leaving the rest at their defaults. `start_utc` and `end_utc`
    are ISO-8601 timestamps in UTC; a naive value is read as UTC.
    `categories` is a comma-separated subset of: {categories}. Leave it
    empty when the user did not name any category. Pass `radius_km=-1.0`
    (or omit it) when the user did not name a radius in kilometres, and
    `budget_cents=-1` (or omit it) when the user did not name a budget
    in cents; the sentinel is negative because `0` is a legitimate
    `budget_cents` value meaning free events only. Pass `limit=-1` (or
    omit it) when the user did not name a count of events; otherwise pass
    the requested count as `limit` (1-50).
    """
    return SearchIntent.model_validate(
        {
            "start_utc": start_utc,
            "end_utc": end_utc,
            "city": city,
            "categories": categories,
            "radius_km": None if radius_km < 0 else radius_km,
            "budget_cents": None if budget_cents < 0 else budget_cents,
            "limit": None if limit < 0 else limit,
        }
    )


# Python docstrings are string literals set at def-time and cannot be
# f-strings; the placeholder is filled in here so the LLM-facing description
# (read verbatim by `schema_for` below) always lists every category by value.
_record_search_intent.__doc__ = (_record_search_intent.__doc__ or "").format(
    categories=_CATEGORY_LIST
)


# Any: JSON Schema mixes str/bool/list/dict — see schema_for.
TOOL_SCHEMA: dict[str, Any] = schema_for(_record_search_intent)


_SYSTEM_PROMPT_TEMPLATE = """You are the Planazo /find query interpreter.

Read the user's free-text message describing what events they want to find,
then call the `_record_search_intent` tool exactly once with your best
interpretation. Never answer in prose; always call the tool.

Defaults you should apply when the user leaves them implicit:

- `city`: "Barcelona". Every user of Planazo is looking for Barcelona
  events unless they explicitly named another city.
- `start_utc` / `end_utc`: an ISO-8601 UTC window inferred from the
  message. If the message names no time window at all, use the next 30
  days starting from now.
- `categories`: a comma-separated subset of the allowed values —
  {categories}. Leave the field empty when the user did not name any.
- `radius_km`: pass `-1.0` when the user did not name a radius in
  kilometres. `0` is a legitimate value.
- `budget_cents`: pass `-1` when the user did not name a budget in
  cents. `0` is a legitimate value (free events only), which is why the
  "unspecified" sentinel is negative.
- `limit`: pass `-1` when the user did not name a count. When the user
  says "give me N events", "top N", "N recommendations", "just N", etc.,
  pass `limit=N`.

Call `_record_search_intent` exactly once."""


_SYSTEM_PROMPT = _SYSTEM_PROMPT_TEMPLATE.format(categories=_CATEGORY_LIST)


_INTERPRET_CONTRACT = (
    "Callers MUST check result.error_type before using any other field: on failure "
    "the returned SearchIntent is a Barcelona-today+30d default tagged "
    '"interpreter_fallback", structurally indistinguishable from a real intent.'
)


def _fallback_intent() -> SearchIntent:
    """Build the degraded intent returned on any interpreter failure."""
    now = _now()
    return SearchIntent(
        start_utc=now,
        end_utc=now + timedelta(days=30),
        city="Barcelona",
        categories=(),
        radius_km=None,
        budget_cents=None,
        limit=None,
        error_type="interpreter_fallback",
    )


def interpret(text: str) -> SearchIntent:
    """Translate a free-text `/find` query into a structured `SearchIntent`.

    Runs one Zen `call()` on the CHEAP model with `_record_search_intent`
    as the only tool. On success returns the parsed intent with
    `error_type is None`. On any failure — the LLM raises, the reply
    carries no tool call, the tool name does not match, or Pydantic
    rejects the wire arguments — returns the degraded fallback intent
    instead of raising.

    {contract}
    """
    try:
        result = call(
            prompt=text,
            system=_SYSTEM_PROMPT,
            model=CHEAP,
            tools=[TOOL_SCHEMA],
        )
        if not result.tool_calls:
            return _fallback_intent()
        first = result.tool_calls[0]
        if first["name"] != _record_search_intent.__name__:
            return _fallback_intent()
        return _record_search_intent(**first["arguments"])
    except Exception:
        return _fallback_intent()


interpret.__doc__ = (interpret.__doc__ or "").format(contract=_INTERPRET_CONTRACT)
