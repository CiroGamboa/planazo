"""Multi-turn conversation engine — `handle_user_message`.

The composition root for the bot's `/find` command and any other
transport that wants a stateful Recommender front door. It sits
above the primitives it composes:

- `planazo.query.interpret` — free text → `SearchIntent`.
- `planazo.agents.event_agent.run_once` — one Recommender loop.
- `planazo.observability.repository.query_recommendations` +
  `query_agent_runs` — read-side, for the "tell me about #N" and
  "more results" follow-up patterns.
- `planazo.catalog.repository.get_event_by_id` — load an `Event` by
  the FK stored on a `RecommendationRecord`.
- `planazo.identity.repository.set_preference` — persist
  clarification answers as preference-namespaced enrichment rows.
- `planazo.conversation.repository.get_state` / `upsert_state` — the
  per-user scratchpad.

The composition is intentional (Rule 8, ADR 0008): this context sits
above the primitives it composes; each primitive still tests
independently under its own bounded-context tests.

Flow (top-down, in `handle_user_message`):

1. Read `ConversationState`. `None` means "fresh user, no state".
2. If a `last_recommendation_run_id` is set AND the text looks like
   a "tell me about #N" pattern → return the Nth event's detail.
3. If a `last_recommendation_run_id` is set AND the text looks like
   a "more results" pattern → re-run the same intent, filter out
   event IDs already surfaced under that run.
4. If a `pending_clarification` is set → consume the text as the
   answer. Persist one `PreferenceRecord` under a
   `pref:clarified.<derived_key>` namespace. Clear the clarification.
   Run a fresh `interpret + run_once` with the augmented profile.
5. Otherwise → fresh path. `interpret(text) + run_once(user_id, intent)`.

Every branch upserts the scratchpad with the current timestamp
before returning. Persistence of the scratchpad is best-effort per
Rule 4 — a swallow-and-log seam means an audit-store outage never
breaks the primary Recommender flow — but the write is on the hot
path so a healthy DB always sees the latest state.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Final

from pydantic import ValidationError

from planazo.agents.event_agent import RecommenderResult, run_once
from planazo.catalog.models import Event
from planazo.catalog.repository import get_event_by_id
from planazo.conversation.models import (
    ConversationReply,
    ConversationState,
    PendingClarification,
)
from planazo.conversation.repository import get_state, now_utc, upsert_state
from planazo.identity.repository import set_preference
from planazo.observability.repository import query_agent_runs, query_recommendations
from planazo.query.interpreter import interpret, interpret_search_only
from planazo.query.models import RoutedMessage, SearchIntent

logger = logging.getLogger(__name__)

# Recognised follow-up patterns. All are anchored to the message start
# (so "more" in a longer sentence does not trip); case-insensitive.
_DETAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:tell me about |details? for |#|number )?\s*(\d+)\s*$", re.IGNORECASE
)
"""Matches: "tell me about 2", "details 2", "#2", "number 2", or a bare "2"."""

_MORE_RESULTS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(more|more results|otros?|otras|m[aá]s)\b", re.IGNORECASE
)
"""Matches: "more", "more results", "otro"/"otros", "otra"/"otras", "mas"/"más".

