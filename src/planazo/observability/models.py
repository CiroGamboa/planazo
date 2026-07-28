"""Pydantic v2 aggregate for one persisted agent run — the `agent_runs` row.

`AgentRunRecord` mirrors the `agent_runs` schema field-for-field. Both
free-form text columns (`user_query`, `final_answer`) pass through the
`format_stored_text` sanitizer at construction and are locked at the
model boundary by a regex that fires even when a caller bypasses the
helper. Same defense-in-depth shape as `SchedulerRunRecord.errors` /
`scheduler.models.format_error_entry`:

- `format_stored_text` is the ergonomic front door: strip C0 (0x00-0x1F
  except tab), C1 (0x80-0x9F) and DEL (0x7F) control characters, collapse
  runs of whitespace to single spaces, strip surrounding whitespace, and
  truncate to `cap` code points.
- The model's after-validator re-checks the sanitized shape at the
  aggregate boundary, so a caller who forgets the helper still fails at
  `AgentRunRecord.__init__` rather than pushing raw caption bytes into
  the DB.

Text stored in the DB is inside the trust boundary — redaction happens
on the way OUT (to the Recommender's response surface, to `/find`
answers, to any operator-facing tool) rather than on the way in. That
keeps the audit trail useful (an operator can inspect what an LLM
produced verbatim) while making sure Rule 2 is enforced at every seam
that projects `agent_runs` rows onto a model-visible surface.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from planazo.monitor.models import AgentName

USER_QUERY_CAP: Final[int] = 2000
"""Maximum length of the sanitized `AgentRunRecord.user_query` field.

The Telegram bot's user text is capped well below this at the input
surface (`bot/models.py`), and Recommender-side `USER_MESSAGE` /
Extractor-side `USER_MESSAGE` are both fixed literals under 200
characters. 2000 gives the audit trail room for a hypothetical future
free-form composer without allowing a runaway model or scraped payload
to bloat the table.
"""

FINAL_ANSWER_CAP: Final[int] = 2000
"""Maximum length of the sanitized `AgentRunRecord.final_answer` field.

The Recommender's `max_output_tokens` cap already keeps `LoopResult.answer`
bounded well below 2000 characters. This is the DB-side belt-and-braces
in case a downstream loop composition ships without an output cap, or a
future agent produces longer answers.
"""

AgentRunStopped = Literal["answered", "truncated", "max_steps"]
"""The three terminal states `agent_runs.stopped` records.

Mirrors the DB CHECK-like taxonomy. `LoopResult.stopped` also carries
`preference_read_error`, but that branch fires BEFORE the loop starts —
it never produces an `AgentRunRecord` because the composition root
returns early. Recording it would violate the invariant that every
`agent_runs` row corresponds to at least one LLM turn.
"""

_SANITIZED_TEXT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[^\x00-\x1F\x7F-\x9F]*$")
"""Regex `user_query` / `final_answer` must match at the model boundary.

Bans every C0 control character (0x00-0x1F, tab included — tab is
whitespace and `format_stored_text` collapses it to a space in step 2),
DEL (0x7F), and every C1 control character (0x80-0x9F). Anchored on
both ends so a stray control byte at the tail cannot slip through.
Length is enforced separately in `_validate_sanitized_text` so the
regex-vs-length invariants stay independently readable.
"""

_INTERNAL_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")
"""Whitespace-run pattern for `format_stored_text`'s collapse step."""

_CONTROL_CHARS_TO_STRIP: Final[frozenset[str]] = frozenset(
    # C0 controls except tab — tab is whitespace and gets collapsed to a
    # space in the next step. Newlines, carriage returns, vertical tab,
    # form feed, and every other 0x00-0x1F byte become a space here.
    {chr(c) for c in range(0x20) if c != 0x09}
    # DEL — 0x7F — is not part of C0 but is treated identically to a
    # control character by every terminal-facing surface.
    | {chr(0x7F)}
    # C1 controls — 0x80-0x9F — the ISO-8859 supplementary control block.
    | {chr(c) for c in range(0x80, 0xA0)}
)


