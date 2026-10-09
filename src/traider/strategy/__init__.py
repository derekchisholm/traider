"""Strategy registry. Add a strategy by importing it here and listing it in ``_REGISTRY``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from traider.strategy.base import Strategy
from traider.strategy.sma_cross import SmaCross

_REGISTRY: dict[str, type[Strategy]] = {SmaCross.name: SmaCross}


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


def create_strategy(name: str, symbols: Sequence[str], params: Mapping[str, Any]) -> Strategy:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown strategy {name!r}; available: {', '.join(available_strategies())}"
        ) from None
    return cls(symbols, params)
