"""An in-process stand-in for the Schwab Trader API, for tests.

It speaks real HTTP and WebSocket on localhost, so the client code under test is
exercised end to end. Paths, payload shapes and status codes are modelled on the
two open-source Schwab libraries this project was cross-checked against
(schwab-py and schwabdev) and on the sample responses in their documentation.
It has never been compared with the live service: that is what
``traider check`` is for.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

APP_KEY = "test-app-key-0123456789abcdef"
APP_SECRET = "test-app-secret-0123456789abcdef"
ACCOUNT_NUMBER = "12345678"
ACCOUNT_HASH = "HASH0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789AB"
SECOND_ACCOUNT_NUMBER = "87654321"
SECOND_ACCOUNT_HASH = "HASH9999999999999999999999999999999999999999999999999999999999ZZ"

REFRESH_LIFETIME_S = 7 * 24 * 3600
_NEW_YORK = ZoneInfo("America/New_York")
ACCESS_LIFETIME_S = 1800


@dataclass
class FakeOrder:
    order_id: int
    account_hash: str
    body: dict[str, Any]
    status: str = "WORKING"
    filled: float = 0.0
    fills: list[tuple[float, float]] = field(default_factory=list)  # (quantity, price)
    entered: datetime = field(default_factory=lambda: datetime.now(UTC))
    closed: datetime | None = None

    @property
    def leg(self) -> dict[str, Any]:
        leg: dict[str, Any] = self.body["orderLegCollection"][0]
        return leg

    @property
    def symbol(self) -> str:
        return str(self.leg["instrument"]["symbol"])

    @property
    def quantity(self) -> float:
        return float(self.leg["quantity"])

    def to_json(self) -> dict[str, Any]:
        activities = [
            {
                "activityType": "EXECUTION",
                "activityId": 9000 + i,
                "executionType": "FILL",
                "quantity": quantity,
                "orderRemainingQuantity": 0.0,
                "executionLegs": [
                    {
                        "legId": 1,
                        "quantity": quantity,
                        "mismarkedQuantity": 0.0,
                        "price": price,
                        "time": _schwab_time(self.entered),
                        "instrumentId": 1,
                    }
                ],
            }
            for i, (quantity, price) in enumerate(self.fills)
        ]
        if self.status == "CANCELED":
            # Schwab reports the cancel as an activity with a zero price. A client that
            # averages every activity, not only fills, would get the fill price wrong.
            activities.append(
                {
                    "activityType": "EXECUTION",
                    "executionType": "CANCELED",
                    "quantity": self.quantity - self.filled,
                    "orderRemainingQuantity": 0.0,
                    "executionLegs": [
                        {
                            "legId": 1,
                            "quantity": self.quantity - self.filled,
                            "price": 0.0,
                            "time": _schwab_time(self.entered),
                        }
                    ],
                }
            )
        out: dict[str, Any] = {
            "session": self.body.get("session", "NORMAL"),
            "duration": self.body.get("duration", "DAY"),
            "orderType": self.body.get("orderType"),
            "complexOrderStrategyType": "NONE",
            "quantity": self.quantity,
            "filledQuantity": self.filled,
            "remainingQuantity": 0.0 if self.status != "WORKING" else self.quantity - self.filled,
            "requestedDestination": "AUTO",
            "destinationLinkName": "AutoRoute",
            "orderLegCollection": [
                {
                    "orderLegType": "EQUITY",
                    "legId": 1,
                    "instrument": {
                        "assetType": "EQUITY",
                        "cusip": "000000000",
                        "symbol": self.symbol,
                        "instrumentId": 1,
                    },
                    "instruction": self.leg["instruction"],
                    "positionEffect": "OPENING",
                    "quantity": self.quantity,
                }
            ],
            "orderStrategyType": "SINGLE",
            "orderId": self.order_id,
            "cancelable": self.status == "WORKING",
            "editable": False,
            "status": self.status,
            "enteredTime": _schwab_time(self.entered),
            "tag": "API_TEST",
            "accountNumber": int(ACCOUNT_NUMBER),
        }
        if "price" in self.body:
            out["price"] = float(self.body["price"])
        if self.closed is not None:
            out["closeTime"] = _schwab_time(self.closed)
        if activities:
            out["orderActivityCollection"] = activities
        return out


def _schwab_time(when: datetime) -> str:
    # Schwab writes offsets without a colon: 2024-03-29T14:30:00+0000
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeSchwab:
    def __init__(self) -> None:
        self.now: Callable[[], float] = time.time
        self.redirect_uri = "https://127.0.0.1"
        # OAuth
        self.auth_codes: dict[str, str] = {}  # code -> redirect_uri it was issued for
        self.refresh_tokens: dict[str, float] = {}  # token -> expiry (epoch)
        self.access_tokens: dict[str, float] = {}
        self.rotate_refresh_token = False
        self._counter = 0
        # Accounts
        self.accounts: dict[str, str] = {ACCOUNT_HASH: ACCOUNT_NUMBER}
        self.account_type: str | None = "MARGIN"
        self.cash = 10000.0
        self.positions: dict[str, tuple[float, float]] = {}  # symbol -> (quantity, avg price)
        self.omit_balance_fields: set[str] = set()
        self.chains: dict[str, list[dict[str, Any]]] = {}
        # Orders
        self.orders: dict[int, FakeOrder] = {}
        self.next_order_id = 1001
        self.fill_on_place = True
        self.omit_location_header = False
        self.reject_orders_with: str | None = None
        # Market data
        self.quotes: dict[str, dict[str, Any]] = {}
        self.candles: dict[str, list[dict[str, Any]]] = {}
        self.movers: dict[str, list[dict[str, Any]]] = {}  # index -> screeners
        self.market_open = True
        self.session_start = "09:30:00"
        self.session_end = "16:00:00"
        self.closed_day_inner_key = "equity"
        # Fault injection and observation
        self.requests: list[dict[str, Any]] = []
        self._faults: list[dict[str, Any]] = []
        # Streaming
        self.sockets: list[web.WebSocketResponse] = []  # every open connection
        self._logged_in: set[web.WebSocketResponse] = set()  # the ones market data goes to
        self.stream_logins = 0
        self.stream_requests: list[dict[str, Any]] = []
        self.stream_login_code = 0
        self.stream_subs_code = 0
        self.stream_silent_subs = False  # accept the login, then never answer a subscription
        self.stream_stray_replies = False  # precede each answer with one to some other request
        self.subscriptions: dict[str, set[str]] = {}

        self._server: TestServer | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_post("/v1/oauth/token", self._token)
        app.router.add_get("/trader/v1/accounts/accountNumbers", self._account_numbers)
        app.router.add_get("/trader/v1/accounts/{hash}", self._account)
        app.router.add_get("/trader/v1/accounts/{hash}/orders", self._orders)
        app.router.add_post("/trader/v1/accounts/{hash}/orders", self._place_order)
        app.router.add_get("/trader/v1/accounts/{hash}/orders/{id}", self._order)
        app.router.add_delete("/trader/v1/accounts/{hash}/orders/{id}", self._cancel_order)
        app.router.add_get("/trader/v1/userPreference", self._preferences)
        app.router.add_get("/marketdata/v1/quotes", self._quotes)
        app.router.add_get("/marketdata/v1/pricehistory", self._price_history)
        app.router.add_get("/marketdata/v1/chains", self._chains)
        app.router.add_get("/marketdata/v1/markets", self._markets)
        app.router.add_get("/marketdata/v1/movers/{index}", self._movers)
        app.router.add_get("/ws", self._stream)
        self._server = TestServer(app)
        await self._server.start_server()

    async def stop(self) -> None:
        for ws in list(self.sockets):
            await ws.close()
        if self._server is not None:
            await self._server.close()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return str(self._server.make_url("")).rstrip("/")

    @property
    def token_url(self) -> str:
        return f"{self.base_url}/v1/oauth/token"

    @property
    def ws_url(self) -> str:
        return self.base_url.replace("http://", "ws://") + "/ws"

    # ------------------------------------------------------------- test controls

    def issue_auth_code(self, redirect_uri: str | None = None) -> str:
        """What Schwab does after the user signs in: mint a single-use code."""
        self._counter += 1
        code = f"C0.code{self._counter}@"  # real codes end in '@' and arrive URL-encoded
        self.auth_codes[code] = redirect_uri or self.redirect_uri
        return code

    def seed_refresh_token(self, lifetime_s: float = REFRESH_LIFETIME_S) -> str:
        """Pretend the user authorised earlier and hand back the refresh token."""
        self._counter += 1
        token = f"refresh-{self._counter}"
        self.refresh_tokens = {token: self.now() + lifetime_s}  # a new grant voids older ones
        return token

    def expire_access_tokens(self) -> None:
        self.access_tokens.clear()

    def revoke_refresh_tokens(self) -> None:
        self.refresh_tokens.clear()

    def fail(
        self,
        method: str,
        path_contains: str,
        status: int | str,
        times: int = 1,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Make matching requests fail. ``status`` may be an HTTP code, "drop" (close the
        connection without answering), "drop_after" (process the request, then drop) or
        "delay" (answer normally after ``body`` seconds)."""
        self._faults.append(
            {
                "method": method,
                "path": path_contains,
                "status": status,
                "times": times,
                "body": body,
                "headers": headers,
            }
        )

    def calls(self, method: str, path_contains: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and path_contains in r["path"]]

    def set_quote(
        self,
        symbol: str,
        bid: float,
        ask: float,
        last: float | None = None,
        *,
        realtime: bool = True,
        at_ms: int | None = None,
    ) -> None:
        stamp = at_ms if at_ms is not None else int(self.now() * 1000)
        self.quotes[symbol] = {
            "assetMainType": "EQUITY",
            "assetSubType": "ETF",
            "quoteType": "NBBO",
            "realtime": realtime,
            "ssid": 1,
            "symbol": symbol,
            "quote": {
                "askPrice": ask,
                "askSize": 3,
                "askTime": stamp,
                "bidPrice": bid,
                "bidSize": 2,
                "bidTime": stamp,
                "closePrice": bid,
                "highPrice": ask,
                "lowPrice": bid,
                "lastPrice": last if last is not None else bid,
                "lastSize": 1,
                "mark": (bid + ask) / 2,
                "netChange": 0.0,
                "openPrice": bid,
                "quoteTime": stamp,
                "securityStatus": "Normal",
                "totalVolume": 1000,
                "tradeTime": stamp,
            },
            "reference": {
                "cusip": "000000000",
                "description": symbol,
                "exchange": "P",
                "exchangeName": "NYSE Arca",
            },
            "regular": {
                "regularMarketLastPrice": bid,
                "regularMarketLastSize": 1,
                "regularMarketNetChange": 0.0,
                "regularMarketPercentChange": 0.0,
                "regularMarketTradeTime": stamp,
            },
        }

    def fill(
        self, order_id: int | str, quantity: float | None = None, price: float | None = None
    ) -> None:
        order = self.orders[int(order_id)]
        amount = order.quantity - order.filled if quantity is None else quantity
        at = (
            price
            if price is not None
            else float(
                order.body.get("price") or self._mark(order.symbol, order.leg["instruction"])
            )
        )
        order.fills.append((amount, at))
        order.filled += amount
        held, avg = self.positions.get(order.symbol, (0.0, 0.0))
        size = 100 if order.leg["instrument"]["assetType"] == "OPTION" else 1
        if order.leg["instruction"].startswith("BUY"):
            self.cash -= amount * at * size
            total = held + amount
            self.positions[order.symbol] = (total, (held * avg + amount * at) / total)
        else:
            self.cash += amount * at * size
            remaining = held - amount
            if remaining == 0:
                self.positions.pop(order.symbol, None)
            else:
                self.positions[order.symbol] = (remaining, avg)
        if order.filled >= order.quantity:
            order.status = "FILLED"
            order.closed = datetime.now(UTC)

    def _mark(self, symbol: str, instruction: str) -> float:
        quote = self.quotes.get(symbol, {}).get("quote", {})
        return float(quote.get("askPrice" if instruction.startswith("BUY") else "bidPrice", 100.0))

    # ------------------------------------------------------------------ plumbing

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        body: Any = None
        if request.can_read_body and request.content_type == "application/json":
            body = await request.json()
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "json": body,
                "headers": dict(request.headers),
            }
        )
        for fault in self._faults:
            if (
                fault["times"] > 0
                and fault["method"] == request.method
                and fault["path"] in request.path
            ):
                fault["times"] -= 1
                if fault["status"] == "delay":
                    await asyncio.sleep(float(fault["body"]))
                    break
                if fault["status"] == "drop":
                    assert request.transport is not None
                    request.transport.close()
                    raise web.HTTPInternalServerError
                if fault["status"] == "drop_after":
                    await handler(request)
                    assert request.transport is not None
                    request.transport.close()
                    raise web.HTTPInternalServerError
                payload = (
                    fault["body"]
                    if fault["body"] is not None
                    else {
                        "errors": [{"id": "fake", "status": fault["status"], "title": "Injected"}]
                    }
                )
                return web.json_response(
                    payload, status=int(fault["status"]), headers=fault["headers"]
                )
        response: web.StreamResponse = await handler(request)
        return response

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        expiry = self.access_tokens.get(header[len("Bearer ") :])
        return expiry is not None and expiry > self.now()

    @staticmethod
    def _unauthorized() -> web.Response:
        return web.json_response(
            {
                "errors": [
                    {
                        "id": "fake",
                        "status": 401,
                        "title": "Unauthorized",
                        "detail": "Client not authorized",
                    }
                ]
            },
            status=401,
        )

    # --------------------------------------------------------------------- OAuth

    async def _token(self, request: web.Request) -> web.Response:
        expected = "Basic " + base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            return web.json_response({"error": "invalid_client"}, status=401)
        if request.content_type != "application/x-www-form-urlencoded":
            return web.json_response({"error": "invalid_request"}, status=400)
        form = await request.post()
        self.requests[-1]["form"] = {k: str(v) for k, v in form.items()}
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            code = str(form.get("code", ""))
            issued_for = self.auth_codes.pop(code, None)  # single use
            if issued_for is None or form.get("redirect_uri") != issued_for:
                return self._bad_grant("Exception while authenticating authorization code")
            refresh = self.seed_refresh_token()
            return self._token_response(refresh)
        if grant_type == "refresh_token":
            refresh = str(form.get("refresh_token", ""))
            expiry = self.refresh_tokens.get(refresh)
            if expiry is None or expiry <= self.now():
                return self._bad_grant("Exception while authenticating refresh token")
            if self.rotate_refresh_token:
                del self.refresh_tokens[refresh]
                self._counter += 1
                refresh = f"refresh-rotated-{self._counter}"
                self.refresh_tokens[refresh] = expiry  # rotation does not extend the 7 days
            return self._token_response(refresh)
        return web.json_response({"error": "unsupported_grant_type"}, status=400)

    @staticmethod
    def _bad_grant(description: str) -> web.Response:
        return web.json_response(
            {
                "error": "unsupported_token_type",
                "error_description": f'400 Bad Request: "{{"error_description":"{description}"}}"',
            },
            status=400,
        )

    def _token_response(self, refresh: str) -> web.Response:
        self._counter += 1
        access = f"access-{self._counter}"
        self.access_tokens[access] = self.now() + ACCESS_LIFETIME_S
        return web.json_response(
            {
                "expires_in": ACCESS_LIFETIME_S,
                "token_type": "Bearer",
                "scope": "api",
                "refresh_token": refresh,
                "access_token": access,
                "id_token": "fake-id-token",
            }
        )

    # ------------------------------------------------------------------ accounts

    async def _account_numbers(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        return web.json_response(
            [
                {"accountNumber": number, "hashValue": hash_}
                for hash_, number in self.accounts.items()
            ]
        )

    async def _preferences(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        return web.json_response(
            {
                "accounts": [
                    {
                        "accountNumber": ACCOUNT_NUMBER,
                        "primaryAccount": True,
                        "type": "BROKERAGE",
                        "nickName": "Individual",
                        "displayAcctId": "...678",
                        "autoPositionEffect": True,
                        "accountColor": "Green",
                    }
                ],
                "streamerInfo": [
                    {
                        "streamerSocketUrl": self.ws_url,
                        "schwabClientCustomerId": "CUSTOMER-ID",
                        "schwabClientCorrelId": "CORREL-ID",
                        "schwabClientChannel": "N9",
                        "schwabClientFunctionId": "APIAPP",
                    }
                ],
                "offers": [{"level2Permissions": True, "mktDataPermission": "NP"}],
            }
        )

    async def _account(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        if request.match_info["hash"] not in self.accounts:
            return web.json_response({"message": "Invalid account number"}, status=400)
        long_value = sum(q * self._mark(s, "SELL") for s, (q, _) in self.positions.items())
        liquidation = self.cash + long_value
        current: dict[str, Any] = {
            "accruedInterest": 0.0,
            "cashBalance": self.cash,
            "cashReceipts": 0.0,
            "longOptionMarketValue": 0.0,
            "liquidationValue": liquidation,
            "longMarketValue": long_value,
            "moneyMarketFund": 0.0,
            "savings": 0.0,
            "shortMarketValue": 0.0,
            "pendingDeposits": 0.0,
            "mutualFundValue": 0.0,
            "bondValue": 0.0,
            "shortOptionMarketValue": 0.0,
            "availableFunds": self.cash * 2,
            "availableFundsNonMarginableTrade": self.cash,
            "buyingPower": self.cash * 2,
            "buyingPowerNonMarginableTrade": self.cash,
            "dayTradingBuyingPower": self.cash * 4,
            "equity": liquidation,
            "equityPercentage": 100.0,
            "longMarginValue": long_value,
            "maintenanceCall": 0.0,
            "maintenanceRequirement": 0.0,
            "marginBalance": 0.0,
            "regTCall": 0.0,
            "shortBalance": 0.0,
            "shortMarginValue": 0.0,
            "sma": 0.0,
        }
        if self.account_type == "CASH":
            current = {
                "accruedInterest": 0.0,
                "cashAvailableForTrading": self.cash,
                "cashAvailableForWithdrawal": self.cash,
                "cashBalance": self.cash,
                "cashCall": 0.0,
                "longNonMarginableMarketValue": long_value,
                "totalCash": self.cash,
                "cashDebitCallValue": 0.0,
                "unsettledCash": 0.0,
                "liquidationValue": liquidation,
                "longMarketValue": long_value,
            }
        for name in self.omit_balance_fields:
            current.pop(name, None)
        account: dict[str, Any] = {
            "type": self.account_type,
            "accountNumber": self.accounts[request.match_info["hash"]],
            # Day-trade counting ended in June 2026. Sent anyway, to show nothing reads it.
            "roundTrips": 0,
            "isDayTrader": False,
            "isClosingOnlyRestricted": False,
            "pfcbFlag": False,
            "initialBalances": {
                "cashBalance": self.cash,
                "liquidationValue": liquidation,
                "accountValue": liquidation,
            },
            "currentBalances": current,
            "projectedBalances": {"availableFunds": self.cash, "buyingPower": self.cash * 2},
        }
        if self.account_type is None:
            del account["type"]
        if request.query.get("fields") == "positions" and self.positions:
            account["positions"] = [
                {
                    "shortQuantity": 0.0 if quantity >= 0 else -quantity,
                    "averagePrice": avg,
                    "currentDayProfitLoss": 0.0,
                    "currentDayProfitLossPercentage": 0.0,
                    "longQuantity": quantity if quantity >= 0 else 0.0,
                    "settledLongQuantity": quantity if quantity >= 0 else 0.0,
                    "settledShortQuantity": 0.0,
                    "instrument": {
                        # Schwab reports ETFs as COLLECTIVE_INVESTMENT, not EQUITY.
                        "assetType": "OPTION"
                        if len(symbol) == 21
                        else "COLLECTIVE_INVESTMENT"
                        if symbol in ("SPY", "QQQ")
                        else "EQUITY",
                        "cusip": "000000000",
                        "symbol": symbol,
                        "description": symbol,
                        "type": "EXCHANGE_TRADED_FUND",
                    },
                    "marketValue": quantity * self._mark(symbol, "SELL"),
                    "maintenanceRequirement": 0.0,
                    "averageLongPrice": avg,
                    "longOpenProfitLoss": 0.0,
                    "currentDayCost": 0.0,
                }
                for symbol, (quantity, avg) in self.positions.items()
            ]
        body = {
            "securitiesAccount": account,
            "aggregatedBalance": {
                "currentLiquidationValue": liquidation,
                "liquidationValue": liquidation,
            },
        }
        if "aggregatedBalance" in self.omit_balance_fields:
            del body["aggregatedBalance"]
        return web.json_response(body)

    # -------------------------------------------------------------------- orders

    async def _orders(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        for name in ("fromEnteredTime", "toEnteredTime"):
            if name not in request.query:
                return web.json_response({"message": f"{name} is required"}, status=400)
        start = datetime.strptime(request.query["fromEnteredTime"], "%Y-%m-%dT%H:%M:%S.%fZ")
        end = datetime.strptime(request.query["toEnteredTime"], "%Y-%m-%dT%H:%M:%S.%fZ")
        start, end = start.replace(tzinfo=UTC), end.replace(tzinfo=UTC)
        status = request.query.get("status")
        found = [
            o.to_json()
            for o in self.orders.values()
            if o.account_hash == request.match_info["hash"]
            and start <= o.entered <= end
            and (status is None or o.status == status)
        ]
        return web.json_response(found)

    async def _place_order(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        account_hash = request.match_info["hash"]
        if account_hash not in self.accounts:
            return web.json_response({"message": "Invalid account number"}, status=400)
        body = self.requests[-1]["json"]
        if self.reject_orders_with is not None:
            return web.json_response({"message": self.reject_orders_with}, status=400)
        problem = _validate_order(body)
        if problem:
            return web.json_response({"message": problem}, status=400)
        order = FakeOrder(self.next_order_id, account_hash, body)
        self.next_order_id += 1
        self.orders[order.order_id] = order
        if self.fill_on_place:
            self.fill(order.order_id)
        headers = {}
        if not self.omit_location_header:
            headers["Location"] = (
                f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/{order.order_id}"
            )
        return web.Response(status=201, headers=headers)

    async def _order(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        order = self.orders.get(int(request.match_info["id"]))
        if order is None or order.account_hash != request.match_info["hash"]:
            return web.json_response({"message": "Order not found"}, status=404)
        return web.json_response(order.to_json())

    async def _cancel_order(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        order = self.orders.get(int(request.match_info["id"]))
        if order is None or order.account_hash != request.match_info["hash"]:
            return web.json_response({"message": "Order not found"}, status=404)
        if order.status != "WORKING":
            return web.json_response({"message": "Order cannot be canceled"}, status=400)
        order.status = "CANCELED"
        order.closed = datetime.now(UTC)
        return web.Response(status=200)

    # --------------------------------------------------------------- market data

    async def _quotes(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        symbols = [s for s in request.query.get("symbols", "").split(",") if s]
        return web.json_response({s: self.quotes[s] for s in symbols if s in self.quotes})

    async def _chains(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        symbol = request.query.get("symbol", "")
        calls: dict[str, dict[str, list[dict[str, Any]]]] = {}
        puts: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for option in self.chains.get(symbol, []):
            side = calls if option["putCall"] == "CALL" else puts
            expiry = f"{option['expirationDate'][:10]}:{option['daysToExpiration']}"
            side.setdefault(expiry, {}).setdefault(str(option["strikePrice"]), []).append(option)
        return web.json_response(
            {
                "symbol": symbol,
                "status": "SUCCESS" if symbol in self.chains else "FAILED",
                "callExpDateMap": calls,
                "putExpDateMap": puts,
            }
        )

    def add_option(
        self,
        symbol: str,
        bid: float,
        ask: float,
        *,
        delta: float = 0.5,
        days: int = 7,
        realtime: bool = True,
    ) -> None:
        """Put a contract in its underlying's chain and give it a quote."""
        underlying = symbol[:6].strip()
        year, month, day = symbol[6:8], symbol[8:10], symbol[10:12]
        self.chains.setdefault(underlying, []).append(
            {
                "putCall": "CALL" if symbol[12] == "C" else "PUT",
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "last": bid,
                "delta": delta,
                "strikePrice": int(symbol[13:]) / 1000,
                "expirationDate": f"20{year}-{month}-{day}T20:00:00.000+00:00",
                "daysToExpiration": days,
                "multiplier": 100.0,
            }
        )
        self.set_quote(symbol, bid, ask, realtime=realtime)
        self.quotes[symbol]["assetMainType"] = "OPTION"

    async def _price_history(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        symbol = request.query.get("symbol", "")
        candles = self.candles.get(symbol, [])
        start, end = request.query.get("startDate"), request.query.get("endDate")
        if start is not None:
            candles = [c for c in candles if c["datetime"] >= int(start)]
        if end is not None:
            candles = [c for c in candles if c["datetime"] <= int(end)]
        return web.json_response({"candles": candles, "symbol": symbol, "empty": not candles})

    async def _movers(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        return web.json_response({"screeners": self.movers.get(request.match_info["index"], [])})

    async def _markets(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        day = request.query.get("date") or datetime.now(UTC).strftime("%Y-%m-%d")
        if not self.market_open:
            return web.json_response(
                {
                    "equity": {
                        self.closed_day_inner_key: {
                            "date": day,
                            "marketType": "EQUITY",
                            "product": "equity",
                            "isOpen": False,
                        }
                    }
                }
            )
        noon = datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=_NEW_YORK)
        raw_offset = noon.strftime("%z")  # -0400 in summer, -0500 in winter
        offset = f"{raw_offset[:3]}:{raw_offset[3:]}"
        return web.json_response(
            {
                "equity": {
                    "EQ": {
                        "date": day,
                        "marketType": "EQUITY",
                        "product": "EQ",
                        "productName": "equity",
                        "isOpen": True,
                        "sessionHours": {
                            "preMarket": [
                                {
                                    "start": f"{day}T07:00:00{offset}",
                                    "end": f"{day}T09:30:00{offset}",
                                }
                            ],
                            "regularMarket": [
                                {
                                    "start": f"{day}T{self.session_start}{offset}",
                                    "end": f"{day}T{self.session_end}{offset}",
                                }
                            ],
                            "postMarket": [
                                {
                                    "start": f"{day}T16:00:00{offset}",
                                    "end": f"{day}T20:00:00{offset}",
                                }
                            ],
                        },
                    }
                }
            }
        )

    # ------------------------------------------------------------------ streamer

    async def _stream(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        logged_in = False
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                requests = payload.get("requests", [payload])
                for item in requests:
                    self.stream_requests.append(item)
                    if self.stream_silent_subs and item["command"] == "SUBS":
                        continue
                    response = self._stream_reply(item, logged_in)
                    if item["service"] == "ADMIN" and item["command"] == "LOGIN":
                        logged_in = response["content"]["code"] == 0
                    if self.stream_stray_replies:
                        stray = {**response, "requestid": "not-yours", "content": {"code": 11}}
                        await ws.send_str(json.dumps({"response": [stray]}))
                    await ws.send_str(json.dumps({"response": [response]}))
                    if logged_in:
                        self._logged_in.add(ws)
                    else:
                        self._logged_in.discard(ws)
                    if item["service"] == "ADMIN" and not logged_in:
                        await ws.close()
        finally:
            self._logged_in.discard(ws)
            if ws in self.sockets:
                self.sockets.remove(ws)
        return ws

    def _stream_reply(self, item: dict[str, Any], logged_in: bool) -> dict[str, Any]:
        reply: dict[str, Any] = {
            "service": item["service"],
            "command": item["command"],
            "requestid": str(item["requestid"]),
            "SchwabClientCorrelId": item.get("SchwabClientCorrelId"),
            "timestamp": int(self.now() * 1000),
            "content": {"code": 0, "msg": "SUBS command succeeded"},
        }
        if item["service"] == "ADMIN" and item["command"] == "LOGIN":
            self.stream_logins += 1
            token = item["parameters"].get("Authorization")
            expiry = self.access_tokens.get(token)
            valid = expiry is not None and expiry > self.now()
            code = self.stream_login_code if valid else 3
            reply["content"] = {
                "code": code,
                "msg": "server=fake;status=PN" if code == 0 else "Login Denied.",
            }
            return reply
        if not logged_in:
            reply["content"] = {"code": 20, "msg": "Stream not logged in"}
            return reply
        if self.stream_subs_code != 0:
            reply["content"] = {"code": self.stream_subs_code, "msg": "SUBS command failed"}
            return reply
        keys = set(item.get("parameters", {}).get("keys", "").split(","))
        current = self.subscriptions.setdefault(item["service"], set())
        if item["command"] == "SUBS":
            current.clear()
            current.update(keys)
        elif item["command"] == "ADD":
            current.update(keys)
        elif item["command"] == "UNSUBS":
            current.difference_update(keys)
        return reply

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        await self.push_raw(json.dumps(payload))

    async def push_level_one(self, symbol: str, **fields: Any) -> None:
        """Send a LEVELONE_EQUITIES update. Field numbers are given as f1=..., f2=..."""
        content = {
            "key": symbol,
            "delayed": fields.pop("delayed", False),
            "assetMainType": "EQUITY",
            "assetSubType": "ETF",
            "cusip": "000000000",
        }
        content.update({name[1:]: value for name, value in fields.items()})
        await self._broadcast(
            {
                "data": [
                    {
                        "service": "LEVELONE_EQUITIES",
                        "timestamp": int(self.now() * 1000),
                        "command": "SUBS",
                        "content": [content],
                    }
                ]
            }
        )

    async def push_quote(
        self,
        symbol: str,
        bid: float,
        ask: float,
        last: float | None = None,
        *,
        at_ms: int | None = None,
        delayed: bool = False,
    ) -> None:
        stamp = at_ms if at_ms is not None else int(self.now() * 1000)
        await self.push_level_one(
            symbol,
            f1=bid,
            f2=ask,
            f3=last if last is not None else bid,
            f8=1000,
            f34=stamp,
            f35=stamp,
            delayed=delayed,
        )

    async def push_bar(
        self,
        symbol: str,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        start_ms: int,
        sequence: int = 1,
    ) -> None:
        await self._broadcast(
            {
                "data": [
                    {
                        "service": "CHART_EQUITY",
                        "timestamp": int(self.now() * 1000),
                        "command": "SUBS",
                        "content": [
                            {
                                "seq": sequence,
                                "key": symbol,
                                "1": sequence,
                                "2": open_,
                                "3": high,
                                "4": low,
                                "5": close,
                                "6": volume,
                                "7": start_ms,
                                "8": 19000,
                            }
                        ],
                    }
                ]
            }
        )

    async def push_heartbeat(self) -> None:
        await self._broadcast({"notify": [{"heartbeat": str(int(self.now() * 1000))}]})

    async def push_raw(self, text: str) -> None:
        """Send a frame to every logged-in socket. Like Schwab, never to one that has not
        logged in, and so never to one being closed after a refused login."""
        for ws in [ws for ws in self.sockets if ws in self._logged_in]:
            await ws.send_str(text)
        await asyncio.sleep(0)

    async def drop_streams(self) -> None:
        for ws in list(self.sockets):
            await ws.close(code=1011)


def _validate_order(body: Any) -> str | None:
    if not isinstance(body, dict):
        return "order body must be a JSON object"
    for name in ("orderType", "session", "duration", "orderStrategyType", "orderLegCollection"):
        if name not in body:
            return f"{name} is required"
    if body["orderType"] == "LIMIT":
        price = body.get("price")
        if not isinstance(price, str):
            return "price must be a string"
        try:
            if float(price) <= 0:
                return "price must be positive"
        except ValueError:
            return "price must be numeric"
        if "." in price and len(price.split(".")[1]) > 2 and float(price) >= 1:
            return "price has too many decimals"
    elif "price" in body:
        return "market order must not carry a price"
    legs = body["orderLegCollection"]
    if not isinstance(legs, list) or len(legs) != 1:
        return "exactly one leg is supported"
    leg = legs[0]
    if not isinstance(leg.get("quantity"), int) or leg["quantity"] <= 0:
        return "quantity must be a positive whole number"
    instrument = leg.get("instrument", {})
    if not instrument.get("symbol"):
        return "instrument must have a symbol"
    if instrument.get("assetType") == "EQUITY":
        allowed = ("BUY", "SELL")
    elif instrument.get("assetType") == "OPTION":
        allowed = ("BUY_TO_OPEN", "SELL_TO_CLOSE")
        if len(instrument["symbol"]) != 21:
            return "option symbol must be 21 characters"
        if body["orderType"] != "LIMIT":
            return "this fake only takes limit orders for options"
    else:
        return "instrument must be an EQUITY or an OPTION"
    if leg.get("instruction") not in allowed:
        return f"instruction must be one of {allowed}"
    return None


def redirect_url(server: FakeSchwab, code: str, state: str | None = None) -> str:
    """The URL the browser lands on after sign-in, as Schwab builds it."""
    from urllib.parse import quote

    url = f"{server.redirect_uri}/?code={quote(code, safe='')}&session=fake-session"
    if state is not None:
        url += f"&state={quote(state, safe='')}"
    return url


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
