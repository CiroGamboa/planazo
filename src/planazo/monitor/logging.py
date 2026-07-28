"""JSONL trace writer attached to the Recommender's existing ``on_step`` seam."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from agentlib.core import CHEAP, STRONG
from planazo.agents.loop import LoopResult, StepRecord
from planazo.monitor.models import ModelTier, RunStep, ToolCallTrace


def default_run_log_dir() -> Path:
    """Return the repository-level directory reserved for Recommender trace logs."""
    return Path(__file__).resolve().parents[3] / "data" / "runs"


def model_tier_for(model: str) -> ModelTier:
    """Map a configured model identifier to the stable tier recorded in the trace."""
    if model == CHEAP:
        return "cheap"
    if model == STRONG:
        return "strong"
    return "custom"


class RunStepLogger:
    """Persist validated tool-dispatch records for one Recommender run."""

    def __init__(
        self,
        *,
        user_message: str,
        model: str,
        run_id: str | None = None,
        output_dir: Path | None = None,
    ) -> None:
        self.run_id = run_id or str(uuid4())
        self._user_message = user_message
        self._model = model
        self._model_tier = model_tier_for(model)
        self._started_at = datetime.now(UTC)
        self._started_clock = perf_counter()
        self._output_path = (output_dir or default_run_log_dir()) / f"{self.run_id}.jsonl"

    def __call__(self, record: StepRecord) -> None:
        """Validate and append one trace line immediately after a tool result exists."""
        step = RunStep(
            run_id=self.run_id,
            agent="recommender",
            started_at=self._started_at,
            recorded_at=datetime.now(UTC),
            model=self._model,
            model_tier=self._model_tier,
            user_message=self._user_message,
            step=record.step,
            wall_clock_ms=round((perf_counter() - self._started_clock) * 1000),
            phase="tool_dispatch",
            tool_calls=[
                ToolCallTrace(
                    name=record.tool,
                    arguments=record.arguments,
                    result=record.result,
                )
            ],
        )
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._output_path.open("a", encoding="utf-8") as trace_file:
            trace_file.write(json.dumps(step.model_dump(mode="json"), separators=(",", ":")))
            trace_file.write("\n")

    def complete(self, result: LoopResult) -> None:
        """Persist the terminal agent outcome so no-tool runs remain monitorable."""
        assert result.stopped != "preference_read_error", (
            "RunStep records actual loop terminals; pre-run failures must not be logged"
        )
        completion = RunStep(
            run_id=self.run_id,
            agent="recommender",
            started_at=self._started_at,
            recorded_at=datetime.now(UTC),
            model=self._model,
            model_tier=self._model_tier,
            user_message=self._user_message,
            step=result.steps,
            wall_clock_ms=round((perf_counter() - self._started_clock) * 1000),
            phase="completion",
            final_answer=result.answer,
            stopped=result.stopped,
        )
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._output_path.open("a", encoding="utf-8") as trace_file:
            trace_file.write(json.dumps(completion.model_dump(mode="json"), separators=(",", ":")))
            trace_file.write("\n")
