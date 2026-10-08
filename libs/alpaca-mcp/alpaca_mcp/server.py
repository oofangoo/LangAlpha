"""The Alpaca MCP server: a small, annotated, paper-only tool surface.

Tool names follow Alpaca's official MCP server where one exists, so a model that
knows that server needs no new vocabulary. The surface is far smaller on
purpose: the order-approval stack on the other side keys on exact tool names,
and a tool it has not been taught is a tool that places orders unseen.

Left out deliberately, each one an explicit step to add rather than an omission:
``cancel_all_orders``, ``close_all_positions``, ``update_account_config``,
watchlist writes, options exercise and locates.

Failure handling carries one distinction that the caller depends on. An error in
Alpaca's own words (a rejected order) comes back as a JSON body with Alpaca's
``code`` and ``message`` and the tool-error flag set. An error with no answer at
all (a timeout, a dropped connection) comes back as plain text, because for an
order that is the case where nothing is known about whether it exists.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from alpaca_mcp import validation as v
from alpaca_mcp.client import (
    AlpacaClient,
    AlpacaError,
    Credentials,
    MissingCredentials,
    UpstreamUnavailable,
)

logger = logging.getLogger("alpaca_mcp")

Side = Literal["buy", "sell"]
StockOrderType = Literal["market", "limit", "stop", "stop_limit", "trailing_stop"]
CryptoOrderType = Literal["market", "limit", "stop_limit"]
StockTif = Literal["day", "gtc", "opg", "cls", "ioc", "fok"]
CryptoTif = Literal["gtc", "ioc"]
Feed = Literal["iex", "sip"]

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _guard(
    tool: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Turn the three ways a call can fail into three honest answers."""
    import functools

    @functools.wraps(tool)
    async def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return await tool(*args, **kwargs)
        except v.InvalidOrder as e:
            return _error_result(f"refused before sending: {e}")
        except MissingCredentials as e:
            return _error_result(str(e))
        except AlpacaError as e:
            return _error_result(json.dumps(e.to_body()))
        except UpstreamUnavailable as e:
            return _error_result(str(e))

    return run


