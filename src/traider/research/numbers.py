"""Small numeric helpers shared by the research jobs."""

from __future__ import annotations

import math


def round_half_up(value: float) -> int:
    # Round to 9 places first: float error must not turn an exact .5 into .4999...
    return math.floor(round(value, 9) + 0.5)
