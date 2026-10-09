"""Order payloads for Schwab's ``POST /accounts/{hash}/orders``.

Only what the bot uses: single-leg equity orders, regular session, good for the
day. The shape matches schwab-py's equity order builders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from traider.models import OrderRequest, OrderType

_CENT = Decimal("0.01")


def build_equity_order(request: OrderRequest) -> dict[str, Any]:
    if request.quantity <= 0:
        raise ValueError("order quantity must be positive")
    payload: dict[str, Any] = {
        "orderType": request.order_type.value,
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": request.side.value,
                "quantity": request.quantity,
                "instrument": {"symbol": request.symbol, "assetType": "EQUITY"},
            }
        ],
    }
    if request.order_type is OrderType.LIMIT:
        price = request.limit_price
        if price is None or price <= 0:
            raise ValueError("limit order needs a positive price")
        if price != price.quantize(_CENT):
            raise ValueError(f"limit price {price} is not a whole number of cents")
        payload["price"] = f"{price:.2f}"  # Schwab takes prices as strings
    return payload
