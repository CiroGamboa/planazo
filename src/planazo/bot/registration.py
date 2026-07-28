"""Registration flow behaviour — `/register` and the plain-text continuation.

PTB-free, like every module in `bot/` except `app.py`/`surface.py` (ADR
0011): both coroutines here take the same `(UserSurface, sqlite3.Connection,
IncomingMessage, BotConfig) -> None` shape every command in `commands.py`
does, so this is CRUD-plus-validation against real SQLite that any surface
can drive.

`handle_register` starts or resumes the flow. `handle_registration_answer` is
the plain-text continuation #57 will route free text past; it is inert (no
reply, no write) whenever nothing is pending. Both read
`UserRecord.pending_registration_field` as the entire state machine — one
column names the next step, `NULL` means nothing is in flight
(`docs/adr/0013-registration-conversation-state.md`).

Every configured step's `profile_field` is checked against
`identity.models.ProfileField`'s known values the first time this module
reads `config.registration.steps`: `bot.config.RegistrationStep.profile_field`
stays a bare `str` by design, so an unrecognized field is caught here, as a
loud internal failure, never as a user-facing reply.
"""

from __future__ import annotations

import sqlite3
from typing import cast, get_args

from pydantic import BaseModel, ConfigDict, ValidationError, ValidationInfo, field_validator

from planazo.bot.config import (
    BotConfig,
    IntRangeConstraint,
    RegistrationStep,
    TextConstraint,
    resolve_for,
)
from planazo.bot.models import IncomingMessage
from planazo.bot.session import resolve_user, stored_id
from planazo.identity import (
    ProfileField,
    record_registration_answer,
    set_pending_registration_field,
)
from planazo.interfaces.surface import UserSurface

_KNOWN_PROFILE_FIELDS = frozenset(get_args(ProfileField))


class _AnswerRejectedError(Exception):
    """One answer failed its step's configured validation.

    Carries the `data/bot.yaml` message id and formatting kwargs for the
    reply the caller sends, rather than the raw `pydantic.ValidationError` —
    which names the layer that raised, not something the user typed wrong.
    """

    def __init__(self, message_id: str, **kwargs: object) -> None:
        super().__init__(message_id)
        self.message_id = message_id
        self.kwargs = kwargs


class _TextAnswer(BaseModel):
    """A `text`-constrained answer, bounded by the answered step's own limits.

    The bounds travel through `model_validate`'s `context` rather than
    `Field`, because they come from the answered step's `TextConstraint`
    (loaded from `data/bot.yaml`), not from this class's own definition.
    """

    model_config = ConfigDict(extra="forbid")

    value: str

    @field_validator("value")
    @classmethod
    def _within_configured_length(cls, value: str, info: ValidationInfo) -> str:
        bounds = info.context or {}
        stripped = value.strip()
        if not (bounds["min_length"] <= len(stripped) <= bounds["max_length"]):
            raise ValueError(
                f"must be between {bounds['min_length']} and {bounds['max_length']} characters"
            )
        return stripped


class _IntRangeAnswer(BaseModel):
    """An `int_range`-constrained answer, bounded by the answered step's own range."""

    model_config = ConfigDict(extra="forbid")

    value: int

    @field_validator("value")
    @classmethod
    def _within_configured_range(cls, value: int, info: ValidationInfo) -> int:
        bounds = info.context or {}
        if not (bounds["minimum"] <= value <= bounds["maximum"]):
            raise ValueError(f"must be between {bounds['minimum']} and {bounds['maximum']}")
        return value


class _LocaleAnswer(BaseModel):
    """A `locale`-constrained answer, checked against the running `BotConfig.locales`."""

    model_config = ConfigDict(extra="forbid")

    value: str

    @field_validator("value")
    @classmethod
    def _is_a_configured_locale(cls, value: str, info: ValidationInfo) -> str:
        locales = (info.context or {}).get("locales", ())
        stripped = value.strip()
        if stripped not in locales:
            raise ValueError(f"must be one of {', '.join(locales)}")
        return stripped


