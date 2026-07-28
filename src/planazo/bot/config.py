"""Pydantic-validated loader for `data/bot.yaml`.

`BotConfig` is the root model — a locale-keyed message catalog plus the
ordered registration-step declarations `bot/registration.py` executes. It
mirrors `planazo.sources.config.load_config` in shape: `load_config()` reads
the YAML file and `model_validate()`s the whole tree in one call, raising
`ValidationError` uncaught. A malformed or incomplete `data/bot.yaml` is
therefore a boot-time failure — before the bot ever opens a Telegram
connection — never a surprise on the first reply (AGENTS.md rule 1, rule 4).

`messages` maps a message id to a locale-keyed dict of text; load-time
validation requires every message's locale-key set to equal `locales`
exactly, so `resolve()` can always fall back to `default_locale` without a
missing-key check of its own. `registration.steps` declares the ordered
profile fields a new user is asked for, each step's `prompt` cross-checked
against `messages` at load time and its `validation` a discriminated union
over `kind` (`text` / `int_range` / `locale`) — `bot/registration.py`
consumes the steps to drive the actual registration flow; this module only
declares and validates their shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from planazo.identity import UserRecord


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

    The membership check itself lives in `bot/registration.py`, which
    consumes this constraint against the running `BotConfig.locales` — this
    module only declares the constraint kind, tying the "language" step's
    accepted values to `BotConfig.locales` by construction rather than a
    separately duplicated list.
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
    persisted schema here: a step naming a field `identity.models.ProfileField`
    does not know is caught in `bot/registration.py`, where the steps are
    consumed, not at load time (`docs/adr/0013-registration-conversation-state.md`).
    This is a deliberate, permanent property of the loader — not a placeholder —
    so a step naming a field nothing downstream maps yet still loads."""

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


class QueueConfig(BaseModel):
    """The per-sender FIFO gate's backlog bound and its two dispatch replies."""

    model_config = ConfigDict(extra="forbid")

    bound: int = Field(gt=0)
    """The number of *waiting* messages admitted per sender, not counting the
    one already running: a bound of 20 admits 1 in-flight message plus up to
    20 queued behind it before the 22nd arrival for that sender overflows."""

    ack_message: str = Field(min_length=1)
    """A key of `BotConfig.messages` — the immediate reply sent when a message
    is queued behind another still in flight for the same sender."""

    overflow_message: str = Field(min_length=1)
    """A key of `BotConfig.messages` — the reply sent, instead of queuing,
    once the sender's backlog has already reached `bound`."""


class BotConfig(BaseModel):
    """Root of `data/bot.yaml` — the message catalog and registration steps."""

    model_config = ConfigDict(extra="forbid")

    default_locale: str = Field(min_length=1)
    locales: list[str] = Field(min_length=2)
    messages: dict[str, dict[str, str]]
    registration: RegistrationConfig = Field(default_factory=RegistrationConfig)
    queue: QueueConfig

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

    @model_validator(mode="after")
    def _queue_messages_exist_in_messages(self) -> BotConfig:
        for field_name in ("ack_message", "overflow_message"):
            message_id = getattr(self.queue, field_name)
            if message_id not in self.messages:
                raise ValueError(
                    f"queue.{field_name} references message {message_id!r}, "
                    f"which is not a key of messages"
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
    call-site hygiene, the same posture as `bot.session.stored_id`'s
    `RuntimeError`).
    """
    translations = config.messages[message_id]
    text = translations.get(locale, translations[config.default_locale])
    return text.format(**kwargs)


def resolve_for(config: BotConfig, message_id: str, user: UserRecord, **kwargs: object) -> str:
    """`resolve()` at `user`'s stored locale, falling back to `config.default_locale`.

    `UserRecord.language` is `None` until the registration flow's language
    step is answered, so a reply to a sender who has not reached it yet
    resolves exactly as `resolve(config, message_id, config.default_locale,
    **kwargs)` would — this is what lets every prompt and failure reply in
    `bot/registration.py` (and, later, the rest of `bot/`) resolve per-sender
    instead of at `config.default_locale` unconditionally.
    """
    return resolve(config, message_id, user.language or config.default_locale, **kwargs)