def build_server(client: AlpacaClient) -> MCPServer:
    server = MCPServer(
        name="alpaca-paper",
        instructions=(
            "Alpaca PAPER trading account. Orders here are simulated and move no "
            "real money. Every order carries a client_order_id; read an order "
            "back with get_order_by_id or get_order_by_client_id before assuming "
            "it filled."
        ),
    )

    def creds(ctx: Context) -> Credentials:
        return Credentials.from_headers(ctx.headers)

    async def trading(
        ctx: Context, method: str, path: str, **kwargs: Any
    ) -> Any:
        return await client.request(method, path, creds(ctx), **kwargs)

    # ----- reads -----------------------------------------------------------

    @server.tool(annotations=_READ)
    @_guard
    async def get_account_info(ctx: Context) -> Any:
        """Cash, buying power, equity and account status for the paper account.

        Use before sizing an order. Returns one account object.
        """
        return await trading(ctx, "GET", "/v2/account")

    @server.tool(annotations=_READ)
    @_guard
    async def get_all_positions(ctx: Context) -> Any:
        """Every open position with quantity, cost basis and unrealised P&L.

        Returns {positions, count}.
        """
        rows = await trading(ctx, "GET", "/v2/positions")
        return {"positions": rows, "count": len(rows)}

    @server.tool(annotations=_READ)
    @_guard
    async def get_open_position(ctx: Context, symbol: str) -> Any:
        """One open position by symbol. Fails if there is none.

        Returns one position object.
        """
        path = "/v2/positions/" + quote(v.position_symbol(symbol), safe="")
        return await trading(ctx, "GET", path)

    @server.tool(annotations=_READ)
    @_guard
    async def get_orders(
        ctx: Context,
        status: Literal["open", "closed", "all"] = "open",
        limit: int = 50,
        symbols: list[str] | None = None,
        after: str | None = None,
        until: str | None = None,
        direction: Literal["asc", "desc"] = "desc",
    ) -> Any:
        """Orders on the paper account, newest first.

        Use to check what is still working or what filled. after and until are
        ISO timestamps. Returns {orders, count}.
        """
        params = {
            "status": status,
            "limit": max(1, min(int(limit), 500)),
            "direction": direction,
            "after": after,
            "until": until,
            "symbols": ",".join(v.position_symbol(s) for s in symbols) if symbols else None,
        }
        rows = await trading(ctx, "GET", "/v2/orders", params=params)
        return {"orders": rows, "count": len(rows)}

    @server.tool(annotations=_READ)
    @_guard
    async def get_order_by_id(ctx: Context, order_id: str) -> Any:
        """One order by its Alpaca id. Returns the order with status and fills."""
        return await trading(ctx, "GET", "/v2/orders/" + v.order_id(order_id))

    @server.tool(annotations=_READ)
    @_guard
    async def get_order_by_client_id(ctx: Context, client_order_id: str) -> Any:
        """One order by the client_order_id it was placed with.

        Use after a placement that returned no answer, to learn whether the order
        exists. Returns the order, or an error if there is none.
        """
        return await trading(
            ctx,
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": v.client_order_id(client_order_id)},
        )

    @server.tool(annotations=_READ)
    @_guard
    async def get_clock(ctx: Context) -> Any:
        """Whether the US market is open now, and the next open and close."""
        return await trading(ctx, "GET", "/v2/clock")

    @server.tool(annotations=_READ)
    @_guard
    async def get_calendar(
        ctx: Context, start: str | None = None, end: str | None = None
    ) -> Any:
        """Trading days with open and close times. start and end are YYYY-MM-DD.

        Returns {days}.
        """
        rows = await trading(
            ctx, "GET", "/v2/calendar", params={"start": start, "end": end}
        )
        return {"days": rows}

    @server.tool(annotations=_READ)
    @_guard
    async def get_stock_bars(
        ctx: Context,
        symbol: str,
        timeframe: str = "1Day",
        start: str | None = None,
        end: str | None = None,
        limit: int = 100,
        adjustment: Literal["raw", "split", "dividend", "all"] = "raw",
        feed: Feed = "iex",
    ) -> Any:
        """OHLCV bars for one stock. timeframe is like 1Min, 1Hour, 1Day.

        The default iex feed is the free one; sip needs a paid data plan. Returns
        {symbol, bars, next_page_token}.
        """
        sym = v.stock_symbol(symbol)
        return await trading(
            ctx,
            "GET",
            f"/v2/stocks/{quote(sym, safe='')}/bars",
            base="data",
            params={
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": max(1, min(int(limit), 1000)),
                "adjustment": adjustment,
                "feed": feed,
            },
        )

    @server.tool(annotations=_READ)
    @_guard
    async def get_stock_latest_quote(ctx: Context, symbol: str, feed: Feed = "iex") -> Any:
        """The latest bid and ask for one stock."""
        sym = v.stock_symbol(symbol)
        return await trading(
            ctx,
            "GET",
            f"/v2/stocks/{quote(sym, safe='')}/quotes/latest",
            base="data",
            params={"feed": feed},
        )

    @server.tool(annotations=_READ)
    @_guard
    async def get_stock_snapshot(ctx: Context, symbol: str, feed: Feed = "iex") -> Any:
        """Latest trade, quote, minute bar and daily bar for one stock."""
        sym = v.stock_symbol(symbol)
        return await trading(
            ctx,
            "GET",
            f"/v2/stocks/{quote(sym, safe='')}/snapshot",
            base="data",
            params={"feed": feed},
        )

    # ----- orders ----------------------------------------------------------

    @server.tool(annotations=_WRITE)
    @_guard
    async def place_stock_order(
        ctx: Context,
        symbol: str,
        side: Side,
        qty: float | None = None,
        notional: float | None = None,
        type: StockOrderType = "market",
        time_in_force: StockTif = "day",
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail_price: float | None = None,
        trail_percent: float | None = None,
        extended_hours: bool = False,
        take_profit_limit_price: float | None = None,
        stop_loss_stop_price: float | None = None,
        stop_loss_limit_price: float | None = None,
        client_order_id: str | None = None,
    ) -> Any:
        """Place a stock order on the paper account.

        Give exactly one of qty (shares) and notional (dollars). A notional
        order must be a market order with time_in_force day. limit_price is
        required for limit and stop_limit, stop_price for stop and stop_limit,
        and exactly one of trail_price and trail_percent for trailing_stop.
        take_profit_limit_price with stop_loss_stop_price makes a bracket; either
        alone makes a one-triggers-other order. Omit client_order_id to have one
        generated. Returns the order, which is usually still working: read it back
        before assuming a fill.
        """
        body = v.build_stock_order(
            symbol=symbol,
            side=side,
            qty=qty,
            notional=notional,
            order_type=type,
            time_in_force=time_in_force,
            limit_price=limit_price,
            stop_price=stop_price,
            trail_price=trail_price,
            trail_percent=trail_percent,
            extended_hours=extended_hours,
            take_profit_limit_price=take_profit_limit_price,
            stop_loss_stop_price=stop_loss_stop_price,
            stop_loss_limit_price=stop_loss_limit_price,
            client_order_id_value=client_order_id,
        )
        return await _place(trading, ctx, body)

    @server.tool(annotations=_WRITE)
    @_guard
    async def place_crypto_order(
        ctx: Context,
        symbol: str,
        side: Side,
        qty: float | None = None,
        notional: float | None = None,
        type: CryptoOrderType = "market",
        time_in_force: CryptoTif = "gtc",
        limit_price: float | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
    ) -> Any:
        """Place a crypto order on the paper account. symbol is a pair like BTC/USD.

        Give exactly one of qty (units) and notional (dollars). A notional order
        must be a market order. limit_price is required for limit and stop_limit,
        and stop_price for stop_limit. Returns the order; read it back before
        assuming a fill.
        """
        body = v.build_crypto_order(
            symbol=symbol,
            side=side,
            qty=qty,
            notional=notional,
            order_type=type,
            time_in_force=time_in_force,
            limit_price=limit_price,
            stop_price=stop_price,
            client_order_id_value=client_order_id,
        )
        return await _place(trading, ctx, body)

    @server.tool(annotations=_WRITE)
    @_guard
    async def replace_order_by_id(
        ctx: Context,
        order_id: str,
        qty: float | None = None,
        time_in_force: StockTif | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trail: float | None = None,
    ) -> Any:
        """Change a working order's size, price or duration. Give at least one field.

        The order keeps its place only if Alpaca accepts the change; the old
        order is replaced by a new one with a new id. Returns the new order.
        """
        body = v.build_replace(
            qty=qty,
            time_in_force=time_in_force,
            limit_price=limit_price,
            stop_price=stop_price,
            trail=trail,
        )
        return await trading(ctx, "PATCH", "/v2/orders/" + v.order_id(order_id), body=body)

    @server.tool(annotations=_DESTRUCTIVE)
    @_guard
    async def cancel_order_by_id(ctx: Context, order_id: str) -> Any:
        """Cancel one working order by its Alpaca id. Returns {} when accepted.

        Acceptance is not cancellation: read the order back to see it settle.
        """
        return await trading(ctx, "DELETE", "/v2/orders/" + v.order_id(order_id))

    @server.tool(annotations=_DESTRUCTIVE)
    @_guard
    async def close_position(
        ctx: Context,
        symbol: str,
        qty: float | None = None,
        percentage: float | None = None,
    ) -> Any:
        """Close all or part of one position with a market order.

        Give qty or percentage (up to 100) to close part; give neither to close
        it all. Returns the order that was placed.
        """
        params = v.close_params(qty, percentage)
        path = "/v2/positions/" + quote(v.position_symbol(symbol), safe="")
        return await trading(ctx, "DELETE", path, params=params)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Any) -> Any:
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    return server


