"""The `extraction` bounded context's aggregate — the delegation hand-off contract.

`ExtractionResult` is what the Extractor returns to the Recommender when
`dispatch_extraction` completes. It is neither an `Event` (that lives in
`catalog/models.py`) nor a transient loop record (that lives in
`agents/loop.py::LoopResult`) — it is the cross-agent hand-off shape both
composition roots depend on. Per ADR 0008 each aggregate goes into its own
bounded context; per the plan for #17 this context is `extraction/` (M2
having claimed `sources/` and retired ADR 0008's `discovery/` placeholder).

The `(status, error_type, event)` invariant is enforced by a Pydantic v2
`model_validator(mode="after")`: `status == "ok"` requires `error_type is None`
and `event is not None`; `status in {"error", "needs_clarification"}` requires
`error_type is not None` and `event is None`. Any other combination raises
`ValidationError`. `notes` is capped at 200 characters — the code-shape
enforcement of AGENTS.md Rule 2 for the notes surface (paraphrases can pass
through, wholesale caption quoting cannot).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from planazo.catalog.models import Event

ExtractionStatus = Literal["ok", "error", "needs_clarification"]

ExtractionErrorType = Literal[
    "unsupported_source",
    "rate_limited",
    "auth_failed",
    "not_found",
    "unsupported_media",
    "low_confidence_extraction",
    "missing_date",
    "location_out_of_metro",
    "multiple_events_in_post",
    "ambiguous_content",
    "no_visual_asset",
    "save_event_failed",
]


class ExtractionResult(BaseModel):
    """The cross-agent hand-off shape returned by ``extract_once``.

    `status == "ok"` — extraction succeeded; `event` carries the persisted
    `Event`, `error_type` is `None`. `status == "error"` — the Extractor
    surfaced a typed failure (adapter-side, LLM-side, or budget cap); `event`
    is `None`, `error_type` names the branch. `status == "needs_clarification"`
    — the LLM signalled ambiguity or a multi-event carousel; `event` is
    `None`, `error_type` names the branch. `needs_approval` is fixed at
    `False` — the Extractor never gates back to its delegator (`save_event`
    is not in `IRREVERSIBLE_TOOLS`; see ADR 0002 + ADR 0005 decision 4).
    """

    model_config = ConfigDict(extra="forbid")

    status: ExtractionStatus
    event: Event | None = None
    needs_approval: Literal[False] = False
    notes: str = Field(default="", max_length=200)
    error_type: ExtractionErrorType | None = None

    @model_validator(mode="after")
    def enforce_status_error_type_event_invariant(self) -> ExtractionResult:
        if self.status == "ok":
            if self.error_type is not None:
                raise ValueError("status='ok' forbids error_type")
            if self.event is None:
                raise ValueError("status='ok' requires event")
        else:
            if self.error_type is None:
                raise ValueError(f"status={self.status!r} requires error_type")
            if self.event is not None:
                raise ValueError(f"status={self.status!r} forbids event")
        return self