def format_stored_text(text: str, cap: int) -> str:
    """Sanitize a free-form string for persistence in `agent_runs`.

    Mirrors the shape of `planazo.scheduler.models.format_error_entry`
    (the audit-log entry sanitizer for `SchedulerRunRecord.errors`) but
    with a configurable `cap` and without the `<error_type>: ` prefix.
    AGENTS.md rule 2 — stored text is inside the trust boundary, but
    injecting a caption's newline or NUL byte into the DB would still
    corrupt every downstream reader that assumes UTF-8-safe printable
    content. The helper:

    1. Replaces every C0 control except tab (0x00-0x1F excluding 0x09),
       DEL (0x7F), and every C1 control (0x80-0x9F) with a single space.
       Tab stays through this step because step 2 collapses it in with
       other whitespace runs; the net effect is that tab becomes a
       single space alongside spaces, newlines, and any other
       whitespace.
    2. Collapses every run of one-or-more whitespace characters (as
       recognised by Python's `\\s`) to a single space and strips
       surrounding whitespace, so the sanitized text is one line with
       single spaces between tokens.
    3. Truncates to `cap` Unicode code points. Truncation is on
       characters, not bytes, so a multi-byte code point at the boundary
       never produces invalid UTF-8.

    An empty `text` is a legitimate input (Extractor runs use the fixed
    `USER_MESSAGE`; some `LoopResult.answer` branches carry an empty
    string) and returns an empty string. `cap` must be >= 1; a `cap` of
    0 would truncate everything to `""` and is treated as a caller bug.
    """
    if cap < 1:
        raise ValueError(f"format_stored_text cap must be >= 1, got {cap}")
    cleaned = "".join(" " if ch in _CONTROL_CHARS_TO_STRIP else ch for ch in text)
    cleaned = _INTERNAL_WHITESPACE_PATTERN.sub(" ", cleaned).strip()
    return cleaned[:cap]


class AgentRunRecord(BaseModel):
    """One `agent_runs` row — a completed Recommender or Extractor loop.

    Field-for-field mirror of the SQL schema in
    `planazo/storage/migrations/003_agent_runs.sql`. Both free-form text
    fields (`user_query`, `final_answer`) are validated at the model
    boundary against `_SANITIZED_TEXT_PATTERN`, so a caller that
    bypassed `format_stored_text` fails here rather than persisting
    unsanitized text. Length caps (`USER_QUERY_CAP`, `FINAL_ANSWER_CAP`)
    are enforced in the same after-validator so the two invariants stay
    on one seam.

    `user_id` is nullable because operator-triggered runs outside a
    Telegram session (e.g. `planazo-agent "<prompt>"` on a dev host with
    no seeded users row) have no identity to attribute to. `final_answer`
    is nullable because `stopped='max_steps'` leaves `LoopResult.answer`
    as `None`. The `stopped` Literal excludes `preference_read_error` —
    that branch aborts before any LLM turn fires, so no `agent_runs`
    row is produced for it (see `AgentRunStopped` docstring).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    agent_kind: AgentName
    user_id: int | None = None
    user_query: str = Field(max_length=USER_QUERY_CAP)
    final_answer: str | None = Field(default=None, max_length=FINAL_ANSWER_CAP)
    stopped: AgentRunStopped
    steps_count: int = Field(ge=0)
    started_at: datetime
    ended_at: datetime

    @model_validator(mode="after")
    def _validate_sanitized_text(self) -> AgentRunRecord:
        if not _SANITIZED_TEXT_PATTERN.fullmatch(self.user_query):
            raise ValueError(
                "AgentRunRecord.user_query must be sanitized — build it "
                "with observability.models.format_stored_text"
            )
        if self.final_answer is not None and not _SANITIZED_TEXT_PATTERN.fullmatch(
            self.final_answer
        ):
            raise ValueError(
                "AgentRunRecord.final_answer must be sanitized — build it "
                "with observability.models.format_stored_text"
            )
        return self
