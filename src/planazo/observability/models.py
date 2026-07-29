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
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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

RATIONALE_CAP: Final[int] = 500
"""Maximum length of the sanitized `LLMDecision.rationale` field.

Set at 500 rather than the 2000 used for the two `agent_runs` free-form
columns because a decision rationale is the LLM's terse per-decision
reasoning (`report_extraction_status.notes` is already capped at 200; a
`save_event` synthetic rationale is bounded by `Event.title` length),
not a whole conversation transcript. 500 leaves headroom above the
`notes` cap while still keeping the corpus M4's ranker will read against
compact enough to store many thousands of rows without bloating the DB.
"""

RECOMMENDATION_REASON_CAP: Final[int] = 500
"""Maximum length of the sanitized `RecommendationRecord.reason` field.

Matches `RATIONALE_CAP`: the ranker's per-candidate `reason` is bounded
by `MAX_REASON_CHARS = 240` at `RankedEvent`'s construction site, so 500
gives comfortable headroom above that cap while keeping the corpus a
`/find` history reader will query against compact. A ranker that emits
longer strings than 240 still fits; nothing above 500 does, matching the
discipline `LLMDecision.rationale` established.
"""

FINAL_ANSWER_CAP: Final[int] = 2000
"""Maximum length of the sanitized `AgentRunRecord.final_answer` field.

The Recommender's `max_output_tokens` cap already keeps `LoopResult.answer`
bounded well below 2000 characters. This is the DB-side belt-and-braces
in case a downstream loop composition ships without an output cap, or a
future agent produces longer answers.
"""

