"""JSONL trace writer for one Extractor run.

Mirrors `planazo.monitor.logging.RunStepLogger` — same `RunStep` schema, same
`on_step` observer shape, same "append one line per call" discipline — but
writes to a single append-only file under `var/extraction_runs.jsonl` (one
extraction log the monitor can tail) instead of one file per run under
`data/runs/`. `agent="extractor"` on every line is the discriminator the
monitor's `run_id` join keys on (see ADR 0007).

`user_message` is a required constructor argument: it threads into every
`RunStep(user_message=..., ...)` write and closes the wire-format hole a
missing prompt would open (`RunStep.user_message` is a non-defaulted `str`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from planazo.agents.loop import LoopResult, StepRecord
from planazo.monitor.logging import model_tier_for
from planazo.monitor.models import RunStep, ToolCallTrace


def default_extraction_log_path() -> Path:
    """Return the repository-level file the extractor appends trace lines to.

    `audit.py` lives at `src/planazo/extraction/audit.py`; walking three
    parents up lands on the repo root — the same repository-root discipline
    `monitor.service.repository_root()` uses. The parent directory is
    created lazily on first write, so `var/` staying gitignored costs
    nothing at import time.
    """
    return Path(__file__).resolve().parents[3] / "var" / "extraction_runs.jsonl"


class ExtractionRunLogger:
    """Persist validated tool-dispatch + completion records for one Extractor run."""

    def __init__(
        self,
        *,
        run_id: str,
        url: str,
        delegator_user_id: int,
        user_message: str,
        model: str,
        output_path: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self._url = url
        self._delegator_user_id = delegator_user_id
        self._user_message = user_message
        self._model = model
        self._model_tier = model_tier_for(model)
        self._started_at = datetime.now(UTC)
        self._started_clock = perf_counter()
        self._output_path = output_path or default_extraction_log_path()

    def __call__(self, record: StepRecord) -> None:
        """Append one ``tool_dispatch`` trace line for the just-completed call."""
        step = RunStep(
            run_id=self.run_id,
            agent="extractor",
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
        self._append(step)

    def complete(self, result: LoopResult) -> None:
        """Append one ``completion`` trace line closing this Extractor run."""
        completion = RunStep(
            run_id=self.run_id,
            agent="extractor",
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
        self._append(completion)

    def _append(self, step: RunStep) -> None:
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with self._output_path.open("a", encoding="utf-8") as trace_file:
            trace_file.write(json.dumps(step.model_dump(mode="json"), separators=(",", ":")))
            trace_file.write("\n")
