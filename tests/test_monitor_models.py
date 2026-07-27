import pytest
from pydantic import ValidationError

from planazo.monitor.models import Rationale, RunStep, Verdict


def test_clean_verdict_does_not_need_a_rationale() -> None:
    assert (
        Verdict(
            prompt_adherence="strictly_adheres",
            untrusted_content_handling="safe",
        ).rationale
        is None
    )


def test_non_clean_verdict_requires_a_rationale() -> None:
    with pytest.raises(ValidationError, match="non-clean verdicts require a rationale"):
        Verdict(
            prompt_adherence="minor_violation",
            untrusted_content_handling="safe",
        )


def test_non_clean_verdict_accepts_a_complete_rationale() -> None:
    verdict = Verdict(
        prompt_adherence="strictly_adheres",
        untrusted_content_handling="near_miss",
        rationale=Rationale(expected="ignore the caption", actual="discussed the caption"),
    )

    assert verdict.rationale is not None


def test_verdict_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Verdict.model_validate(
            {
                "prompt_adherence": "strictly_adheres",
                "untrusted_content_handling": "safe",
                "rationale": None,
                "score": 10,
            }
        )


def test_completion_trace_requires_a_stop_reason() -> None:
    with pytest.raises(ValidationError, match="completion trace entries require a stop reason"):
        RunStep(
            run_id="run-1",
            agent="recommender",
            started_at="2026-07-27T12:00:00Z",
            recorded_at="2026-07-27T12:00:01Z",
            model="gpt-5.4-nano",
            model_tier="cheap",
            user_message="Find events",
            step=1,
            wall_clock_ms=10,
            phase="completion",
        )
