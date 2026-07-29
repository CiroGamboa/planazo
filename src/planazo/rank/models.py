"""Validated public models for deterministic event ranking."""

from __future__ import annotations

# `Any` is limited to Pydantic's untrusted pre-validation input. The validator
# immediately narrows it to the concrete public boundary types below.
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from planazo.catalog.models import Event

MAX_PREFERENCE_TOKENS = 20
MAX_PREFERENCE_TOKEN_CHARS = 64
MAX_REASON_CHARS = 240


class RankingPreferences(BaseModel):
    """The deliberately narrow, normalized preference projection for ranking."""

    model_config = ConfigDict(frozen=True)

    preferred_categories: tuple[str, ...] = ()

    @field_validator("preferred_categories", mode="before")
    @classmethod
    def _normalize_categories(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("preferred_categories must be a list or tuple of strings")
        if len(value) > MAX_PREFERENCE_TOKENS:
            raise ValueError("preferred_categories may contain at most 20 values")
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise ValueError("preferred_categories values must be strings")
            if "\n" in item or "\r" in item:
                raise ValueError("preferred_categories values must be single-line")
            token = item.strip().casefold()
            if not 1 <= len(token) <= MAX_PREFERENCE_TOKEN_CHARS:
                raise ValueError("preferred_categories values must be 1 to 64 characters")
            if token not in seen:
                seen.add(token)
                normalized.append(token)
        if len(normalized) > MAX_PREFERENCE_TOKENS:
            raise ValueError("preferred_categories may contain at most 20 distinct values")
        return tuple(normalized)


class RankedEvent(BaseModel):
    """One validated candidate with an explainable deterministic score."""

    model_config = ConfigDict(frozen=True)

    event: Event
    score: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)

    @field_validator("reason")
    @classmethod
    def _reason_is_single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("reason must be single-line")
        return value