Bilingual because the bot is en+es (see `data/bot.yaml.locales`).
"""

# Derived-key mapping for clarification answers → preference namespace.
# The Recommender's `ask_user` tool records arbitrary questions; we
# match a small set of common phrasings to canonical namespaced keys
# and fall back to `general` for anything unrecognised. Every key is
# prefixed with `pref:clarified.` so an operator reading the row can
# tell at a glance where it came from.
_CLARIFICATION_KEY_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(?i)\b(category|categories|kind|type of event)\b"), "categories"),
    (re.compile(r"(?i)\b(city|neighbou?rhood|barrio|ciudad)\b"), "city"),
    (re.compile(r"(?i)\b(budget|price|cost|precio|presupuesto)\b"), "budget"),
    (re.compile(r"(?i)\b(when|time|day|date|hora|d[ií]a|fecha)\b"), "time_window"),
    (re.compile(r"(?i)\b(where|venue|place|lugar|sitio)\b"), "venue"),
)
_CLARIFICATION_KEY_FALLBACK: Final[str] = "general"
_CLARIFICATION_KEY_PREFIX: Final[str] = "pref:clarified."

_RECOMMENDATIONS_PREFACE_CAP: Final[int] = 500
"""Bound for the `ok`-branch `ConversationReply.answer` (the recommendations
preface). `ConversationReply.answer` itself allows up to 2,000 chars — wide
enough for the `no_results`/`detail` branches' longer explanations — but the
preface sits *above* a numbered candidate list a Telegram message must still
fit, so it is truncated to this tighter bound in `_project_recommendations`
before the reply is constructed. Truncating here (rather than leaving the
model's own `max_length` as the only gate) means an over-length
`RecommenderResult.answer` truncates silently instead of raising
`ValidationError` on `ConversationReply(...)` construction."""


def _derive_clarification_key(question: str) -> str:
    """Map a clarification question to a namespaced preference key.

    The Recommender's `ask_user` tool records the question verbatim
    (bounded by `ClarificationRequest.question`'s 500-char cap). We
    scan for a small set of common phrasings and fall back to
    `general` — the preference value is the answer text, so an
    operator can always inspect the row to see what was asked even
    when the key defaulted.

    All returned keys are prefixed with `pref:clarified.` so the
    profile-enrichment provenance is greppable.
    """
    for pattern, derived in _CLARIFICATION_KEY_PATTERNS:
        if pattern.search(question):
            return f"{_CLARIFICATION_KEY_PREFIX}{derived}"
    return f"{_CLARIFICATION_KEY_PREFIX}{_CLARIFICATION_KEY_FALLBACK}"


def _rebuild_intent_from_last_run(
    conn: sqlite3.Connection, run_id: str, user_id: int
) -> SearchIntent | None:
    """Rebuild the `SearchIntent` the last Recommender run used.

    The Recommender persists the intent as `agent_runs.user_query`
    JSON on every completed loop. We fetch the row for `run_id`
    (filtered on `user_id` too — belt and braces against a raw-SQL
    stray) and reconstruct the intent via
    `SearchIntent.model_validate_json`. A missing row (audit write
    was disabled, or the run pre-dates M3.7) or a malformed JSON blob
    returns `None`; the caller falls back to a fresh interpret.
    """
    runs = query_agent_runs(conn, user_id=user_id, limit=100)
    for run in runs:
        if run.run_id == run_id:
            try:
                return SearchIntent.model_validate_json(run.user_query)
            except ValidationError:
                logger.warning(
                    "conversation.service: last run %s had a user_query that did "
                    "not roundtrip to SearchIntent; falling back to fresh interpret",
                    run_id,
                )
                return None
    return None


def _lookup_last_run_id(conn: sqlite3.Connection, user_id: int) -> str | None:
    """Return the `run_id` of the most recent Recommender run for `user_id`.

    `run_once` does not return the run_id, so the service reads the
    newest `agent_runs` row for the user right after the loop
    returns. The composition is documented in the task brief; the
    lookup is `LIMIT 1` on the composite `idx_agent_runs_user_started`
    index.
    """
    runs = query_agent_runs(conn, user_id=user_id, limit=1)
    return runs[0].run_id if runs else None


def _extract_detail_index(text: str) -> int | None:
    """Return the 0-indexed rank a "tell me about #N" message asks about.

    Users address candidates 1-indexed ("tell me about 2" means the
    second candidate), which is the convention every renderer uses.
    The `RecommendationRecord.rank_position` column is 0-indexed, so
    we subtract one before returning.

    A non-numeric text, a zero, or a negative value returns `None` —
    the caller falls through to the fresh-query path.
    """
    match = _DETAIL_PATTERN.match(text.strip())
    if match is None:
        return None
    try:
        raw = int(match.group(1))
    except ValueError:
        return None
    if raw < 1:
        return None
    return raw - 1


def _is_more_results(text: str) -> bool:
    """Return True when `text` matches the "more results" follow-up shape."""
    return _MORE_RESULTS_PATTERN.match(text.strip()) is not None


def _map_recommender_error(result: RecommenderResult) -> str:
    """Project a `RecommenderResult` error into the `ConversationReply.error_type` string.

    Both `status="error"` and `status="incomplete"` land here. An
    `error_type` on the result is passed through; `incomplete` uses
    the `stopped` value as the error type (truncated / max_steps).
    """
    if result.error_type is not None:
        return result.error_type
    if result.status == "incomplete":
        return f"incomplete_{result.stopped}"
    return "unknown_error"


def _handle_detail_lookup(
    conn: sqlite3.Connection, run_id: str, rank_index: int
) -> ConversationReply | None:
    """Return a `detail` reply for the Nth candidate under `run_id`, or `None`.

    `query_recommendations(run_id=..., limit=100)` returns the batch
    ordered by rank_position ascending (see the primitive's
    docstring). We index into that list by `rank_index` — a legal
    lookup returns a `detail` reply; an out-of-range index returns
    `None` so the caller can fall through to the fresh path.

    A `RecommendationRecord.event_id is None` (an ON DELETE SET NULL
    retention sweep dropped the target event) is a legitimate branch
    that also falls through to `None` — the record still exists but
    the underlying Event does not.
    """
    records = query_recommendations(conn, run_id=run_id, limit=100)
    if rank_index >= len(records):
        return None
    record = records[rank_index]
    if record.event_id is None:
        return None
    event = get_event_by_id(conn, record.event_id)
    if event is None:
        return None
    return ConversationReply(
        kind="detail",
        event=event,
        answer=_format_detail_answer(event),
    )


def _format_detail_answer(event: Event) -> str:
    """Render one `Event` as a bounded, single-line free-form summary.

    The transport layer (bot handler, CLI) may render the full
    `event` field directly for a rich card; the `answer` string is
    the plain-text fallback every surface can always render. Fields
    that are `None` are omitted rather than rendered as "None".
    """
    parts: list[str] = [event.title]
    parts.append(f"{event.start_utc.date().isoformat()}")
    if event.venue_name is not None:
        parts.append(event.venue_name)
    parts.append(event.city)
    parts.append(f"{event.category}")
    if event.price_cents == 0:
        parts.append("free")
    else:
        parts.append(f"{event.price_cents / 100:.2f} EUR")
    return " · ".join(parts)


def _run_and_capture(
    user_id: int, intent: SearchIntent, text: str | None = None
) -> tuple[RecommenderResult, str | None]:
    """Run one Recommender loop and return `(result, run_id)`.

    `run_once` does not expose its `run_id` in its return value; the
    convention (documented in the task brief) is to read the
    most-recent `agent_runs` row for the user right after the loop
    returns. The `run_id` is `None` when the audit write was disabled
    or failed (Rule 4 best-effort) — in that case the follow-up
    features degrade gracefully to "cannot re-run same intent".

    `text` is the raw user message this turn, if any, forwarded to
    `run_once` as push context (`docs/adr/0022-user-text-push-context.md`)
    so the Recommender's LLM can reason over nuance the interpreter's
    structured `SearchIntent` fields don't capture.
    """
    result = run_once(user_id, intent, text=text)
    # Best-effort: the audit surface is Rule 4 best-effort, so the
    # newest agent_runs row may be missing for a run that had a
    # legitimate best-effort audit failure. Falling back to `None`
    # here keeps the primary flow live even without a run_id.
    try:
        conn = _connect_for_lookup()
        try:
            run_id = _lookup_last_run_id(conn, user_id)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        logger.warning(
            "conversation.service: could not look up last run_id for user %d: %s",
            user_id,
            exc,
        )
        run_id = None
    return result, run_id


def _connect_for_lookup() -> sqlite3.Connection:
    """Open one connection for the audit-store lookups.

    Small indirection so tests can monkeypatch this if they want to
    isolate the run_id lookup from the shared `db.connect()` in the
    fresh path.
    """
    from planazo.storage import db

    return db.connect()


def _augment_state_after_result(
    state: ConversationState | None,
    user_id: int,
    result: RecommenderResult,
    run_id: str | None,
    intent: SearchIntent,
) -> ConversationState:
    """Compute the next `ConversationState` from a Recommender result.

    - `status="needs_clarification"` → store `pending_clarification`;
      keep the prior `last_recommendation_run_id` intact (the
      follow-ups it powers are still relevant if the user abandons
      the clarification).
    - `status in {"ok", "no_results"}` → clear `pending_clarification`
      and stamp `last_recommendation_run_id` with the new run_id.
    - Otherwise (error / incomplete) → preserve whatever the prior
      state had; a failed loop must not erase useful bookkeeping.
    """
    pending: PendingClarification | None
    last_run_id: str | None
    prior_pending = state.pending_clarification if state is not None else None
    prior_run_id = state.last_recommendation_run_id if state is not None else None

    if result.status == "needs_clarification" and result.clarification is not None:
        pending = PendingClarification(
            question=result.clarification.question, intent_snapshot=intent
        )
        last_run_id = prior_run_id
    elif result.status in ("ok", "no_results"):
        pending = None
        last_run_id = run_id if run_id is not None else prior_run_id
    else:
        pending = prior_pending
        last_run_id = prior_run_id

    return ConversationState(
        user_id=user_id,
        pending_clarification=pending,
        last_recommendation_run_id=last_run_id,
        updated_at=now_utc(),
    )


def _persist_state_best_effort(conn: sqlite3.Connection, state: ConversationState) -> None:
    """Upsert the scratchpad; swallow + log any DB failure (Rule 4).

    The Recommender's answer is the primary flow — a
    conversation-state write failure must not break it. Every branch
    of `handle_user_message` calls this once before returning, so a
    healthy DB always sees the latest state and an outage degrades
    to "state resets to last successful write".
    """
    try:
        upsert_state(conn, state)
    except (sqlite3.Error, OSError) as exc:
        logger.warning(
            "conversation.service: could not upsert state for user %d: %s",
            state.user_id,
            exc,
        )


def _project_recommendations(result: RecommenderResult) -> ConversationReply:
    """Project one `RecommenderResult` onto the matching `ConversationReply`.

    The `ok` branch carries `result.answer` through as the reply's `answer` —
    the Recommender's own brief natural-language summary, truncated to
    `_RECOMMENDATIONS_PREFACE_CAP` so it can never overrun the template
    `bot.commands.format_reply` renders it into. An empty or `None` answer
    stays `None`; `format_reply` treats that as "no preface" and falls back
    to the plain candidate list unchanged.
    """
    if result.status == "ok":
        answer = result.answer[:_RECOMMENDATIONS_PREFACE_CAP] if result.answer else None
        return ConversationReply(
            kind="recommendations", candidates=result.candidates, answer=answer
        )
    if result.status == "no_results":
        return ConversationReply(
            kind="no_results",
            answer=(
                result.answer if result.answer else "No matching events. Try broadening the query."
            ),
        )
    if result.status == "needs_clarification" and result.clarification is not None:
        return ConversationReply(kind="clarification", question=result.clarification.question)
    return ConversationReply(kind="error", error_type=_map_recommender_error(result))


def _handle_more_results(
    conn: sqlite3.Connection,
    user_id: int,
    state: ConversationState,
    prior_run_id: str,
    text: str,
) -> ConversationReply | None:
    """Return a fresh recommendations reply that excludes prior candidates.

    Rebuilds the intent from the prior run's `agent_runs.user_query`
    (JSON) and re-runs the Recommender. Excluded event IDs are the
    ones already persisted under `recommendations WHERE run_id =
    prior_run_id`. Filtering is client-side per plan decision 4 —
    the colleagues' `SearchIntent` has no `exclude_ids` field and
    reaching into their code is out of scope.

    `text` is the raw "more"/"otros ..." trigger message, forwarded to
    `run_once` as push context like every other Recommender call.

    `None` means "cannot re-run same intent" (missing prior run,
    corrupt JSON) — the caller falls through to the fresh path.
    """
    intent = _rebuild_intent_from_last_run(conn, prior_run_id, user_id)
    if intent is None:
        return None
    prior = query_recommendations(conn, run_id=prior_run_id, limit=100)
    excluded_ids = {rec.event_id for rec in prior if rec.event_id is not None}
    result, new_run_id = _run_and_capture(user_id, intent, text=text)
    if result.status != "ok":
        # Any non-ok branch we surface as-is; the state augmentation
        # is done by the caller after we return.
        reply = _project_recommendations(result)
        new_state = _augment_state_after_result(state, user_id, result, new_run_id, intent)
        _persist_state_best_effort(conn, new_state)
        return reply
    remaining = tuple(event for event in result.candidates if event.id not in excluded_ids)
    if not remaining:
        # Every candidate this run surfaced was already shown — a
        # legitimate "no_results" branch even though `run_once`
        # returned "ok". Preserve the state's last_recommendation_run_id
        # so the user can keep asking about the original batch.
        new_state = ConversationState(
            user_id=user_id,
            pending_clarification=None,
            last_recommendation_run_id=state.last_recommendation_run_id,
            updated_at=now_utc(),
        )
        _persist_state_best_effort(conn, new_state)
        return ConversationReply(
            kind="no_results",
            answer="No more results — every match has already been shown.",
        )
    # A non-empty remaining set is a fresh batch; stamp the new run_id
    # so future "tell me about #N" resolves against this run.
    new_state = ConversationState(
        user_id=user_id,
        pending_clarification=None,
        last_recommendation_run_id=new_run_id if new_run_id is not None else prior_run_id,
        updated_at=now_utc(),
    )
    _persist_state_best_effort(conn, new_state)
    return ConversationReply(kind="recommendations", candidates=remaining)


def _handle_clarification_answer(
    conn: sqlite3.Connection,
    user_id: int,
    state: ConversationState,
    text: str,
) -> ConversationReply:
    """Consume `text` as the user's answer to a pending clarification.

    Persists one `PreferenceRecord` under a
    `pref:clarified.<derived_key>` namespace (sanitized to a single
    line + length-capped by `PreferenceRecord`'s own validators).
    Clears `pending_clarification` before running a fresh
    `interpret + run_once` so the enriched profile is picked up on
    the next Recommender loop.

    A `PreferenceRecord` `ValidationError` (multi-line, over-long)
    swallows to a WARNING and continues to the re-interpret — the
    user's message is still useful as a fresh query even when the
    enrichment write failed.
    """
    assert state.pending_clarification is not None, (
        "caller guarantees pending_clarification is set on this branch"
    )
    derived_key = _derive_clarification_key(state.pending_clarification.question)
    try:
        set_preference(conn, user_id, derived_key, text)
    except ValidationError:
        logger.warning(
            "conversation.service: clarification answer for user %d failed "
            "PreferenceRecord validation; skipping enrichment",
            user_id,
        )
    except (sqlite3.Error, OSError) as exc:
        logger.warning(
            "conversation.service: could not persist clarification-derived "
            "preference for user %d: %s",
            user_id,
            exc,
        )
    # Re-run interpret + run_once with the same text; the fresh
    # preference row is already in the DB, so `run_once` will pick
    # it up as push context. ADR 0020 §D5: use `interpret_search_only`
    # so a router that would classify a numeric answer ("2") or a
    # short cue ("music") as chat still lands us in the search branch
    # — the clarification-answer path is the more specific state and
    # wins over the router.
    fresh_intent = interpret_search_only(text)
    result, new_run_id = _run_and_capture(user_id, fresh_intent, text=text)
    reply = _project_recommendations(result)
    new_state = _augment_state_after_result(
        state=None,  # cleared: pending was consumed
        user_id=user_id,
        result=result,
        run_id=new_run_id,
        intent=fresh_intent,
    )
    _persist_state_best_effort(conn, new_state)
    return reply


def _handle_fresh_query(
    conn: sqlite3.Connection,
    user_id: int,
    state: ConversationState | None,
    text: str,
) -> ConversationReply:
    """Run one fresh `interpret + (chat branch OR run_once)` turn.

    ADR 0020: the interpreter now returns a `RoutedMessage` — either
    `SearchRoute` (continue to `run_once` as before) or `ChatRoute`
    (return the LLM's own reply verbatim, no Recommender loop, no
    `agent_runs` / `recommendations` / `llm_decisions` row).

    A `ChatRoute` turn still refreshes `conversation_state.updated_at`
    so operator-activity queries see the turn happened — but nothing
    else about the state changes (no new `last_recommendation_run_id`,
    no `pending_clarification`).
    """
    routed = interpret(text)
    if routed.kind == "chat":
        return _handle_chat_route(conn, user_id, state, routed)
    intent = routed.intent
    result, new_run_id = _run_and_capture(user_id, intent, text=text)
    reply = _project_recommendations(result)
    new_state = _augment_state_after_result(state, user_id, result, new_run_id, intent)
    _persist_state_best_effort(conn, new_state)
    return reply


def _handle_chat_route(
    conn: sqlite3.Connection,
    user_id: int,
    state: ConversationState | None,
    routed: RoutedMessage,
) -> ConversationReply:
    """Return the router's `ChatRoute` reply verbatim.

    Called from the fresh-query path when `interpret()` classified the
    text as small-talk or a meta-question (ADR 0020). No Recommender
    loop, no LLM cost beyond the router turn itself. The scratchpad
    row's `updated_at` still refreshes so the audit trail sees the
    turn happened; every other field is preserved from the prior state.
    """
    assert routed.kind == "chat", "caller guarantees chat route"
    # Refresh updated_at while preserving whatever the prior state
    # carried (a pending clarification a user hasn't answered stays
    # pending; a prior recommendation run_id stays reachable for
    # follow-ups — a chat turn does not invalidate them).
    new_state = ConversationState(
        user_id=user_id,
        pending_clarification=state.pending_clarification if state is not None else None,
        last_recommendation_run_id=(
            state.last_recommendation_run_id if state is not None else None
        ),
        updated_at=now_utc(),
    )
    _persist_state_best_effort(conn, new_state)
    return ConversationReply(kind="chat", answer=routed.answer)


def handle_user_message(conn: sqlite3.Connection, user_id: int, text: str) -> ConversationReply:
    """Drive one turn of the multi-turn conversation for `user_id`.

    The single public entry point of the `conversation/` context.
    Reads `ConversationState`, dispatches to the appropriate
    follow-up path, and returns a `ConversationReply` the calling
    surface renders. Every branch upserts the scratchpad with the
    current timestamp before returning (best-effort per Rule 4).

    Precedence of the follow-up branches (highest priority first):

    1. **Detail lookup** — text parses as "tell me about #N" AND a
       `last_recommendation_run_id` is set. Highest priority so a
       numeric-only answer to a clarification does not accidentally
       trigger the detail path — the clarification-answer branch
       fires only when `pending_clarification` is set, and a fresh
       "2" without a clarification is unambiguously a detail lookup.
    2. **More results** — text matches the more-results pattern AND
       a `last_recommendation_run_id` is set.
    3. **Clarification answer** — `pending_clarification` is set.
       The text is consumed as the answer whatever its shape.
    4. **Fresh query** — none of the above. `interpret + run_once`
       with the current profile (including any preference rows just
       written by earlier clarification-answer turns).

    Precedence between (1)/(2) and (3): the follow-up patterns
    require the prior run_id to be set. When both a run_id AND a
    pending clarification exist (a user asked "more" after a
    successful `/find`, then a later clarification landed), the
    follow-up patterns win — they are more specific than "any text
    is a clarification answer".
    """
    stripped = text.strip()
    state = get_state(conn, user_id)

    # Follow-up paths (1) + (2): require a last_recommendation_run_id.
    if state is not None and state.last_recommendation_run_id is not None:
        detail_index = _extract_detail_index(stripped)
        if detail_index is not None:
            reply = _handle_detail_lookup(conn, state.last_recommendation_run_id, detail_index)
            if reply is not None:
                # No state mutation on a detail lookup — the batch and
                # any pending clarification are still relevant.
                new_state = ConversationState(
                    user_id=user_id,
                    pending_clarification=state.pending_clarification,
                    last_recommendation_run_id=state.last_recommendation_run_id,
                    updated_at=now_utc(),
                )
                _persist_state_best_effort(conn, new_state)
                return reply
            # Out-of-range N or FK-cleared event — fall through.

        if _is_more_results(stripped):
            reply = _handle_more_results(
                conn, user_id, state, state.last_recommendation_run_id, stripped
            )
            if reply is not None:
                return reply
            # Prior run's intent could not be rebuilt — fall through.

    # (3) Clarification-answer path.
    if state is not None and state.pending_clarification is not None:
        return _handle_clarification_answer(conn, user_id, state, stripped)

    # (4) Fresh-query path.
    return _handle_fresh_query(conn, user_id, state, stripped)
