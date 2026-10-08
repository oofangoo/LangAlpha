"""A real MCP client talking to the real ASGI app, with Alpaca replaced by a stub.

The only fake is the network behind the client: headers, tool schemas and the
streamable-HTTP framing are the production ones, which is what makes a test of
"does this call carry the caller's keys and nothing else" worth having.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from alpaca_mcp.client import AlpacaClient, Credentials
from alpaca_mcp.server import build_app, build_server
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

KEY = "PKTESTKEYID"
SECRET = "test-secret-value"
HOST = "alpaca-mcp:8765"


@dataclass
class Upstream:
    """What Alpaca was asked, and what it will answer."""

    requests: list[httpx.Request] = field(default_factory=list)
    handler: Callable[[httpx.Request], httpx.Response] | None = None

    def bodies(self) -> list[Any]:
        return [json.loads(r.content) for r in self.requests if r.content]


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
async def connect(upstream: Upstream):
    """``connect(headers)`` -> an initialised MCP client against a fresh app.

    A fresh app per connection, because the session manager runs once per
    instance and its lifespan has to be entered and left by the same task as
    the test body, which a fixture cannot promise.
    """

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.requests.append(request)
        if upstream.handler is not None:
            return upstream.handler(request)
        return httpx.Response(200, json={})

    alpaca = AlpacaClient(httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    @contextlib.asynccontextmanager
    async def open_client(
        headers: dict[str, str] | None = None,
        default_credentials: Credentials | None = None,
    ) -> AsyncIterator[Client]:
        sent = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}
        if headers is not None:
            sent = headers
        app = build_app(build_server(alpaca, default_credentials), [HOST])
        async with app.router.lifespan_context(app):
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=f"http://{HOST}",
                headers=sent,
            )
            async with http:
                transport = streamable_http_client(
                    f"http://{HOST}/mcp", http_client=http
                )
                async with Client(transport) as client:
                    yield client

    yield open_client
    await alpaca.aclose()


def text_of(result: Any) -> str:
    return "".join(
        getattr(block, "text", "") for block in getattr(result, "content", [])
    )


def json_of(result: Any) -> Any:
    return json.loads(text_of(result))
