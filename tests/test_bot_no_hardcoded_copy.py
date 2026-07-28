"""The no-hardcoded-copy invariant (#55), as a source-text/AST scan.

Every user-facing string the bot sends must live in `data/bot.yaml` and reach
the user through `planazo.bot.config.resolve`, mirroring
`tests/test_bot_no_llm.py`'s shape. The guard walks the AST of every `*.py`
module under `src/planazo/bot/` except `config.py` — the catalog loader, which
legitimately holds copy — and flags two shapes: a string or f-string literal
passed directly to a call whose method is named `reply`, and any string value
inside a module-level dict/list literal that contains a whitespace character
(a `COMMANDS`/old-`MESSAGES`-shaped dict of copy, even one that never reaches
`.reply` directly).

The one known, deliberate non-match: `commands._stored_id`'s `RuntimeError`
message. It documents an internal invariant violation that never reaches
`surface.reply` and is not a module-level dict/list literal, so it sits
outside this scan's scope by construction rather than being a missed case.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

_BOT_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "planazo" / "bot"
_EXEMPT_MODULES = {"config.py"}


def _scanned_modules() -> list[Path]:
    return sorted(path for path in _BOT_PACKAGE.rglob("*.py") if path.name not in _EXEMPT_MODULES)


def _string_value(node: ast.expr) -> str | None:
    """The literal string `node` evaluates to, or `None` if it is not one.

    An f-string with an interpolated `{expr}` piece is not a literal, so it
    returns `None` rather than the static parts alone — a partially dynamic
    string is not the shape this scan is built to catch.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # an f-string
        parts: list[str] = []
        for value in node.values:
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return None
            parts.append(value.value)
        return "".join(parts)
    return None


def _reply_literal_violations(tree: ast.Module) -> list[str]:
    """Every string/f-string literal passed straight to a `.reply(...)` call."""
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute) and node.func.attr == "reply"):
            continue
        for arg in node.args:
            text = _string_value(arg)
            if text is not None:
                violations.append(text)
    return violations


def _module_level_dict_or_list_literals(tree: ast.Module) -> list[ast.expr]:
    """The dict/list literals directly assigned at module scope."""
    literals: list[ast.expr] = []
    for node in tree.body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign | ast.AnnAssign):
            value = node.value
        if isinstance(value, ast.Dict | ast.List):
            literals.append(value)
    return literals


def _string_values_of(literal: ast.expr) -> list[str]:
    """Every string a dict/list literal holds, recursing into nested containers."""
    candidates: list[ast.expr]
    if isinstance(literal, ast.Dict):
        candidates = literal.values
    elif isinstance(literal, ast.List):
        candidates = literal.elts
    else:
        candidates = []

    values: list[str] = []
    for candidate in candidates:
        text = _string_value(candidate)
        if text is not None:
            values.append(text)
        elif isinstance(candidate, ast.Dict | ast.List):
            values.extend(_string_values_of(candidate))
    return values


def _has_whitespace(value: str) -> bool:
    return any(character.isspace() for character in value)


def _hardcoded_copy_violations(source: str) -> list[str]:
    """Every literal this scan flags in `source`."""
    tree = ast.parse(source)
    violations = list(_reply_literal_violations(tree))
    for literal in _module_level_dict_or_list_literals(tree):
        violations.extend(text for text in _string_values_of(literal) if _has_whitespace(text))
    return violations


def test_the_scan_has_modules_to_read() -> None:
    """Without this, renaming the package would make the invariant pass vacuously."""
    assert _scanned_modules(), f"no modules found under {_BOT_PACKAGE}"


def test_no_module_under_bot_hardcodes_user_facing_copy() -> None:
    offenders = {
        path.name: violations
        for path in _scanned_modules()
        if (violations := _hardcoded_copy_violations(path.read_text(encoding="utf-8")))
    }

    assert not offenders, (
        f"{offenders} hardcode copy outside data/bot.yaml — resolve it through "
        "planazo.bot.config.resolve instead (see this module's docstring for "
        "the one known, deliberate non-match)."
    )


def test_the_scan_actually_flags_a_hardcoded_reply() -> None:
    """The check itself is real: it must flag a fixture module built to trip it."""
    fixture = textwrap.dedent(
        """
        async def handle_example(surface):
            await surface.reply("hi there")
        """
    )

    assert _hardcoded_copy_violations(fixture) == ["hi there"]
