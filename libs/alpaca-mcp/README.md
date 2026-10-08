# langalpha-alpaca-mcp

A small, paper-only [Alpaca](https://alpaca.markets) MCP server (streamable HTTP, stateless) built to sit behind
LangAlpha's egress relay, so Alpaca orders get the same per-order approval, ledger and reconciliation as the
shipped brokers.

It exists because Alpaca's official MCP server (`alpacahq/alpaca-mcp-server`) is a local stdio process with no
hosted endpoint, no MCP OAuth and no confirmation step, and LangAlpha can only gate orders that cross its relay.
See `docs/design/alpaca-paper-connector.md` for the full reasoning.

## What it guarantees

- **Paper only.** The trading host is the constant `https://paper-api.alpaca.markets`. There is no setting, header or
  argument that selects another. A live key sent to the paper host is refused by Alpaca.
- **Keys are never stored or logged.** Each request may carry `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`, which win.
  A request that carries neither falls back to `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` from the environment if
  both are set; a request with only half a pair is refused, never completed from the environment. **With the
  environment keys set, anyone who can reach the server trades that account**, which suits a single-user install and
  not a shared one. With them unset the server holds no secret at all.
- **Every order has a `client_order_id`** (`lamcp-…` unless the caller supplies one), so an order whose answer is
  lost can be looked up with `get_order_by_client_id`.
- **Arguments are checked before Alpaca sees them** (`alpaca_mcp/validation.py`): exactly one of `qty`/`notional`,
  prices that match the order type, no extended-hours flag where it cannot apply, symbols and ids that cannot
  reshape a URL path.
- **Honest failures.** An error in Alpaca's words comes back as a tool error whose body is
  `{"message", "http_status", "code"?}`. No answer at all (timeout, dropped connection) comes back as plain text
  that says the order *may or may not* have been placed and names the id to look up. LangAlpha's adapter keys on
  both.

## Tools

Names follow Alpaca's official server where one exists.

| Reads | Orders |
|---|---|
| `get_account_info`, `get_all_positions`, `get_open_position` | `place_stock_order`, `place_crypto_order` |
| `get_orders`, `get_order_by_id`, `get_order_by_client_id` | `replace_order_by_id`, `cancel_order_by_id` |
| `get_clock`, `get_calendar` | `close_position` |
| `get_stock_bars`, `get_stock_latest_quote`, `get_stock_snapshot` | |

Reads carry `readOnlyHint`; order tools carry `destructiveHint` where they cancel or close. Deliberately **not**
exposed: `cancel_all_orders`, `close_all_positions`, `update_account_config`, watchlist writes, options exercise and
locates. Each is one explicit step to add; the adapter's order-tool table in
`src/server/services/brokerage_capabilities.py` must be extended with it.

## Running it

```bash
# In the stack (no port is published; only containers on the compose network can reach it):
COMPOSE_PROFILES=infra,alpaca
EGRESS_PRIVATE_ALLOWLIST=http://alpaca-mcp:8765
docker compose up --build

# Standalone, loopback only:
cd libs/alpaca-mcp && uv run alpaca-mcp          # 127.0.0.1:8765
```

| Variable | Default | |
|---|---|---|
| `ALPACA_MCP_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | bind address |
| `ALPACA_MCP_PORT` | `8765` | |
| `ALPACA_MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*` | `Host` values accepted (DNS-rebinding protection); compose sets `alpaca-mcp:8765` |
| `ALPACA_MCP_LOG_LEVEL` | `INFO` | |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | unset | optional paper key pair used when a request carries none |

`GET /healthz` answers `ok`. The MCP endpoint is `/mcp`.

## Tests

```bash
cd libs/alpaca-mcp
uv run --group dev pytest        # or: PYTHONPATH=. pytest -c pyproject.toml --rootdir .
```

The tests drive the real HTTP app through a real MCP client with only Alpaca's network stubbed.
`tests/unit/server/services/brokerage_orders/test_alpaca_sidecar_contract.py` in the backend feeds the adapter what
this server actually emits, so a change to either side's field names or error sentences fails there.

## Not verified against a live account

The Alpaca REST paths and response shapes were taken from Alpaca's public documentation and the official server's
source, but the docs host was not reachable when this was written. Run it once against a real paper account before
trusting any of it (see the checklist in `docs/design/alpaca-paper-handoff.md`).
