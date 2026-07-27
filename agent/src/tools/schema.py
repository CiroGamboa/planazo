"""Auto-derive a JSON tool schema from a function's signature and docstring.

Mirrors Session 3's "rung 3" pattern (derive name/description/parameters from
the function itself, not a hand-written dict) with one extension: a
`Literal[...]` annotation is translated into a JSON-schema `enum`, so a
constrained parameter comes from the signature itself rather than a manual
post-hoc patch.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Literal, get_args, get_origin, get_type_hints

_PYTYPE: dict[type, str] = {int: "integer", float: "number", str: "string", bool: "boolean"}


def schema_for(
    fn: Callable[..., object],
) -> dict[str, Any]:  # Any: JSON Schema mixes str/bool/list/dict
    """Derive a flat function-tool schema from `fn`'s signature and docstring.

    `fn.__doc__` becomes `description`. Each parameter's type annotation
    becomes its JSON-schema `type` (falling back to `"string"` for anything
    not in `_PYTYPE`). A `Literal[...]` annotation becomes an `enum` on top
    of the base type instead of a plain type. Parameters without a default
    are `required`.
    """
    signature = inspect.signature(fn)
    # `get_type_hints` (not `param.annotation` directly) so this still resolves
    # real Literal/int/str objects even when `fn`'s module uses
    # `from __future__ import annotations` and annotations are stringized.
    hints = get_type_hints(fn)
    properties: dict[str, dict[str, Any]] = {}  # Any: see schema_for's return type
    required: list[str] = []

    for name, param in signature.parameters.items():
        properties[name] = _property_for(hints.get(name, param.annotation))
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "name": fn.__name__,
        "description": (fn.__doc__ or "").strip(),
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _property_for(
    annotation: object,
) -> dict[str, Any]:  # Any: enum values vary by the Literal's members
    if get_origin(annotation) is Literal:
        choices = get_args(annotation)
        base_type = type(choices[0]) if choices else str
        return {"type": _PYTYPE.get(base_type, "string"), "enum": list(choices)}
    if isinstance(annotation, type):
        return {"type": _PYTYPE.get(annotation, "string")}
    return {"type": "string"}
