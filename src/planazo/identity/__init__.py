"""The identity bounded context.

Owns user identity (`UserRecord`) and structured filter preferences
(`PreferenceRecord`), plus the repository primitives that read and write
both. External identity providers land here too as they arrive — Telegram's
`telegram_user_id` is the multi-user seam today, WhatsApp / frontend
adapters will plug in against the same aggregate.

Per [ADR 0008](../../../../../docs/adr/0008-domain-driven-module-layout.md):
one folder per aggregate cluster, models beside repository. This context
does not expose LLM tools directly — its data reaches the loop via
`event_agent`'s push context, not the tool registry.
"""

from planazo.identity.models import PreferenceRecord, UserRecord
from planazo.identity.repository import (
    get_or_create_user,
    get_preferences,
    set_preference,
)

__all__ = [
    "PreferenceRecord",
    "UserRecord",
    "get_or_create_user",
    "get_preferences",
    "set_preference",
]
