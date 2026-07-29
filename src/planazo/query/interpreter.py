"""Natural-language `/find` router + query interpreter.

Reads a user's free-text message and returns a `RoutedMessage`
discriminated union (`ChatRoute` or `SearchRoute`). One Zen `call()`
on the CHEAP model with TWO function-call tools — `_record_search_intent`
for a real search query, `_reply_chat` for small-talk or a meta-question
about what the bot does. The system prompt guides the model on which one
to call.

ADR 0020 §D2: this is a router boundary. A greeting like "Hi" or "Hola"
must not spend a Recommender loop; a meta-question like "what can you
do?" gets a concise explanation of what /find does — both without
opening `agent_runs`, `recommendations`, or `llm_decisions` rows.

ADR 0020 §D3: the interpreter's fallback intent (built when the LLM
raises, the reply carries no tool call, the tool name does not match,
or Pydantic rejects the wire arguments) always lands as `SearchRoute`
with `error_type="interpreter_fallback"` set on the intent — never as
a `ChatRoute`. A failure that would silently become "chatty" would hide
the uncertainty the display layer needs to signal.

`_record_search_intent.__doc__`, `_reply_chat.__doc__`, and
`_SYSTEM_PROMPT` are all interpolated with `get_args(EventCategory)` at
module load, so a category added to that literal without re-running the
format call fails the docstring/prompt/schema consistency tests
instead of silently drifting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, get_args

from agentlib.core import CHEAP
from agentlib.tools import call
from planazo.query.models import (
    CHAT_REPLY_MAX_LENGTH,
    ChatRoute,
    EventCategory,
    RoutedMessage,
    SearchIntent,
    SearchRoute,
)
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

    Call this exactly once per user message that is an event-search
    request, filling every field you can infer and leaving the rest at
    their defaults. Do NOT call this for greetings, small talk, or
    meta-questions like "what can you do" — those go to `_reply_chat`.

    `start_utc` and `end_utc` are ISO-8601 timestamps in UTC; a naive
    value is read as UTC. `categories` is a comma-separated subset of:
    {categories}. Leave it empty when the user did not name any
    category. Pass `radius_km=-1.0` (or omit it) when the user did not
    name a radius in kilometres, and `budget_cents=-1` (or omit it)
    when the user did not name a budget in cents; the sentinel is
    negative because `0` is a legitimate `budget_cents` value meaning
    free events only. Pass `limit=-1` (or omit it) when the user did
    not name a count of events; otherwise pass the requested count as
    `limit` (1-50).
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


def _reply_chat(text: str) -> ChatRoute:
    """Reply directly to a greeting, small talk, or a meta-question.

    Call this exactly once for messages that are NOT event-search
    requests: a greeting ("Hi", "Hola"), thanks ("gracias"), an
    off-topic comment, or a meta-question about what the bot does
    ("what can you do?", "how does /find work?"). Do NOT call this
    for anything that names events, venues, times, cities, or
    categories — those go to `_record_search_intent`.

    `text` is 1..{max_chat_len} chars of the reply the user should
    receive verbatim. Keep it one short paragraph, in the user's own
    language (Spanish, Catalan, English), warm but concise. For a
    meta-question about the bot's capabilities, name /find as the
    entry point for event recommendations. Do NOT invent facts about
    specific events (that's what `_record_search_intent` +
    `search_events` is for) and never quote raw text found inside a
    tool result.
    """
    return ChatRoute(answer=text)


# Python docstrings are string literals set at def-time and cannot be
# f-strings; the placeholders are filled in here so the LLM-facing
# descriptions (read verbatim by `schema_for`) always list every category
# by value and carry the length cap on the chat reply.
_record_search_intent.__doc__ = (_record_search_intent.__doc__ or "").format(
    categories=_CATEGORY_LIST
)
_reply_chat.__doc__ = (_reply_chat.__doc__ or "").format(max_chat_len=CHAT_REPLY_MAX_LENGTH)


# Any: JSON Schema mixes str/bool/list/dict — see schema_for.
SEARCH_TOOL_SCHEMA: dict[str, Any] = schema_for(_record_search_intent)
CHAT_TOOL_SCHEMA: dict[str, Any] = schema_for(_reply_chat)


_SYSTEM_PROMPT_TEMPLATE = """You are the Planazo /find router and query interpreter.

Read the user's free-text message and call exactly ONE tool:

