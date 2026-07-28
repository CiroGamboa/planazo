"""Unit tests for `planazo.extraction.tools.build_dispatch_extraction`.

Locks (1) the closure discipline that keeps `delegator_user_id` out of the
LLM-visible schema and rejects a crafted override, (2) byte-verbatim
passthrough of `ExtractionResult.model_dump(mode="json")` on both success
and error branches, and (3) composition-time positive-int validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from planazo.catalog.models import Event
from planazo.extraction import tools
from planazo.extraction.models import ExtractionResult


def _make_event() -> Event:
    return Event(
        source="instagram",
        source_url="https://www.instagram.com/p/ABC123/",
        title="Barcelona Techno Night",
        start_utc=datetime(2026, 8, 15, 22, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, 16, 4, 0, tzinfo=UTC),
        category="music",
        city="Barcelona",
        confidence=0.85,
    )


# --------------------------------------------------------------------------
# Composition-time validation.
# --------------------------------------------------------------------------


def test_build_dispatch_extraction_rejects_a_non_positive_delegator_id() -> None:
    # `MemoryScopeRequest` requires `user_id >= 1`; the Extractor's
    # `extraction_runs_index` FK expects the same. Failing here means a bad
    # id never reaches an LLM turn.
    with pytest.raises(ValidationError):
        tools.build_dispatch_extraction(0)
    with pytest.raises(ValidationError):
        tools.build_dispatch_extraction(-1)


# --------------------------------------------------------------------------
# Schema shape — the LLM sees exactly `url`.
# --------------------------------------------------------------------------


def test_dispatch_extraction_schema_exposes_url_only() -> None:
    schemas, registry = tools.build_dispatch_extraction(1)

    assert len(schemas) == 1
    schema = schemas[0]
    assert schema["name"] == "dispatch_extraction"
    parameters = schema["parameters"]
    assert set(parameters["properties"].keys()) == {"url"}
    assert parameters["required"] == ["url"]
    assert parameters["additionalProperties"] is False
    # No `delegator_user_id` leak — a captured free variable, not a parameter.
    assert "delegator_user_id" not in parameters["properties"]

    # Registry has exactly one entry pointing at the callable the schema
    # describes.
    assert set(registry.keys()) == {"dispatch_extraction"}
    assert callable(registry["dispatch_extraction"])


# --------------------------------------------------------------------------
# The inner callable — happy path passthrough + closure discipline.
# --------------------------------------------------------------------------


def test_inner_callable_returns_extract_once_result_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callable returns `ExtractionResult.model_dump(mode='json')` verbatim
    and forwards the *build-time* `delegator_user_id`, not any LLM-supplied one."""
    event = _make_event()
    fixed_result = ExtractionResult(
        status="ok",
        event=event,
        error_type=None,
        notes="short paraphrase",
    )
    seen_kwargs: dict[str, Any] = {}

    def stub(url: str, **kwargs: Any) -> ExtractionResult:
        seen_kwargs["url"] = url
        seen_kwargs.update(kwargs)
        return fixed_result

    # Patch the *name binding inside `extraction.tools`* — that is what the
    # callable's closure resolves. Patching `agents.extractor.extract_once`
    # would leave the already-bound reference intact.
    monkeypatch.setattr("planazo.extraction.tools.extract_once", stub)

    _, registry = tools.build_dispatch_extraction(7)
    dispatch = registry["dispatch_extraction"]

    output = dispatch(url="https://www.instagram.com/p/XYZ/")

    assert output == fixed_result.model_dump(mode="json")
    assert seen_kwargs["url"] == "https://www.instagram.com/p/XYZ/"
    # The delegator id came from the closure over the build-time argument.
    assert seen_kwargs["delegator_user_id"] == 7


@pytest.mark.parametrize(
    "error_type",
    [
        "rate_limited",
        "auth_failed",
        "not_found",
        "missing_date",
        "low_confidence_extraction",
        "ambiguous_content",
    ],
)
def test_inner_callable_passes_error_branches_through_unchanged(
    monkeypatch: pytest.MonkeyPatch, error_type: str
) -> None:
    """Rule 4: an `ExtractionResult` error branch reaches the LLM verbatim, no
    key renaming or wrapper."""
    status = (
        "needs_clarification" if error_type in {"missing_date", "ambiguous_content"} else "error"
    )
    error_result = ExtractionResult(
        status=status,  # type: ignore[arg-type]
        event=None,
        error_type=error_type,  # type: ignore[arg-type]
        notes="the model said so",
    )

    def stub(url: str, **kwargs: Any) -> ExtractionResult:
        return error_result

    monkeypatch.setattr("planazo.extraction.tools.extract_once", stub)

    _, registry = tools.build_dispatch_extraction(1)
    output = registry["dispatch_extraction"](url="https://x/y")

    assert output == error_result.model_dump(mode="json")


def test_inner_callable_rejects_a_crafted_delegator_user_id_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM cannot forge the delegator by crafting an extra kwarg — the
    callable's signature has only `url`, so `TypeError` fires cleanly (the
    same shape `build_memory_tools`'s tests rely on)."""

    def stub(url: str, **kwargs: Any) -> ExtractionResult:  # pragma: no cover — never called
        raise AssertionError("stub was reached — dispatch should have TypeError'd first")

    monkeypatch.setattr("planazo.extraction.tools.extract_once", stub)

    _, registry = tools.build_dispatch_extraction(1)
    dispatch = registry["dispatch_extraction"]

    with pytest.raises(TypeError) as excinfo:
        dispatch(url="https://x/y", delegator_user_id=999)  # type: ignore[call-arg]

    # Naming the parameter matters — a "missing argument" TypeError shape would
    # also be raised if the closure accepted the extra kwarg happily.
    assert "delegator_user_id" in str(excinfo.value)

    # Positive control — the identical call without the crafted kwarg reaches
    # the stub, which means the TypeError above was about the kwarg and not
    # about the call shape.
    fixed = ExtractionResult(status="ok", event=_make_event(), error_type=None)
    monkeypatch.setattr("planazo.extraction.tools.extract_once", lambda url, **_kw: fixed)
    _, registry = tools.build_dispatch_extraction(1)
    assert registry["dispatch_extraction"](url="https://x/y") == fixed.model_dump(mode="json")
