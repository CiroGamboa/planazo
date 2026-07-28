"""Mypy-strict type-shape tests for `scheduler.service.ExtractorCallable`.

Locks the M11 plan fix — the callable alias `ExtractorCallable` names the
positional `(url, delegator_user_id)` contract, and the composition-root
lambda that adapts `agents.extractor.extract_once` (keyword-only for
`delegator_user_id`) into that shape must mypy-strict clean. If this file
starts to fail mypy under `uv run mypy src tests`, the alias has drifted or
`extract_once`'s signature has changed without the wire lambda being
updated.

The runtime assertions are secondary; the mypy-strict lane is the load-
bearing check. `assert callable(...)` keeps the tests non-vacuous so a
future maintainer removing the runtime line notices immediately.
"""

from __future__ import annotations

from planazo.agents.extractor import extract_once
from planazo.extraction.models import ExtractionResult
from planazo.scheduler.service import ExtractorCallable


def test_lambda_adapts_extract_once_to_extractor_callable_shape() -> None:
    # The wire the CLI uses (Stage 4 lands the actual CLI; this locks the
    # shape ahead of that). Mypy strict checks the closure's signature
    # matches ExtractorCallable's positional (str, int) → ExtractionResult.
    extractor: ExtractorCallable = lambda url, uid: extract_once(url, delegator_user_id=uid)  # noqa: E731
    assert callable(extractor)


def test_extractor_callable_accepts_positional_lambda() -> None:
    # A fake that ignores its arguments still satisfies the shape — this is
    # the pattern every service test uses to stub out the STRONG-tier call.
    def fake(url: str, delegator_user_id: int) -> ExtractionResult:
        del url, delegator_user_id
        return ExtractionResult(status="error", error_type="not_found", notes="")

    extractor: ExtractorCallable = fake
    assert callable(extractor)