DecisionKind = Literal[
    "save_event",
    "needs_clarification",
    "error",
    "answered",
    "archive",
    "merge",
    "update_category",
]
"""The seven terminal LLM decisions `llm_decisions.decision_kind` records.

Recommender + Extractor decisions (M3.6):

- `save_event` — the Extractor's LLM issued a successful `save_event`
  call. One `LLMDecision` row per persisted `Event`; `event_db_id`
  points at the row.
- `needs_clarification` — the Extractor's LLM issued
  `report_extraction_status(status="needs_clarification", ...)`. Ambiguous
  post (missing date, out-of-metro venue, multi-event carousel).
  `error_type` names the branch.
- `error` — a typed failure branch. Emitted by the Extractor for a
  `report_extraction_status(status="error", ...)` call, and by both
  composition roots on a loop terminated by `truncated` / `max_steps`.
- `answered` — the Recommender's LLM produced a final text answer with
  no tool calls. Both `event_db_id` and `error_type` are `None`; the
  Recommender does not project structured decisions into this table
  today (per-item reasoning is deferred to M4 #20).

Curator decisions (ADR 0020):

- `archive` — the curator's LLM issued `archive_event(id, reason)`.
  `event_db_id` points at the archived row; `error_type=None`.
- `merge` — the curator's LLM issued `merge_events(keep_id, archive_ids,
  reason)`. One `LLMDecision` row per archived id in the merge group;
  `event_db_id` points at each archived row.
- `update_category` — the curator's LLM issued
  `update_event_category(id, new_category, reason)`. `event_db_id`
  points at the corrected row; `error_type=None`.
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


class LLMDecision(BaseModel):
    """One `llm_decisions` row — one terminal decision the LLM produced.

    Field-for-field mirror of the SQL schema in
    `planazo/storage/migrations/004_llm_decisions.sql`. The four
    `decision_kind` branches have different required-field shapes:

    - `save_event` — `event_db_id` required (the row the LLM's tool call
      persisted); `error_type` must be `None`.
    - `needs_clarification` / `error` — `error_type` required (the typed
      branch the LLM signalled); `event_db_id` must be `None`.
    - `answered` — both `event_db_id` and `error_type` must be `None`
      (a Recommender loop that produced a final text answer; per-item
      reasoning is deferred to M4 #20).

    The `rationale` field is DB-inside per AGENTS.md Rule 2's rationale
    hook: full LLM reasoning is allowed, subject to length cap
    (`RATIONALE_CAP`) + `format_stored_text` sanitization. Callers build
    the field through the helper; the after-validator's regex re-check
    is defense-in-depth against a caller that bypassed the sanitizer,
    same shape as `AgentRunRecord.user_query` / `final_answer`.

    Redaction from the audit surface out to any model-visible or
    operator-facing projection is the reader's responsibility, matching
    the discipline already established for `agent_runs` free-form text.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    decision_kind: DecisionKind
    event_db_id: int | None = None
    error_type: str | None = None
    rationale: str = Field(max_length=RATIONALE_CAP)
    recorded_at: datetime

    @field_validator("rationale")
    @classmethod
    def _validate_sanitized_rationale(cls, value: str) -> str:
        """Reject an unsanitized rationale at the field boundary.

        The regex bans every C0/C1 control character and DEL — a caller
        who forgets `format_stored_text` fails here rather than pushing
        raw caption bytes into the DB. Length is separately field-capped
        so the two invariants stay independently readable.
        """
        if not _SANITIZED_TEXT_PATTERN.fullmatch(value):
            raise ValueError(
                "LLMDecision.rationale must be sanitized — build it "
                "with observability.models.format_stored_text"
            )
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Self:
        """Enforce the four `decision_kind` → required-field shapes.

        The four branches encode a state machine the DB CHECK cannot
        express in one clause: `save_event` writes a pointer to an
        `events` row, `needs_clarification`/`error` name a typed failure
        branch, and `answered` carries a Recommender text-answer
        terminal with neither structured artifact. A mismatch is a
        caller bug (a hand-composed test fixture, a raw-SQL write path
        that skipped this aggregate) — surface it loudly at the model
        boundary rather than persisting a row whose shape violates the
        four-way invariant.
        """
        # `save_event` (Extractor) and `archive`/`merge`/`update_category`
        # (Curator, ADR 0020) share the same shape: a required pointer to
        # the affected `events` row and no `error_type`.
        pointer_kinds = ("save_event", "archive", "merge", "update_category")
        if self.decision_kind in pointer_kinds:
            if self.event_db_id is None:
                raise ValueError(f"decision_kind={self.decision_kind!r} requires event_db_id")
            if self.error_type is not None:
                raise ValueError(f"decision_kind={self.decision_kind!r} requires error_type=None")
        elif self.decision_kind in ("needs_clarification", "error"):
            if self.error_type is None:
                raise ValueError(f"decision_kind={self.decision_kind!r} requires error_type")
            if self.event_db_id is not None:
                raise ValueError(f"decision_kind={self.decision_kind!r} requires event_db_id=None")
        else:  # "answered"
            if self.event_db_id is not None:
                raise ValueError("decision_kind='answered' requires event_db_id=None")
            if self.error_type is not None:
                raise ValueError("decision_kind='answered' requires error_type=None")
        return self


class RecommendationRecord(BaseModel):
    """One `recommendations` row — one candidate the Recommender surfaced.

    Field-for-field mirror of the SQL schema in
    `planazo/storage/migrations/005_recommendations.sql`. A completed
    Recommender loop with `RecommenderResult.status in {"ok",
    "no_results"}` produces 0..N of these — one per candidate for `ok`,
    zero for `no_results`.

    `event_id` is nullable because the FK is `ON DELETE SET NULL`: a
    future retention sweep that deletes stale `events` rows must not
    cascade-delete the audit rows documenting that we once recommended
    them. `score` and `reason` are nullable because today (M3.7 T1) the
    Recommender does not invoke the deterministic ranker — `run_once`
    returns filtered but unranked candidates, so persistence lands with
    `score=None`, `reason=None`. The columns exist for the follow-up
    ticket that wires `rank_events` at the composition root.

    `reason` is DB-inside per AGENTS.md Rule 2's rationale hook: full
    ranker reasoning is allowed subject to `RECOMMENDATION_REASON_CAP` +
    `format_stored_text` sanitization enforced at construction. The
    field validator's regex re-check is defense-in-depth against a
    caller that bypassed the sanitizer — same discipline as
    `LLMDecision.rationale` / `AgentRunRecord.user_query`.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    event_id: int | None = None
    rank_position: int = Field(ge=0)
    score: float | None = None
    reason: str | None = Field(default=None, max_length=RECOMMENDATION_REASON_CAP)
    recorded_at: datetime

    @field_validator("reason")
    @classmethod
    def _validate_sanitized_reason(cls, value: str | None) -> str | None:
        """Reject an unsanitized `reason` at the field boundary.

        `None` is a legitimate value (see class docstring — today the
        ranker is not wired). A non-None value must pass the shared
        `_SANITIZED_TEXT_PATTERN` regex, same defense-in-depth shape as
        `LLMDecision.rationale`.
        """
        if value is None:
            return value
        if not _SANITIZED_TEXT_PATTERN.fullmatch(value):
            raise ValueError(
                "RecommendationRecord.reason must be sanitized — build it "
                "with observability.models.format_stored_text"
            )
        return value
