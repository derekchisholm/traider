"""Regular-session hours from Schwab's market calendar (it knows holidays and half days)."""

from __future__ import annotations

from datetime import date

from traider.schwab.client import SchwabClient
from traider.schwab.parse import parse_market_hours
from traider.session import Session


class SchwabSessionProvider:
    def __init__(self, client: SchwabClient) -> None:
        self._client = client

    async def session_for(self, day: date) -> Session | None:
        """Raises if Schwab cannot be asked or the answer is not understood. The caller
        (SessionTracker) then treats the market as closed and asks again later."""
        return parse_market_hours(await self._client.market_hours(day), day)