def _validated_steps(config: BotConfig) -> list[RegistrationStep]:
    """`config.registration.steps`, with every `profile_field` checked once.

    `RegistrationStep.profile_field` stays a bare `str` in `bot/config.py` by
    design (ADR 0013), so the loader keeps accepting a step naming a field
    nothing downstream maps yet. This is where that gets cross-checked: a
    step naming anything other than a known `ProfileField` is a config bug,
    not a user-facing outcome, so it raises rather than replying.
    """
    steps = config.registration.steps
    for step in steps:
        if step.profile_field not in _KNOWN_PROFILE_FIELDS:
            raise ValueError(
                f"registration step names profile_field {step.profile_field!r}, which is "
                f"not one of {sorted(_KNOWN_PROFILE_FIELDS)}"
            )
    return steps


def _field_of(step: RegistrationStep) -> ProfileField:
    """`step.profile_field`, narrowed for a step that has passed `_validated_steps`."""
    return cast(ProfileField, step.profile_field)


def _step_for(steps: list[RegistrationStep], field: ProfileField) -> RegistrationStep:
    for step in steps:
        if step.profile_field == field:
            return step
    raise RuntimeError(f"no configured registration step for pending field {field!r}")


def _next_field(steps: list[RegistrationStep], field: ProfileField) -> ProfileField | None:
    index = next(i for i, step in enumerate(steps) if step.profile_field == field)
    return _field_of(steps[index + 1]) if index + 1 < len(steps) else None


def _validate_answer(config: BotConfig, step: RegistrationStep, text: str) -> str | int:
    """The step's validated answer, or a raised `_AnswerRejectedError`.

    Every constraint kind validates through a Pydantic v2 model at the
    boundary (AGENTS.md rule 1): a bad answer never advances the pointer or
    reaches `record_registration_answer`.
    """
    constraint = step.validation
    if isinstance(constraint, TextConstraint):
        try:
            return _TextAnswer.model_validate(
                {"value": text},
                context={
                    "min_length": constraint.min_length,
                    "max_length": constraint.max_length,
                },
            ).value
        except ValidationError as error:
            raise _AnswerRejectedError(
                "register_invalid_text",
                min_length=constraint.min_length,
                max_length=constraint.max_length,
            ) from error
    if isinstance(constraint, IntRangeConstraint):
        try:
            return _IntRangeAnswer.model_validate(
                {"value": text},
                context={"minimum": constraint.minimum, "maximum": constraint.maximum},
            ).value
        except ValidationError as error:
            raise _AnswerRejectedError(
                "register_invalid_int_range",
                minimum=constraint.minimum,
                maximum=constraint.maximum,
            ) from error
    try:
        return _LocaleAnswer.model_validate(
            {"value": text}, context={"locales": config.locales}
        ).value
    except ValidationError as error:
        raise _AnswerRejectedError(
            "register_invalid_locale", locales=", ".join(config.locales)
        ) from error


async def handle_register(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Start the guided registration flow, or resume it unchanged if one is pending.

    A fresh sender (`pending_registration_field is None`) is pointed at the
    first configured step. A sender mid-flow — from either a fresh
    `/register` moments ago or an abandoned flow resumed by this very
    message — gets that same pending step's prompt re-sent: no reset, no
    data loss (ADR 0013).
    """
    steps = _validated_steps(config)
    user = resolve_user(conn, message)
    field = user.pending_registration_field
    if field is None:
        field = _field_of(steps[0])
        user = set_pending_registration_field(conn, stored_id(user), field)
    step = _step_for(steps, field)
    await surface.reply(resolve_for(config, step.prompt, user))


async def handle_registration_answer(
    surface: UserSurface, conn: sqlite3.Connection, message: IncomingMessage, config: BotConfig
) -> None:
    """Validate one answer against the pending step, then advance or re-prompt.

    Inert when nothing is pending: no reply, no write — the shape #57 layers
    its own free-text routing onto without touching this mechanism. On a
    valid answer, the field and the pointer's next value are written in one
    call (`record_registration_answer`), so there is no state between "the
    answer landed" and "the pointer advanced" for a crash to land inside.
    """
    user = resolve_user(conn, message)
    field = user.pending_registration_field
    if field is None:
        return

    steps = _validated_steps(config)
    step = _step_for(steps, field)
    try:
        value = _validate_answer(config, step, message.text)
    except _AnswerRejectedError as rejected:
        await surface.reply(resolve_for(config, rejected.message_id, user, **rejected.kwargs))
        return

    next_field = _next_field(steps, field)
    updated = record_registration_answer(conn, stored_id(user), field, value, next_field)
    if next_field is None:
        await surface.reply(resolve_for(config, "register_complete", updated))
        return
    await surface.reply(resolve_for(config, _step_for(steps, next_field).prompt, updated))
