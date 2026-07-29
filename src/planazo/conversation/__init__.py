"""The conversation bounded context — multi-turn `/find` service.

Owns the per-user scratchpad that turns the stateless `run_once`
Recommender loop into a stateful, multi-turn conversation:
`ConversationState` (one `conversation_state` row per user, upserted
every message), `PendingClarification` (JSON blob stored in
`conversation_state.pending_clarification`), and the
`handle_user_message` composition root the bot's `/find` handler
plus any CLI helper calls.

Per [ADR 0008](../../../../../docs/adr/0008-domain-driven-module-layout.md):
one folder per aggregate cluster, models beside repository beside
service. `service.py` is intentionally composition-root shape — it
imports the primitives from other contexts (`query`, `identity`,
`observability`, `catalog`, `agents.event_agent`) rather than
mirroring them, so the per-context tests still exercise their own
primitives independently.

Per [ADR 0016](../../../../../docs/adr/0016-multi-turn-recommender-conversation.md):
DB-backed conversation state, preference-namespaced profile
enrichment, per-user single row, client-side "more results" filter,
one shared `interpret + run_once` seam.
"""

from planazo.conversation.models import (
    ConversationReply,
    ConversationReplyKind,
    ConversationState,
    PendingClarification,
)
from planazo.conversation.repository import get_state, now_utc, upsert_state
from planazo.conversation.service import handle_user_message

__all__ = [
    "ConversationReply",
    "ConversationReplyKind",
    "ConversationState",
    "PendingClarification",
    "get_state",
    "handle_user_message",
    "now_utc",
    "upsert_state",
]
