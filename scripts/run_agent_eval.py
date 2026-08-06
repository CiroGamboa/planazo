"""One-line wrapper for the ``planazo-agent-eval`` console script.

Mirrors ``scripts/run_retrieval_eval.py`` / ``scripts/run_generation_eval.py``
so operators reach for the same shape whether they are running retrieval,
generation, or agent eval. Delegates to the real entrypoint at
``planazo.eval.agent.cli:main``.

Per ADR 0027 (HW4 orchestration ADR).
"""

from __future__ import annotations

import sys

from planazo.eval.agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
