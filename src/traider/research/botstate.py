"""What research may read of the bot's own state: its position ledger (``POS#<ns>``) and
its event log (``LOG#<ns>#<day>``). Nothing else, and never a write: the research task's
role may only Query those two kinds of partition in the state table.

``<ns>`` is the bot's trading mode (``paper`` or ``live``). The research task does not run
in that mode, so it is told the namespace explicitly (``TRAIDER_STATE_NAMESPACE``).
"""

from __future__ import annotations

from typing import Any, Protocol

from traider.state.base import LedgerEntry
from traider.universe import root_symbol


class BotState(Protocol):
    """The read-only part of ``StateStore`` research uses. ``DynamoStateStore`` and
    ``MemoryStateStore`` both satisfy it."""

    async def ledger(self) -> dict[str, LedgerEntry]: ...

    async def events(self, day: str) -> list[dict[str, Any]]: ...


async def held_symbols(state: BotState) -> set[str]:
    """The equity symbols the bot holds positions in, by its ledger. An option counts as
    its underlying."""
    return {root_symbol(symbol) for symbol in await state.ledger()}


class ReadOnlyState:
    """Only the two reads, whatever store is underneath: what research is handed has no
    method that writes."""

    __slots__ = ("_state",)

    def __init__(self, state: BotState) -> None:
        self._state = state

    async def ledger(self) -> dict[str, LedgerEntry]:
        return await self._state.ledger()

    async def events(self, day: str) -> list[dict[str, Any]]:
        return await self._state.events(day)
