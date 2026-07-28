"""Contract tests for ``planazo.extraction.audit.ExtractionRunLogger``.

Locks the JSONL wire format (`RunStep(agent="extractor", ...)`), the required
`user_message` constructor argument, and the "parent directory created on
first append" behaviour that lets `var/` stay gitignored.
"""

from __future__ import annotations

import json
from pathlib import Path

from planazo.agents.loop import LoopResult, StepRecord
from planazo.extraction.audit import ExtractionRunLogger
from planazo.monitor.models import RunStep


def _logger(*, output_path: Path, user_message: str = "Extract this post.") -> ExtractionRunLogger:
    return ExtractionRunLogger(
        run_id="run-abc",
        url="https://www.instagram.com/p/abc123/",
        delegator_user_id=1,
        user_message=user_message,
        model="gpt-5.4-nano",
        output_path=output_path,
    )


def _read_lines(path: Path) -> list[RunStep]:
    return [
        RunStep.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_call_writes_one_tool_dispatch_line(tmp_path: Path) -> None:
    output = tmp_path / "extraction_runs.jsonl"
    logger = _logger(output_path=output, user_message="Extract this post.")
    record = StepRecord(
        step=1,
        tool="fetch_instagram_post",
        arguments={"url": "https://www.instagram.com/p/abc123/"},
        result={"caption": "hello", "media": []},
    )

    logger(record)

    steps = _read_lines(output)
    assert len(steps) == 1
    step = steps[0]
    assert step.agent == "extractor"
    assert step.phase == "tool_dispatch"
    assert step.user_message == "Extract this post."
    assert step.model_tier == "cheap"
    assert step.step == 1
    assert len(step.tool_calls) == 1
    assert step.tool_calls[0].name == "fetch_instagram_post"
    assert step.tool_calls[0].arguments == {"url": "https://www.instagram.com/p/abc123/"}


def test_complete_writes_one_completion_line(tmp_path: Path) -> None:
    output = tmp_path / "extraction_runs.jsonl"
    logger = _logger(output_path=output, user_message="Extract this post.")

    logger.complete(LoopResult(answer="done", steps=3, stopped="answered"))

    steps = _read_lines(output)
    assert len(steps) == 1
    step = steps[0]
    assert step.agent == "extractor"
    assert step.phase == "completion"
    assert step.stopped == "answered"
    assert step.final_answer == "done"
    assert step.user_message == "Extract this post."
    assert step.step == 3


def test_call_then_complete_appends_two_lines(tmp_path: Path) -> None:
    output = tmp_path / "extraction_runs.jsonl"
    logger = _logger(output_path=output)
    logger(
        StepRecord(
            step=1,
            tool="fetch_instagram_post",
            arguments={"url": "u"},
            result={"caption": "c", "media": []},
        )
    )
    logger.complete(LoopResult(answer=None, steps=1, stopped="max_steps"))

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["phase"] == "tool_dispatch"
    assert second["phase"] == "completion"
    assert second["stopped"] == "max_steps"


def test_parent_directory_is_created_on_first_append(tmp_path: Path) -> None:
    output = tmp_path / "missing_dir" / "extraction_runs.jsonl"
    assert not output.parent.exists()

    logger = _logger(output_path=output)
    logger.complete(LoopResult(answer="done", steps=1, stopped="answered"))

    assert output.exists()
    assert len(_read_lines(output)) == 1


def test_empty_user_message_still_validates(tmp_path: Path) -> None:
    output = tmp_path / "extraction_runs.jsonl"
    logger = _logger(output_path=output, user_message="")
    logger.complete(LoopResult(answer="done", steps=1, stopped="answered"))

    steps = _read_lines(output)
    assert steps[0].user_message == ""


def test_missing_user_message_is_a_type_error(tmp_path: Path) -> None:
    # `user_message` is required — mypy strict flags this call as
    # `[call-arg]`. The runtime raises `TypeError` because there is no
    # default; the ignore is the static-type-error assertion.
    try:
        ExtractionRunLogger(  # type: ignore[call-arg]
            run_id="run-abc",
            url="u",
            delegator_user_id=1,
            model="gpt-5.4-nano",
            output_path=tmp_path / "x.jsonl",
        )
    except TypeError:
        return
    raise AssertionError("ExtractionRunLogger without user_message must raise TypeError")
