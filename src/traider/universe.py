"""The universe: the equity symbols the bot watches right now.

Held, owned or busy symbols are always in it (their exits must keep working), then the
pinned symbols, then the best live research picks up to the cap. Options are tracked
through their underlying, which is why everything here works on root symbols.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from traider.options import is_option_symbol, parse_option_symbol


def root_symbol(symbol: str) -> str:
    return parse_option_symbol(symbol).underlying if is_option_symbol(symbol) else symbol


def compute_universe(
    *,
    required: Iterable[str],
    pinned: Sequence[str],
    picks: Sequence[tuple[str, int]],
    cap: int,
) -> tuple[str, ...]:
    out: list[str] = []
    for symbol in [*sorted(set(required)), *pinned]:
        if symbol not in out:
            out.append(symbol)
    for symbol, _ in sorted(picks, key=lambda p: (-p[1], p[0])):
        if len(out) >= cap:
            break
        if symbol not in out:
            out.append(symbol)
    return tuple(out)
