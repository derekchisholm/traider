"""What the model calls cost, and the budgets that stop them.

Cost = sum over calls of ``tokens_in x in_price + tokens_out x out_price``, per model, at
the prices in the settings (dollars per million tokens), as a Decimal. Before each call
the caller reserves its worst case (the input estimate plus ``max_tokens`` of output); a
call that might break the run budget or what is left of the day budget is not made.
Reservations make that hold with several calls in flight.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal

from traider.research.job_settings import ModelPrice
from traider.research.llm import Usage

MTOK = Decimal(1_000_000)
CENT_HUNDREDTHS = Decimal("0.0001")


def _check_usage(usage: Usage) -> None:
    if usage.input_tokens < 0 or usage.output_tokens < 0:
        raise ValueError("token counts cannot be negative")


class CostMeter:
    def __init__(
        self,
        prices: Mapping[str, ModelPrice],
        *,
        run_usd: Decimal,
        day_remaining_usd: Decimal,
    ) -> None:
        self._prices = dict(prices)
        self.run_usd = run_usd
        self.day_remaining_usd = day_remaining_usd
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self.tokens_in = 0
        self.tokens_out = 0
        self.models: set[str] = set()
        # Set once a call was refused for budget: the run did not do all it planned.
        self.exhausted = False
        # Set when a call cost more than was reserved for it (the input estimate was low).
        self.overrun = False

    @property
    def limit(self) -> Decimal:
        return min(self.run_usd, self.day_remaining_usd)

    @property
    def spent_usd(self) -> Decimal:
        return self.spent.quantize(CENT_HUNDREDTHS, rounding=ROUND_HALF_UP)

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        price = self._prices[model]
        return (
            Decimal(input_tokens) * price.in_per_mtok + Decimal(output_tokens) * price.out_per_mtok
        ) / MTOK

    def would_exceed(self, model: str, input_tokens: int, max_tokens: int) -> bool:
        if model not in self._prices:
            return True  # an unpriced call cannot be bounded
        worst = self.cost(model, input_tokens, max_tokens)
        return self.spent + self.reserved + worst > self.limit

    def reserve(self, model: str, input_tokens: int, max_tokens: int) -> Decimal | None:
        """Hold the worst case for one call, or None (and ``exhausted``) if it may not run.
        Once ``exhausted`` is set (budget refusal, overrun, or spend past the limit) nothing
        more is reserved.

        ``input_tokens`` must be an upper bound on the request's real input size: the
        reservation is only as safe as that estimate."""
        if input_tokens < 0 or max_tokens < 0:
            raise ValueError("token counts cannot be negative")
        if self.exhausted or self.would_exceed(model, input_tokens, max_tokens):
            self.exhausted = True
            return None
        amount = self.cost(model, input_tokens, max_tokens)
        self.reserved += amount
        return amount

    def settle(self, model: str, reserved: Decimal, usage: Usage | None) -> None:
        """Replace a reservation with what the call cost. Without usage (the call failed and
        may still have been billed) the reservation is kept as spent. A call that cost more
        than its reservation sets ``overrun`` and ``exhausted``: nothing more is spent."""
        if reserved > self.reserved:
            raise ValueError("settling more than is reserved")
        if usage is not None:
            _check_usage(usage)
        self.reserved -= reserved
        if usage is None:
            self.spent += reserved
            self.models.add(model)
            self._check_limit()
            return
        if self.cost(model, usage.input_tokens, usage.output_tokens) > reserved:
            self.overrun = True
            self.exhausted = True
        self.record(model, usage)

    def record(self, model: str, usage: Usage) -> None:
        _check_usage(usage)
        self.spent += self.cost(model, usage.input_tokens, usage.output_tokens)
        self.tokens_in += usage.input_tokens
        self.tokens_out += usage.output_tokens
        self.models.add(model)
        self._check_limit()

    def _check_limit(self) -> None:
        if self.spent > self.limit:
            self.exhausted = True
