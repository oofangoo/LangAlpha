# Alpaca paper-trading connector

Status: proposal. Nothing here is implemented yet.

## Goal

Let the agent trade an **Alpaca paper account** with the same safeguards the shipped brokers get: per-order
approval, a durable attempt ledger, reconciliation, the Orders page and capability consent. Paper only. A live
Alpaca account is out of scope and must be impossible to reach by configuration.

## Why the obvious approach does not work

Alpaca's official MCP server (`alpacahq/alpaca-mcp-server`, PyPI `alpaca-mcp-server`) is a **local stdio**
process. The README documents no hosted endpoint and no MCP OAuth, and says that binding its HTTP transport to
`0.0.0.0` gives no authentication. It has no confirmation step, and its tools carry no `readOnlyHint` /
`destructiveHint` annotations (upstream issue #127), so `cancel_all_orders` looks like a read to a client.

LangAlpha cannot gate a stdio server's orders:

- A server reachable only over stdio is bound as a sandbox wrapper. The `trading` capability group allows only
  the **direct** path (`tool_binding.py`), because the direct path is the one that runs through the egress relay.
- The single-use execution grant that backs approval is verified **inside the relay**, on the MCP frame
  (`egress/relay.py::_authorize_order`). A tool called any other way has no equivalent.
- Vendor identity is the server **URL host** (`brokerage_capabilities.vendor_for_url`). A stdio server has none.
- The relay dials with `pin_public_url(..., require_https=True)`, which rejects any non-global address.

So an Alpaca connector has to be a remote streamable-HTTP MCP server the relay can reach.

## Design

### 1. A small Alpaca MCP server that we own (`services/alpaca_mcp/`)

Not the official server wrapped in HTTP. Reasons: its tool surface is large (the README lists 72) and renamed
between major versions; our order-tool table is keyed by exact tool name; it validates almost nothing; and
v2.3.x has open order bugs (e.g. #129, option limit orders). A thin server of our own gives a stable, small,
annotated surface that we can pin tests to.

- Streamable-HTTP, stateless per request.
- **Paper host hardcoded** (`https://paper-api.alpaca.markets`). There is no setting that selects live.
- Credentials arrive per request as headers (`APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`) and are forwarded to
  Alpaca. They are never written to disk or logged. This is the existing `header_mcp` grant kind: the relay
  expands the catalog row's headers from the user's **vault**, so keys stay per user and rotate instantly.
- Every placement carries a `client_order_id`. If the caller omits one, the server generates it. This gives
  idempotent retries and a join key for reconciliation.
- Arguments are validated before Alpaca sees them (qty xor notional, price required for limit/stop, TIF
  allowed for the order type and asset class).
- Initial tools (reads): `get_account`, `get_positions`, `get_orders`, `get_order`, `get_clock`,
  `get_calendar`, `get_bars`, `get_latest_quote`, `get_snapshot`.
  Order tools: `place_order`, `replace_order`, `cancel_order`, `close_position`.
  Deliberately **not** exposed: `cancel_all_orders`, `close_all_positions`, `update_account_config`, watchlist
  writes, options exercise, locates. Each of these can be added later as a deliberate step.
- Equities and crypto first. Options are deferred (paper support is unclear, and upstream option ordering is
  broken in 2.3.x).

### 2. Platform changes in LangAlpha

| Area | Change |
|---|---|
| `brokerages.py` | New `alpaca` entry. `url` is the sidecar's address, named in source like every other broker. |
| `brokerage_capabilities.py` | Capability groups for `alpaca` (read, paper trading) and `_ORDER_TOOLS` entries, all `OrderMode.PAPER`. |
| `brokerage_orders/alpaca.py` | `AlpacaOrderAdapter`: request parsing, outcome parsing, status query, listing parse, match key. Modelled on the IBKR adapter (the smallest) and moomoo's paper path. Registered in `adapter_for`. |
| Status mapping | `new`/`accepted`/`pending_new`/`held` → working; `partially_filled`; `filled`; `canceled`/`expired`/`replaced` → cancelled; `rejected`. `pending_replace`/`pending_cancel` stay open. |
| Migration | Reserve the `alpaca` catalog name (the `030_free_brokerage_names` pattern). |
| Egress | A **narrow** allowance so the relay and the discovery probe may dial the sidecar on the private Docker network over plain HTTP. See "Security-sensitive change". |
| Compose | `alpaca-mcp` service behind a profile, no published port. |
| Frontend | Locale strings, brand icon, and the consent dialog already reads the capability map. |
| Tests | Adapter tests with recorded Alpaca payloads; sidecar tests with a mocked Alpaca; a relay test for the egress allowance. |

### Security-sensitive change

The relay's `pin_public_url` guard exists to stop SSRF. The sidecar lives on a private address, so some
exception is unavoidable for a self-hosted install. It must be:

- **Operator-set**, from an environment variable, never from a catalog row or the UI.
- An **exact host:port match**, not a CIDR or a "private is fine" switch.
- Applied only to a destination whose vendor resolves to `alpaca`.

Repointing the row elsewhere therefore loses both the vendor identity and the exception. This is the part of the
change that most deserves review.

## Evaluation interplay (trading-agents-poc)

The agent must use its **own** Alpaca paper account, not the one `tapoc` trades. Sharing it would contaminate
tapoc's SPY comparison, drawdown halt and signal scorecard. Alpaca paper accounts have separate keys.

## Known unknowns (to verify before or during implementation)

- Alpaca REST details (base URLs, header names, multi-account limit) came from search snippets and memory; the
  docs host was not reachable from the research environment. Verify against a real paper account.
- The shape of `client_order_id` collisions and the exact error body for duplicate ids.
- Whether the relay's header allowlist and discovery probe accept a plain-HTTP internal URL without further
  changes (to be found by running the stack).
- The full stack (Postgres, Redis, Docker) has not been run in the authoring environment, so end-to-end
  behaviour is unverified until run on a real host.

## Phasing

1. Sidecar + tests (no platform change).
2. Adapter, capability map, migration, tests.
3. Egress allowance + compose wiring.
4. Frontend strings/icon.
5. End-to-end run against a real paper account, then a short write-up.
