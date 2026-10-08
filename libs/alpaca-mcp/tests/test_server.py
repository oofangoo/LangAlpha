from __future__ import annotations

import json

import httpx
import pytest
from alpaca_mcp.client import DATA_URL, PAPER_TRADING_URL

from tests.conftest import KEY, SECRET, json_of, text_of

ORDER = {
    "id": "61e69015-8549-4bfd-b9c3-01e75843f47d",
    "client_order_id": "lamcp-abc",
    "status": "accepted",
    "symbol": "AAPL",
    "side": "buy",
    "type": "market",
    "qty": "1",
}


async def test_lists_the_expected_tools_all_annotated(connect):
    async with connect() as client:
        tools = (await client.list_tools()).tools
    names = {t.name for t in tools}
    assert {"place_stock_order", "place_crypto_order", "replace_order_by_id",
            "cancel_order_by_id", "close_position", "get_order_by_id",
            "get_order_by_client_id", "get_orders", "get_all_positions",
            "get_account_info", "get_clock"} <= names
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is not None, tool.name


async def test_bulk_and_account_mutations_are_not_exposed(connect):
    async with connect() as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert not names & {"cancel_all_orders", "close_all_positions",
                        "update_account_config", "exercise_options_position"}


async def test_reads_and_writes_are_marked_apart(connect):
    async with connect() as client:
        by_name = {t.name: t for t in (await client.list_tools()).tools}
    assert by_name["get_orders"].annotations.read_only_hint is True
    assert by_name["place_stock_order"].annotations.read_only_hint is False
    assert by_name["cancel_order_by_id"].annotations.destructive_hint is True


async def test_every_call_goes_to_the_paper_host_with_the_callers_keys(connect, upstream):
    upstream.handler = lambda r: httpx.Response(200, json={"id": "acct"})
    async with connect() as client:
        await client.call_tool("get_account_info", {})
        await client.call_tool("get_stock_latest_quote", {"symbol": "aapl"})
    trading, data = upstream.requests
    assert str(trading.url) == f"{PAPER_TRADING_URL}/v2/account"
    assert str(data.url).startswith(f"{DATA_URL}/v2/stocks/AAPL/quotes/latest")
    for request in upstream.requests:
        assert request.headers["apca-api-key-id"] == KEY
        assert request.headers["apca-api-secret-key"] == SECRET


async def test_a_call_without_keys_never_reaches_alpaca(connect, upstream):
    async with connect(headers={}) as client:
        result = await client.call_tool("get_account_info", {})
    assert result.is_error
    assert "APCA-API-KEY-ID" in text_of(result)
    assert upstream.requests == []


async def test_keys_are_never_echoed_in_a_result(connect, upstream):
    upstream.handler = lambda r: httpx.Response(401, json={"code": 40110000, "message": "forbidden"})
    async with connect() as client:
        result = await client.call_tool("get_account_info", {})
    assert KEY not in text_of(result) and SECRET not in text_of(result)


async def test_place_stock_order_sends_a_validated_body_with_a_client_id(connect, upstream):
    upstream.handler = lambda r: httpx.Response(200, json=ORDER)
    async with connect() as client:
        result = await client.call_tool(
            "place_stock_order", {"symbol": "aapl", "side": "buy", "qty": 1}
        )
    assert not result.is_error
    assert json_of(result)["id"] == ORDER["id"]
    (request,) = upstream.requests
    assert request.method == "POST" and request.url.path == "/v2/orders"
    body = json.loads(request.content)
    assert body["symbol"] == "AAPL"
    assert body["client_order_id"].startswith("lamcp-")


async def test_an_invalid_order_is_refused_without_a_request(connect, upstream):
    async with connect() as client:
        result = await client.call_tool(
            "place_stock_order",
            {"symbol": "AAPL", "side": "buy", "qty": 1, "notional": 100},
        )
    assert result.is_error
    assert "exactly one" in text_of(result)
    assert upstream.requests == []


async def test_alpaca_refusal_comes_back_in_its_own_words(connect, upstream):
    upstream.handler = lambda r: httpx.Response(
        403, json={"code": 40310000, "message": "insufficient buying power"}
    )
    async with connect() as client:
        result = await client.call_tool(
            "place_stock_order", {"symbol": "AAPL", "side": "buy", "qty": 1000000}
        )
    assert result.is_error
    assert json_of(result) == {
        "message": "insufficient buying power",
        "http_status": 403,
        "code": 40310000,
    }


async def test_a_timeout_on_an_order_names_the_id_to_look_up(connect, upstream):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.handler = boom
    async with connect() as client:
        result = await client.call_tool(
            "place_stock_order",
            {"symbol": "AAPL", "side": "buy", "qty": 1, "client_order_id": "mine-9"},
        )
    assert result.is_error
    text = text_of(result)
    assert "may or may not have been placed" in text
    assert "client_order_id=mine-9" in text
    # Plain text, not a vendor body: nothing is known, so nothing is claimed.
    with pytest.raises(ValueError):
        json.loads(text)


async def test_ids_that_could_reshape_a_path_are_refused(connect, upstream):
    async with connect() as client:
        for args in ({"order_id": "../positions"}, {"order_id": "x/y"}):
            result = await client.call_tool("get_order_by_id", args)
            assert result.is_error
        bad = await client.call_tool("get_stock_snapshot", {"symbol": "AAPL/../x"})
        assert bad.is_error
    assert upstream.requests == []


async def test_get_orders_wraps_the_list_and_bounds_the_limit(connect, upstream):
    upstream.handler = lambda r: httpx.Response(200, json=[ORDER, ORDER])
    async with connect() as client:
        result = await client.call_tool("get_orders", {"status": "all", "limit": 9999})
    assert json_of(result)["count"] == 2
    assert upstream.requests[0].url.params["limit"] == "500"
    assert upstream.requests[0].url.params["status"] == "all"


async def test_cancel_and_replace_and_close_use_the_right_verbs(connect, upstream):
    upstream.handler = lambda r: httpx.Response(200, json={})
    oid = ORDER["id"]
    async with connect() as client:
        await client.call_tool("cancel_order_by_id", {"order_id": oid})
        await client.call_tool("replace_order_by_id", {"order_id": oid, "limit_price": 10.5})
        await client.call_tool("close_position", {"symbol": "BTC/USD", "percentage": 50})
    cancel, replace, close = upstream.requests
    assert (cancel.method, cancel.url.path) == ("DELETE", f"/v2/orders/{oid}")
    assert (replace.method, replace.url.path) == ("PATCH", f"/v2/orders/{oid}")
    assert json.loads(replace.content) == {"limit_price": "10.5"}
    assert close.method == "DELETE"
    assert close.url.raw_path.startswith(b"/v2/positions/BTC%2FUSD")
    assert close.url.params["percentage"] == "50"


async def test_health_endpoint():
    from alpaca_mcp.client import AlpacaClient
    from alpaca_mcp.server import build_app, build_server

    app = build_app(build_server(AlpacaClient(httpx.AsyncClient())), ["testserver"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        response = await http.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"


async def test_unlisted_host_is_refused_by_dns_rebinding_protection():
    from alpaca_mcp.client import AlpacaClient
    from alpaca_mcp.server import build_app, build_server

    app = build_app(build_server(AlpacaClient(httpx.AsyncClient())), ["alpaca-mcp:8765"])
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://evil.example"
    ) as http:
        response = await http.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"accept": "application/json, text/event-stream"},
        )
    assert response.status_code in (400, 403, 421)
