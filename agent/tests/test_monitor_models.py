import pytest
from pydantic import ValidationError

from planazo.monitor.models import Rationale, Verdict


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
