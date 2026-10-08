"""The adapter reads what the real sidecar emits, not what a test author imagines.

``test_alpaca`` pins the adapter to hand-written payloads. This holds the other
half of the contract: it runs the actual ``libs/alpaca-mcp`` server over its real
HTTP app, with only Alpaca's network stubbed, and hands each tool result to the
adapter exactly as the relay would. If either side changes a field name, a
status word or the sentence that marks an unanswered placement, this is where it
shows.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

import httpx
import pytest

_SIDECAR = Path(__file__).resolve().parents[5] / "libs" / "alpaca-mcp"
if str(_SIDECAR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR))

pytest.importorskip("alpaca_mcp")

from alpaca_mcp.client import AlpacaClient
from alpaca_mcp.server import build_app, build_server
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from src.server.services.brokerage_orders import (
    AttemptStatus,
    adapter_for,
)

ALPACA = adapter_for("alpaca")
HOST = "alpaca-mcp:8765"
ORDER_ID = "61e69015-8549-4bfd-b9c3-01e75843f47d"
ORDER = {
    "id": ORDER_ID,
    "client_order_id": "lamcp-abc",
    "created_at": "2026-10-08T14:30:01.942282Z",
    "updated_at": "2026-10-08T14:30:01.942282Z",
    "symbol": "AAPL",
    "side": "buy",
    "qty": "1",
    "filled_qty": "0",
    "filled_avg_price": None,
    "type": "market",
    "time_in_force": "day",
    "limit_price": None,
    "notional": None,
    "status": "accepted",
}
KEYS = {"APCA-API-KEY-ID": "PKTEST", "APCA-API-SECRET-KEY": "secret"}


@contextlib.asynccontextmanager
async def sidecar(handler, headers=KEYS):
    alpaca = AlpacaClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    app = build_app(build_server(alpaca), [HOST])
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=f"http://{HOST}",
        headers=headers,
    ) as http:
        transport = streamable_http_client(f"http://{HOST}/mcp", http_client=http)
        async with Client(transport) as client:
            yield client
    await alpaca.aclose()


def as_relay_saw_it(result) -> tuple[str, str]:
    text = "".join(getattr(b, "text", "") for b in result.content)
    return text, "error" if result.is_error else "success"


async def place(handler, args=None, headers=KEYS):
    args = args or {"symbol": "AAPL", "side": "buy", "qty": 1}
    request = ALPACA.parse_request("place_stock_order", args)
    async with sidecar(handler, headers) as client:
        result = await client.call_tool("place_stock_order", args)
    body, status = as_relay_saw_it(result)
    return ALPACA.parse_result("place_stock_order", body, tool_status=status, request=request)


@pytest.mark.asyncio
async def test_an_accepted_placement_settles_as_submitted_with_the_order_id():
    outcome = await place(lambda r: httpx.Response(200, json=ORDER))
    assert outcome.status is AttemptStatus.SUBMITTED
    assert outcome.vendor_order_id == ORDER_ID


@pytest.mark.asyncio
async def test_alpacas_refusal_reaches_the_adapter_as_the_vendors_word():
    outcome = await place(
        lambda r: httpx.Response(
            403, json={"code": 40310000, "message": "insufficient buying power"}
        )
    )
    assert outcome.status is AttemptStatus.REJECTED_BY_VENDOR
    assert outcome.failure.code == "40310000"
    assert outcome.failure.message == "insufficient buying power"


@pytest.mark.asyncio
async def test_a_timeout_reaches_the_adapter_as_unknown_not_failed():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    outcome = await place(boom)
    assert outcome.status is AttemptStatus.UNKNOWN
    assert "client_order_id=lamcp-" in (outcome.raw_status or "")


@pytest.mark.asyncio
async def test_a_refusal_before_sending_is_terminal():
    outcome = await place(
        lambda r: pytest.fail("nothing should have been sent"),
        args={"symbol": "AAPL", "side": "buy", "qty": 1, "notional": 100},
    )
    assert outcome.status is AttemptStatus.REJECTED_BY_VENDOR
    assert outcome.failure.code == "not_sent"


@pytest.mark.asyncio
async def test_missing_credentials_are_terminal_too():
    outcome = await place(lambda r: pytest.fail("nothing should have been sent"), headers={})
    assert outcome.status is AttemptStatus.REJECTED_BY_VENDOR
    assert outcome.failure.code == "not_sent"


@pytest.mark.asyncio
async def test_a_cancel_reaches_the_adapter_as_cancelled():
    args = {"order_id": ORDER_ID}
    request = ALPACA.parse_request("cancel_order_by_id", args)
    async with sidecar(lambda r: httpx.Response(204)) as client:
        result = await client.call_tool("cancel_order_by_id", args)
    body, status = as_relay_saw_it(result)
    outcome = ALPACA.parse_result("cancel_order_by_id", body, tool_status=status, request=request)
    assert outcome.status is AttemptStatus.CANCELLED
    assert outcome.vendor_order_id == ORDER_ID


@pytest.mark.asyncio
async def test_a_listing_and_a_single_read_parse_through_the_status_queries():
    rows = [ORDER, {**ORDER, "id": "8f1d4b57-0c07-4a49-9d64-6b0f6b9d3c11", "status": "filled"}]
    async with sidecar(lambda r: httpx.Response(200, json=rows)) as client:
        query = ALPACA.status_query(None)
        result = await client.call_tool(query.tool, dict(query.args))
    body, _ = as_relay_saw_it(result)
    listed = ALPACA.parse_status(query, body)
    assert [o.outcome.status for o in listed] == [AttemptStatus.SUBMITTED, AttemptStatus.FILLED]

    async with sidecar(lambda r: httpx.Response(200, json=ORDER)) as client:
        query = ALPACA.status_query(None, vendor_order_id=ORDER_ID)
        result = await client.call_tool(query.tool, dict(query.args))
    body, _ = as_relay_saw_it(result)
    assert ALPACA.parse_status(query, body)[0].vendor_order_id == ORDER_ID


@pytest.mark.asyncio
async def test_alpaca_having_no_such_order_reads_as_absent_not_as_a_failed_read():
    missing = httpx.Response(404, json={"code": 40410000, "message": "order not found"})
    async with sidecar(lambda r: missing) as client:
        query = ALPACA.status_query(None, vendor_order_id=ORDER_ID)
        result = await client.call_tool(query.tool, dict(query.args))
    body, status = as_relay_saw_it(result)
    assert status == "error"
    assert json.loads(body)["http_status"] == 404
    assert ALPACA.parse_status(query, body) == []
