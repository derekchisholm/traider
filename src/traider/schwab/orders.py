"""Order payloads for Schwab's ``POST /accounts/{hash}/orders``.

Only what the bot uses: single-leg orders, regular session, good for the day.
Shares are bought and sold; options are bought to open and sold to close, never
the other way round. The shapes match schwab-py's builders and schwabdev's samples.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from traider.models import OrderRequest, OrderType, Side
from traider.options import is_option_symbol

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


def build_order(request: OrderRequest) -> dict[str, Any]:
    """The payload for a share order or a long-option order, by the symbol's form."""
    if not is_option_symbol(request.symbol):
        return build_equity_order(request)
    if request.order_type is not OrderType.LIMIT:
        raise ValueError("option orders must be limit orders")
    payload = build_equity_order(request)
    leg = payload["orderLegCollection"][0]
    leg["instruction"] = "BUY_TO_OPEN" if request.side is Side.BUY else "SELL_TO_CLOSE"
    leg["instrument"] = {"symbol": request.symbol, "assetType": "OPTION"}
    return payload
