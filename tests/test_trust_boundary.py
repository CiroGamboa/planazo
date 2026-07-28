"""Static trust-boundary guard for the Recommender's import graph.

ADR 0005 §Trust boundary commits that `agents/event_agent.py` never
statically reaches `planazo.sources.instagram.*` — because reaching it
would drag Instaloader into the Recommender's runtime and, more
importantly, would make caption text one `from` line away from the
system-message-assembling code. The composition-root wiring for
`dispatch_extraction` deliberately uses a lazy import inside `run_once`,
and this test enforces that discipline by walking the module-level import
graph and asserting the transitive `planazo.*` set never touches either
`planazo.sources.instagram` or `planazo.agents.extractor` (the tighter
Recommender-side guarantee — the Extractor's composition root is not a
static dependency of the Recommender either).

Deterministic and no code execution: this is `ast.walk` filtered to
module-scope nodes, skipping bodies of `FunctionDef` / `AsyncFunctionDef`
/ `ClassDef` and any `if TYPE_CHECKING:` block. Function-body imports are
intentionally invisible to the walker — that IS the seam the composition
root uses.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_ROOT_MODULE = "planazo.agents.event_agent"
_FORBIDDEN_PREFIX = "planazo.sources.instagram"
_FORBIDDEN_EXACT = "planazo.agents.extractor"


def _resolve_module_path(module_name: str) -> Path | None:
    """Return the `.py` file backing `module_name`, or `None` if unresolvable.

    Package `__init__.py` files are what `importlib.util.find_spec` returns
    for a bare package name, which is exactly what we want to walk — a
    package's own top-level imports are what a caller of `from pkg import x`
    ends up depending on.
    """
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    if spec.origin == "built-in":
        return None
    return Path(spec.origin)


def _is_type_checking_block(node: ast.AST) -> bool:
    """True for `if TYPE_CHECKING:` — a block a static import walker skips."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _module_level_imports(path: Path) -> set[str]:
    """Collect every `planazo.*` module name reachable via *module-scope* imports.

    Skips nodes nested under function/class bodies (those are the lazy-import
    seams the trust boundary tolerates) and inside `if TYPE_CHECKING:` blocks
    (those never execute at runtime, so they cannot leak Instaloader in).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                # Bodies of functions/classes are runtime-scoped, not import-time.
                continue
            if _is_type_checking_block(child):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    if alias.name.startswith("planazo."):
                        names.add(alias.name)
            elif isinstance(child, ast.ImportFrom) and child.module is not None:
                module = child.module
                if not module.startswith("planazo"):
                    continue
                names.add(module)
                for alias in child.names:
                    # `from planazo.catalog import search_events` — the imported
                    # symbol might itself be a submodule; recording the parent
                    # is what actually matters for the transitive walk, so no
                    # extra work is needed for aliases.
                    _ = alias
            visit(child)

    visit(tree)
    return names


def _transitive_planazo_imports(root_module: str) -> set[str]:
    """BFS the module-level `planazo.*` import graph starting at `root_module`."""
    reachable: set[str] = set()
    queue = [root_module]
    while queue:
        module = queue.pop()
        if module in reachable:
            continue
        reachable.add(module)
        path = _resolve_module_path(module)
        if path is None:
            continue
        for imported in _module_level_imports(path):
            if imported.startswith("planazo") and imported not in reachable:
                queue.append(imported)
    return reachable


def test_event_agent_static_graph_never_reaches_sources_instagram() -> None:
    reachable = _transitive_planazo_imports(_ROOT_MODULE)

    leaks = {name for name in reachable if name.startswith(_FORBIDDEN_PREFIX)}
    assert not leaks, (
        "Recommender's static import graph reaches "
        f"{_FORBIDDEN_PREFIX} via: {sorted(leaks)!r} — the Extractor "
        "composition root must be reached through a function-body lazy "
        "import (ADR 0005 §Trust boundary)."
    )


def test_event_agent_static_graph_never_reaches_extractor_composition_root() -> None:
    """The tighter guarantee: even `planazo.agents.extractor` (which
    top-imports `planazo.sources.instagram.*`) must not appear as a static
    dependency of the Recommender."""
    reachable = _transitive_planazo_imports(_ROOT_MODULE)

    assert _FORBIDDEN_EXACT not in reachable, (
        f"Recommender's static import graph reaches {_FORBIDDEN_EXACT} — "
        "the Extractor composition root must be reached only through a "
        "function-body lazy import (ADR 0005 §Trust boundary)."
    )
