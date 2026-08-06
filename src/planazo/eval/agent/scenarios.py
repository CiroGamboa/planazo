"""Loader for the committed agent-eval scenario JSONL.

Mirrors ``planazo.eval.dataset.load_golden_cases``: strict validation, one
row per line, no silent skipping. This is fixture data committed by the
maintainers, not user input — a bad row is a bug to fix in the file, not
one to swallow.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from planazo.eval.agent.models import AgentEvalCase


def load_agent_scenarios(path: Path) -> list[AgentEvalCase]:
    """Read ``path`` (a JSONL of ``AgentEvalCase`` rows) into a validated list.

    Raises ``ValueError`` on any line that isn't valid JSON or that
    Pydantic rejects. The error message carries the file path and 1-based
    line number so the maintainer can find the offending row.
    """
    cases: list[AgentEvalCase] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            try:
                cases.append(AgentEvalCase.model_validate(payload))
            except ValidationError as exc:
                raise ValueError(f"{path}:{line_number}: invalid AgentEvalCase") from exc
    return cases


__all__ = ["load_agent_scenarios"]
