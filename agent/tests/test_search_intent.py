from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from planazo.schemas.events import SearchIntent


def _happy_intent(**overrides: object) -> SearchIntent:
    kwargs: dict[str, object] = {
        "start_utc": datetime(2026, 8, 1, 18, tzinfo=UTC),
        "end_utc": datetime(2026, 8, 1, 23, tzinfo=UTC),
        "city": "Barcelona",
        "categories": ("tech", "networking"),
        "radius_km": 2.0,
        "budget_cents": 1500,
    }
    kwargs.update(overrides)
    return SearchIntent(**kwargs)  # type: ignore[arg-type]


def test_happy_construction_sets_every_field_and_leaves_error_type_none() -> None:
    intent = _happy_intent()

    assert intent.start_utc == datetime(2026, 8, 1, 18, tzinfo=UTC)
    assert intent.end_utc == datetime(2026, 8, 1, 23, tzinfo=UTC)
    assert intent.city == "Barcelona"
    assert intent.categories == ("tech", "networking")
    assert intent.radius_km == 2.0
    assert intent.budget_cents == 1500
    assert intent.error_type is None


def test_end_before_start_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _happy_intent(
            start_utc=datetime(2026, 8, 1, 23, tzinfo=UTC),
            end_utc=datetime(2026, 8, 1, 18, tzinfo=UTC),
        )


def test_end_equal_to_start_is_accepted() -> None:
    # Point-in-time queries are legitimate — the model rejects `<`, not `==`.
    same = datetime(2026, 8, 1, 18, tzinfo=UTC)
    intent = _happy_intent(start_utc=same, end_utc=same)

    assert intent.start_utc == intent.end_utc


def test_unknown_category_is_rejected_as_iterable() -> None:
    with pytest.raises(ValidationError):
        _happy_intent(categories=("tech", "crypto"))


def test_unknown_category_is_rejected_as_csv_string() -> None:
    with pytest.raises(ValidationError):
        _happy_intent(categories="tech,crypto")


def test_duplicate_categories_normalize_and_preserve_order_as_iterable() -> None:
    intent = _happy_intent(categories=("tech", "tech", "music"))

    assert intent.categories == ("tech", "music")


def test_duplicate_categories_normalize_and_preserve_order_as_csv() -> None:
    intent = _happy_intent(categories="tech, tech ,music")

    assert intent.categories == ("tech", "music")


def test_negative_radius_km_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _happy_intent(radius_km=-1.0)


def test_negative_budget_cents_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _happy_intent(budget_cents=-1)


def test_radius_km_and_budget_cents_may_be_none() -> None:
    intent = _happy_intent(radius_km=None, budget_cents=None)

    assert intent.radius_km is None
    assert intent.budget_cents is None


def test_error_type_accepts_the_fallback_literal() -> None:
    intent = _happy_intent(error_type="interpreter_fallback")

    assert intent.error_type == "interpreter_fallback"


def test_error_type_rejects_any_other_string() -> None:
    with pytest.raises(ValidationError):
        _happy_intent(error_type="something_else")


def test_naive_and_aware_datetimes_both_normalize_to_aware_utc() -> None:
    intent = _happy_intent(
        start_utc=datetime(2026, 8, 1, 18),  # naive — stamped UTC by the validator
        end_utc=datetime(2026, 8, 1, 23, tzinfo=UTC),
    )

    assert intent.start_utc.tzinfo == UTC
    assert intent.end_utc.tzinfo == UTC
    assert intent.start_utc == datetime(2026, 8, 1, 18, tzinfo=UTC)
    assert intent.end_utc == datetime(2026, 8, 1, 23, tzinfo=UTC)


def test_iso_strings_mixed_aware_and_naive_also_normalize() -> None:
    intent = _happy_intent(
        start_utc="2026-08-01T18:00:00",
        end_utc="2026-08-01T23:00:00+00:00",
    )

    assert intent.start_utc.tzinfo == UTC
    assert intent.end_utc.tzinfo == UTC
    assert intent.start_utc == datetime(2026, 8, 1, 18, tzinfo=UTC)
    assert intent.end_utc == datetime(2026, 8, 1, 23, tzinfo=UTC)
