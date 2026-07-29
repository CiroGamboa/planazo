"""Validated contracts for persisted agent traces and monitor verdicts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

PromptAdherence = Literal["strictly_adheres", "minor_violation", "serious_violation"]
UntrustedContentHandling = Literal["safe", "near_miss", "obeyed"]
AgentName = Literal["recommender", "extractor", "curator"]
ModelTier = Literal["cheap", "strong", "custom"]
TracePhase = Literal["tool_dispatch", "completion"]


class Rationale(BaseModel):
    """What the judge expected and the observed behaviour that differed."""

    model_config = ConfigDict(extra="forbid")

    expected: str = Field(min_length=1)
    actual: str = Field(min_length=1)


class Verdict(BaseModel):
    """The judge's categorical assessment of one joined agent run."""

    model_config = ConfigDict(extra="forbid")

    prompt_adherence: PromptAdherence
    untrusted_content_handling: UntrustedContentHandling
    rationale: Rationale | None = None

    @model_validator(mode="after")
    def require_rationale_for_non_clean_verdict(self) -> Verdict:
        if (
            self.prompt_adherence != "strictly_adheres" or self.untrusted_content_handling != "safe"
        ) and self.rationale is None:
            raise ValueError("non-clean verdicts require a rationale")
        return self


class ToolCallTrace(BaseModel):
    """One tool call and its boundary-validated JSON result."""

    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    result: JsonValue


class RunStep(BaseModel):
    """One persisted tool-dispatch trace line from a Recommender or Extractor run."""

    run_id: str = Field(min_length=1)
    agent: AgentName
    started_at: datetime
    recorded_at: datetime
    model: str = Field(min_length=1)
    model_tier: ModelTier
    user_message: str
    step: int = Field(ge=1)
    wall_clock_ms: int = Field(ge=0)
    phase: TracePhase = "tool_dispatch"
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    final_answer: str | None = None
    stopped: Literal["answered", "truncated", "max_steps"] | None = None

    @model_validator(mode="after")
    def validate_trace_phase(self) -> RunStep:
        if self.phase == "tool_dispatch" and not self.tool_calls:
            raise ValueError("tool-dispatch trace entries require at least one tool call")
        if self.phase == "completion" and self.stopped is None:
            raise ValueError("completion trace entries require a stop reason")
        return self


class RunSession(BaseModel):
    """All trace lines joined by ``run_id`` before a judge evaluates the run."""

    run_id: str = Field(min_length=1)
    started_at: datetime
    steps: list[RunStep] = Field(min_length=1)


class GradedRun(BaseModel):
    """One serializable monitor result written to the JSONL sidecar."""

    run_id: str
    started_at: datetime
    verdict: Verdict
