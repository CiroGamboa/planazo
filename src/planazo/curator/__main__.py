"""`python -m planazo.curator` — start one curator tick.

The exit status is `main()`'s: 0 on completion, 1 on uncaught exception,
2 on future configuration failures. Matches `planazo-scheduler`'s
one-line-per-tick shape.
"""

from __future__ import annotations

import sys

from planazo.curator.cli import main

sys.exit(main())
