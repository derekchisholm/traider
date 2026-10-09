"""An in-process stand-in for the parts of Finnhub's API research uses.

Real HTTP on localhost. Paths, parameters and reply shapes follow Finnhub's public
documentation for the free tier; it has never been compared with the live service.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer

API_KEY = "fhtestkey0123456789ab"


class FakeFinnhub:
    def __init__(self) -> None:
        self.earnings: list[dict[str, Any]] = []
        self.company_news: dict[str, list[dict[str, Any]]] = {}
        self.general_news: Any = []  # a list; tests may set something else
        self.profiles: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.faults: list[dict[str, Any]] = []  # {"path": ..., "status": ..., "times": ...}
        self._server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/api/v1/calendar/earnings", self._earnings)
        app.router.add_get("/api/v1/company-news", self._company_news)
        app.router.add_get("/api/v1/news", self._news)
        app.router.add_get("/api/v1/stock/profile2", self._profile)
        self._server = TestServer(app)
        await self._server.start_server()

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.close()

    @property
    def base_url(self) -> str:
        assert self._server is not None
        return str(self._server.make_url("/api/v1"))

    def fail(self, path_contains: str, status: int | str, times: int = 1) -> None:
        """Answer matching requests with ``status``, or "drop" the connection."""
        self.faults.append({"path": path_contains, "status": status, "times": times})

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        self.requests.append(
            {"path": request.path, "query": dict(request.query), "headers": dict(request.headers)}
        )
        for fault in self.faults:
            if fault["times"] > 0 and fault["path"] in request.path:
                fault["times"] -= 1
                if fault["status"] == "drop":
                    assert request.transport is not None
                    request.transport.close()
                    raise web.HTTPInternalServerError
                return web.json_response({"error": "injected"}, status=int(fault["status"]))
        if request.headers.get("X-Finnhub-Token") != API_KEY:
            return web.json_response({"error": "Invalid API key"}, status=401)
        response: web.StreamResponse = await handler(request)
        return response

    async def _earnings(self, request: web.Request) -> web.Response:
        # Finnhub filters by from/to; the fake sends every row, odd ones included.
        return web.json_response({"earningsCalendar": self.earnings})

    async def _company_news(self, request: web.Request) -> web.Response:
        return web.json_response(self.company_news.get(request.query["symbol"], []))

    async def _news(self, request: web.Request) -> web.Response:
        return web.json_response(self.general_news)

    async def _profile(self, request: web.Request) -> web.Response:
        return web.json_response(self.profiles.get(request.query["symbol"], {}))
