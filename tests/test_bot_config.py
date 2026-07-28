from pathlib import Path

import pytest
from pydantic import ValidationError

from planazo.bot.config import (
    IntRangeConstraint,
    LocaleConstraint,
    RegistrationStep,
    TextConstraint,
    load_config,
    resolve,
)

_MINIMAL_YAML = """
default_locale: en
locales: [en, es]
messages:
  start: {en: "Hi {name}", es: "Hola {name}"}
  register_display_name: {en: "What name?", es: "¿Qué nombre?"}
registration:
  steps:
    - profile_field: display_name
      prompt: register_display_name
      validation: {kind: text, min_length: 1, max_length: 80}
""".strip()


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_parses_shipped_catalog() -> None:
    config = load_config(Path("data/bot.yaml"))

    assert "en" in config.locales
    assert "es" in config.locales
    for step in config.registration.steps:
        assert step.prompt in config.messages


def test_load_config_rejects_a_message_missing_a_locale(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "bot.yaml",
        """
default_locale: en
locales: [en, es]
messages:
  start: {en: "Hi {name}"}
""".strip(),
    )

    with pytest.raises(ValidationError, match="start"):
        load_config(yaml_path)


def test_load_config_accepts_a_sixth_registration_step(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "bot.yaml",
        """
default_locale: en
locales: [en, es]
messages:
  start: {en: "Hi {name}", es: "Hola {name}"}
  register_display_name: {en: "What name?", es: "¿Qué nombre?"}
  register_favourite_colour: {en: "Favourite colour?", es: "¿Color favorito?"}
registration:
  steps:
    - profile_field: display_name
      prompt: register_display_name
      validation: {kind: text, min_length: 1, max_length: 80}
    - profile_field: favourite_colour
      prompt: register_favourite_colour
      validation: {kind: text, min_length: 1, max_length: 40}
""".strip(),
    )

    config = load_config(yaml_path)

    assert config.registration.steps[-1].profile_field == "favourite_colour"


def test_load_config_rejects_a_step_naming_an_unknown_prompt(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "bot.yaml",
        """
default_locale: en
locales: [en, es]
messages:
  start: {en: "Hi {name}", es: "Hola {name}"}
registration:
  steps:
    - profile_field: display_name
      prompt: register_missing
      validation: {kind: text, min_length: 1, max_length: 80}
""".strip(),
    )

    with pytest.raises(ValidationError):
        load_config(yaml_path)


def test_load_config_rejects_a_single_locale(tmp_path: Path) -> None:
    yaml_path = _write(
        tmp_path / "bot.yaml",
        """
default_locale: en
locales: [en]
messages:
  start: {en: "Hi {name}"}
""".strip(),
    )

    with pytest.raises(ValidationError):
        load_config(yaml_path)


def test_resolve_falls_back_to_the_default_locale_for_an_unknown_locale(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path / "bot.yaml", _MINIMAL_YAML))

    text = resolve(config, "start", "fr", name="X")

    assert text == "Hi X"


def test_step_validation_round_trips_a_text_constraint() -> None:
    step = RegistrationStep.model_validate(
        {
            "profile_field": "display_name",
            "prompt": "register_display_name",
            "validation": {"kind": "text", "min_length": 1, "max_length": 80},
        }
    )

    assert isinstance(step.validation, TextConstraint)
    assert step.validation.min_length == 1
    assert step.validation.max_length == 80


def test_step_validation_round_trips_an_int_range_constraint() -> None:
    step = RegistrationStep.model_validate(
        {
            "profile_field": "age",
            "prompt": "register_age",
            "validation": {"kind": "int_range", "minimum": 13, "maximum": 120},
        }
    )

    assert isinstance(step.validation, IntRangeConstraint)
    assert step.validation.minimum == 13
    assert step.validation.maximum == 120


def test_step_validation_round_trips_a_locale_constraint() -> None:
    step = RegistrationStep.model_validate(
        {
            "profile_field": "language",
            "prompt": "register_language",
            "validation": {"kind": "locale"},
        }
    )

    assert isinstance(step.validation, LocaleConstraint)
