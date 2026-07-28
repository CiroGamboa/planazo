"""The no-LLM-in-`bot/` invariant (ADR 0011), as a source-text scan.

The bot layer is CRUD against SQLite and nothing else, so no LLM call
originates in it. The guard is deliberately shallow — it reads the text of
every module under `src/planazo/bot/` and fails if one names `agentlib` —
rather than walking the import graph the way `tests/test_trust_boundary.py`
does. The invariant is about where a call *originates*: routing free text to
`planazo.agents.event_agent`, whose graph legitimately reaches `agentlib`, is
the intended evolution of this package rather than a violation of it, and a
transitive walk could not tell the two apart.
"""

from __future__ import annotations

from pathlib import Path

_BOT_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "planazo" / "bot"
_FORBIDDEN = "agentlib"


def _bot_modules() -> list[Path]:
    return sorted(_BOT_PACKAGE.rglob("*.py"))


def test_the_scan_has_modules_to_read() -> None:
    """Without this, renaming the package would make the invariant pass vacuously."""
    assert _bot_modules(), f"no modules found under {_BOT_PACKAGE}"


def test_no_module_under_bot_names_agentlib() -> None:
    offenders = [
        path.name for path in _bot_modules() if _FORBIDDEN in path.read_text(encoding="utf-8")
    ]

    assert not offenders, (
        f"{offenders} name {_FORBIDDEN!r} — no LLM call may originate in "
        "src/planazo/bot/ (ADR 0011)."
    )
