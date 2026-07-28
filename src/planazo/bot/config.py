"""Pydantic-validated loader for `data/bot.yaml`.

`BotConfig` is the root model — a locale-keyed message catalog plus the
ordered registration-step declarations #56 will execute. It mirrors
`planazo.sources.config.load_config` in shape: `load_config()` reads the YAML
file and `model_validate()`s the whole tree in one call, raising
`ValidationError` uncaught. A malformed or incomplete `data/bot.yaml` is
therefore a boot-time failure — before the bot ever opens a Telegram
connection — never a surprise on the first reply (AGENTS.md rule 1, rule 4).

`messages` maps a message id to a locale-keyed dict of text; load-time
validation requires every message's locale-key set to equal `locales`
exactly, so `resolve()` can always fall back to `default_locale` without a
missing-key check of its own. `registration.steps` declares the ordered
profile fields a new user is asked for, each step's `prompt` cross-checked
against `messages` at load time and its `validation` a discriminated union
over `kind` (`text` / `int_range` / `locale`) — #56 consumes the steps to
drive the actual registration flow; this module only declares and validates
their shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class TextConstraint(BaseModel):
    """A registration step answered with free text, bounded by length."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["text"] = "text"
    min_length: int = Field(default=1, ge=0)
    max_length: int = Field(gt=0)


class IntRangeConstraint(BaseModel):
    """A registration step answered with an integer within `[minimum, maximum]`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["int_range"] = "int_range"
    minimum: int
    maximum: int

    @model_validator(mode="after")
    def _minimum_is_at_most_maximum(self) -> IntRangeConstraint:
        if self.minimum > self.maximum:
            raise ValueError(f"minimum {self.minimum!r} must be <= maximum {self.maximum!r}")
        return self


class LocaleConstraint(BaseModel):
    """A registration step answered with one of `BotConfig.locales`.

    The membership check itself is whatever later consumes this constraint
    (#56) — this ticket only declares the constraint kind, tying the
    "language" step's accepted values to `BotConfig.locales` by construction
    rather than a separately duplicated list.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["locale"] = "locale"


StepValidation = Annotated[
    TextConstraint | IntRangeConstraint | LocaleConstraint, Field(discriminator="kind")
]


class RegistrationStep(BaseModel):
    """One ordered question in the registration flow."""

    model_config = ConfigDict(extra="forbid")

    profile_field: str = Field(min_length=1)
    """The identity field this step fills. Not cross-checked against any
    persisted schema, since #56 (not this ticket) decides where it lands."""

    prompt: str = Field(min_length=1)
    """A message id — must be a key of `BotConfig.messages`."""

    validation: StepValidation


class RegistrationConfig(BaseModel):
    """The ordered registration steps a new user is asked to complete."""

    model_config = ConfigDict(extra="forbid")

    steps: list[RegistrationStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_profile_fields(self) -> RegistrationConfig:
        seen: set[str] = set()
        for step in self.steps:
            if step.profile_field in seen:
                raise ValueError(
                    f"duplicate profile_field {step.profile_field!r} in registration steps"
                )
            seen.add(step.profile_field)
        return self


class BotConfig(BaseModel):
    """Root of `data/bot.yaml` — the message catalog and registration steps."""

    model_config = ConfigDict(extra="forbid")

    default_locale: str = Field(min_length=1)
    locales: list[str] = Field(min_length=2)
    messages: dict[str, dict[str, str]]
    registration: RegistrationConfig = Field(default_factory=RegistrationConfig)

    @model_validator(mode="after")
    def _default_locale_is_declared(self) -> BotConfig:
        if self.default_locale not in self.locales:
            raise ValueError(
                f"default_locale {self.default_locale!r} is not one of locales {self.locales!r}"
            )
        return self

    @model_validator(mode="after")
    def _messages_cover_every_locale_exactly(self) -> BotConfig:
        declared = set(self.locales)
        for message_id, translations in self.messages.items():
            covered = set(translations)
            if covered != declared:
                missing = sorted(declared - covered)
                stray = sorted(covered - declared)
                raise ValueError(
                    f"message {message_id!r} covers locales {sorted(covered)}, "
                    f"which does not match locales {sorted(declared)} "
                    f"(missing={missing}, stray={stray})"
                )
        return self

    @model_validator(mode="after")
    def _registration_prompts_exist_in_messages(self) -> BotConfig:
        for step in self.registration.steps:
            if step.prompt not in self.messages:
                raise ValueError(
                    f"registration step {step.profile_field!r} references prompt "
                    f"{step.prompt!r}, which is not a key of messages"
                )
        return self


def load_config(path: Path = Path("data/bot.yaml")) -> BotConfig:
    """Read + validate the bot config; raise `ValidationError` on any issue.

    Fails before the bot ever opens a Telegram connection rather than on the
    first reply: a message id missing a locale, a registration step naming an
    unknown prompt, or fewer than two locales all raise before any token
    check or `run_polling()` call.
    """
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if raw is None:
        raw = {}
    return BotConfig.model_validate(raw)


def resolve(config: BotConfig, message_id: str, locale: str, **kwargs: object) -> str:
    """The text for `message_id` in `locale`, formatted with `kwargs`.

    Falls back to `config.default_locale` when `locale` is absent from the
    catalog — guaranteed present there by the load-time validation above —
    so an unrecognized locale never raises. An unknown `message_id` is a
    `KeyError`, deliberately unguarded: message ids are code-controlled
    constants, not user input, so a typo is a programmer error and should
    fail loud (AGENTS.md rule 1 is about external input; this is internal
    call-site hygiene, the same posture as `commands._stored_id`'s
    `RuntimeError`).
    """
    translations = config.messages[message_id]
    text = translations.get(locale, translations[config.default_locale])
    return text.format(**kwargs)