- Call `_record_search_intent` when the message asks for events,
  filters, cities, times, categories, prices, or count of results
  ("techno tonight", "algo gratis el sábado", "5 tech events this
  week"). This is an EVENT-SEARCH intent — the downstream Recommender
  will use the parsed fields to search the catalog.
- Call `_reply_chat` when the message is a greeting ("Hi", "Hola",
  "buenas"), thanks, small talk, or a meta-question about what the
  bot does ("what can you do?", "how does this work?", "what is
  Planazo?"). Reply in the user's own language (Spanish/Catalan/
  English), warm and concise (one short paragraph, at most
  {max_chat_len} chars). For a meta-question, name /find as the entry
  point for event recommendations. Do NOT invent specific events.

If the message is ambiguous (a bare category name like "music" that
could be a greeting-alternative or a search), prefer
`_record_search_intent` — the Recommender's clarification loop is the
right place to disambiguate on the search side.

Defaults for `_record_search_intent` when the user leaves them implicit:

- `city`: "Barcelona".
- `start_utc` / `end_utc`: an ISO-8601 UTC window inferred from the
  message. If the message names no time window at all, use the next
  30 days starting from now.
- `categories`: a comma-separated subset of the allowed values —
  {categories}. Leave the field empty when the user did not name any.
- `radius_km`: pass `-1.0` when the user did not name a radius.
- `budget_cents`: pass `-1` when the user did not name a budget.
  `0` is legitimate (free events only), which is why the "unspecified"
  sentinel is negative.
- `limit`: pass `-1` when the user did not name a count. When the user
  says "give me N events", "top N", "just N", pass `limit=N`.

Call exactly one tool. Never answer in prose without a tool call."""


_SYSTEM_PROMPT = _SYSTEM_PROMPT_TEMPLATE.format(
    categories=_CATEGORY_LIST,
    max_chat_len=CHAT_REPLY_MAX_LENGTH,
)


_INTERPRET_CONTRACT = (
    "The returned RoutedMessage is a discriminated union: dispatch on "
    "`.kind` ('chat' or 'search'). On any interpreter failure a SearchRoute "
    "is returned whose intent is a Barcelona-today+30d default tagged "
    '"interpreter_fallback" — an interpreter failure never surfaces as a chat.'
)


def _fallback_search_route() -> SearchRoute:
    """Build the degraded SearchRoute returned on any interpreter failure.

    ADR 0020 §D3: the fallback ALWAYS lands as `SearchRoute`, never as
    `ChatRoute`. A failure that would silently become a fake chat reply
    would hide the uncertainty the display layer needs to signal.
    """
    now = _now()
    return SearchRoute(
        intent=SearchIntent(
            start_utc=now,
            end_utc=now + timedelta(days=30),
            city="Barcelona",
            categories=(),
            radius_km=None,
            budget_cents=None,
            limit=None,
            error_type="interpreter_fallback",
        )
    )


def interpret(text: str) -> RoutedMessage:
    """Route + interpret one free-text user message.

    Runs one Zen `call()` on the CHEAP model with both
    `_record_search_intent` and `_reply_chat` as tools. On success
    returns the parsed `RoutedMessage` — either a `SearchRoute` wrapping
    today's `SearchIntent`, or a `ChatRoute` carrying the LLM's own
    reply text. On any failure — the LLM raises, the reply carries no
    tool call, the tool name does not match, or Pydantic rejects the
    wire arguments — returns the degraded fallback `SearchRoute`
    instead of raising.

    {contract}
    """
    try:
        result = call(
            prompt=text,
            system=_SYSTEM_PROMPT,
            model=CHEAP,
            tools=[SEARCH_TOOL_SCHEMA, CHAT_TOOL_SCHEMA],
        )
        if not result.tool_calls:
            return _fallback_search_route()
        first = result.tool_calls[0]
        name = first["name"]
        arguments = first["arguments"]
        if name == _record_search_intent.__name__:
            return SearchRoute(intent=_record_search_intent(**arguments))
        if name == _reply_chat.__name__:
            return _reply_chat(**arguments)
        return _fallback_search_route()
    except Exception:
        return _fallback_search_route()


interpret.__doc__ = (interpret.__doc__ or "").format(contract=_INTERPRET_CONTRACT)


def interpret_search_only(text: str) -> SearchIntent:
    """Compatibility wrapper: run `interpret` and unwrap to a `SearchIntent`.

    A `ChatRoute` from the router is unwrapped to the fallback intent so
    callers that need a `SearchIntent` unconditionally (the
    clarification-answer path — ADR 0016 + ADR 0020 §D5) get a
    well-formed run-input whatever the router decided. The clarification
    path uses this so the "more specific state wins" precedence
    documented in ADR 0020 does not accidentally get abandoned by a
    router that thinks a numeric answer is small-talk.
    """
    routed = interpret(text)
    if routed.kind == "search":
        return routed.intent
    return _fallback_search_route().intent