async def _place(
    trading: Callable[..., Awaitable[Any]], ctx: Context, body: dict[str, Any]
) -> Any:
    """Send an order, and say what is known if no answer comes back.

    The id is minted before the call so a timeout can still name it: the one
    thing that settles whether an unanswered order exists is looking it up.
    """
    try:
        order = await trading(ctx, "POST", "/v2/orders", body=body)
    except UpstreamUnavailable as e:
        raise UpstreamUnavailable(
            f"{e}. The order may or may not have been placed; look it up with "
            f"get_order_by_client_id using client_order_id={body['client_order_id']}"
        ) from e
    logger.info(
        "placed %s %s %s id=%s client_order_id=%s status=%s",
        body["side"],
        body["symbol"],
        body["type"],
        order.get("id") if isinstance(order, dict) else None,
        body["client_order_id"],
        order.get("status") if isinstance(order, dict) else None,
    )
    return order


def build_app(server: MCPServer, allowed_hosts: list[str] | None = None) -> Any:
    """The streamable-HTTP app. Stateless, because every call carries its own keys."""
    return server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts or ["127.0.0.1:*", "localhost:*"],
        ),
    )


def main() -> None:
    import asyncio

    import uvicorn

    logging.basicConfig(level=os.environ.get("ALPACA_MCP_LOG_LEVEL", "INFO"))
    hosts = [
        h.strip()
        for h in os.environ.get(
            "ALPACA_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*"
        ).split(",")
        if h.strip()
    ]
    client = AlpacaClient.create()
    app = build_app(build_server(client), hosts)
    try:
        uvicorn.run(
            app,
            host=os.environ.get("ALPACA_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("ALPACA_MCP_PORT", "8765")),
            log_level=os.environ.get("ALPACA_MCP_LOG_LEVEL", "info").lower(),
        )
    finally:
        asyncio.run(client.aclose())


if __name__ == "__main__":
    main()
