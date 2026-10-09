"""Liveness check for the container.

The engine rewrites a heartbeat file on every pass of its loop. ECS runs
``python -m traider.health`` as the container health check; if the file goes
stale the loop is stuck and ECS replaces the task. The check deliberately says
nothing about Schwab or the market: being signed out is not a reason to restart.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DEFAULT_FILE = "/tmp/traider-heartbeat"  # noqa: S108 - the container's own scratch space
MAX_AGE_S = 60.0


def is_alive(path: str | os.PathLike[str], *, max_age_s: float = MAX_AGE_S) -> bool:
    try:
        age = time.time() - Path(path).stat().st_mtime
    except OSError:
        return False
    return age <= max_age_s


def main() -> int:
    path = os.environ.get("TRAIDER_HEARTBEAT_FILE") or DEFAULT_FILE
    return 0 if is_alive(path) else 1


if __name__ == "__main__":
    sys.exit(main())
