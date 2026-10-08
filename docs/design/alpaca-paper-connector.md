# Alpaca paper-trading connector

Status: **backend half built and unit-tested; the connect flow, consent and reconciliation for a header-credential
connector are not**. The connector is therefore hidden unless the operator opts in, and cannot yet be used end to
end. `alpaca-paper-handoff.md` lists what is left, in order.

## Goal

Let the agent trade an **Alpaca paper account** with the same safeguards the shipped brokers get: per-order approval,
a durable attempt ledger, reconciliation, the Orders page and capability consent. Paper only. A live Alpaca account
is out of scope and unreachable by configuration.

## Why the obvious approach does not work

Alpaca's official MCP server (`alpacahq/alpaca-mcp-server`, PyPI `alpaca-mcp-server`) is a **local stdio** process.
It documents no hosted endpoint and no MCP OAuth, has no confirmation step, and its tools carry no `readOnlyHint` /
`destructiveHint` (upstream issue #127), so `cancel_all_orders` looks like a read to a client.

LangAlpha cannot gate a stdio server's orders:

- A server reachable only over stdio is bound as a sandbox wrapper, but the `trading` capability group allows only the
  **direct** path (`tool_binding.py`), because the direct path is the one that runs through the egress relay.
- The single-use execution grant that backs approval is verified **inside the relay**, on the MCP frame
  (`egress/relay.py::_authorize_order`).
- Vendor identity is the server **URL host** (`brokerage_capabilities.vendor_for_url`), which a stdio server lacks.
- The relay dials with `pin_public_url(..., require_https=True)`, which rejects any non-global address.

So an Alpaca connector has to be a remote streamable-HTTP MCP server the relay can reach.

## What is built

### 1. The sidecar (`libs/alpaca-mcp`, `deploy/Dockerfile.alpaca-mcp`, compose profile `alpaca`)

A small server of our own, not the official one wrapped in HTTP: the official tool surface is large, unannotated and
renamed between major versions, our order-tool table is keyed by exact tool name, and v2.3.x has open order bugs.

- Stateless streamable HTTP. **The paper host is a constant**; no setting selects another.
- Holds no credentials. Each call carries `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY`, forwarded and never stored.
- Every order gets a `client_order_id`. Arguments are validated before Alpaca sees them.
- Tool names follow the official server. Reads carry `readOnlyHint`. Bulk cancel, close-all, account-config writes,
  watchlist writes, options exercise and locates are deliberately absent.
- A refusal in Alpaca's words is a tool error with a JSON body; **no answer at all** is plain text that says the order
  *may or may not* exist and names the id to look up. The adapter keys on both.
- Compose publishes no port; the service is reachable by name on the stack's network only.

### 2. The operator's private-origin allowance (`EGRESS_PRIVATE_ALLOWLIST`)

The sidecar is on a private address over plain HTTP, which every SSRF guard here refuses. The allowance is the one
deliberate hole, kept as narrow as possible:

- **Operator-set** from the environment, never from a catalog row or the UI. Empty by default, which changes nothing.
- Entries are exactly `scheme://host:port`. A path, credential, wildcard or CIDR is refused (and logged), not
  interpreted. Matching is exact on scheme, host and port.
- It lifts the https and global-address rules for that origin **only at the three places that dial a server's own
  address**: the relay (`egress/relay.py`), the probe (`mcp_probe.py`, including its preflight via `pinned_request`),
  and the write-time URL validator (`models/mcp_server.py::validate_remote_url`). Every other hop, notably OAuth
  discovery, keeps the old rules because its URLs are supplied by a remote party.
- The resolved address is still checked: link-local (cloud metadata), multicast, unspecified and reserved are refused;
  loopback only if the operator listed a loopback host; otherwise it must be private.
- A user can create a row pointing at the allowlisted origin. That is acceptable only because the sidecar is stateless
  and secret-free: reaching it yields nothing the caller's own keys did not, and the paper host is hardcoded.
  **Only list services with those properties.**

### 3. The adapter, capability map and registry entry

- `brokerage_orders/alpaca.py`: request parsing (stock, crypto, replace, cancel, close), outcome parsing, status
  reads (by id when the order id is known, otherwise a paged listing), a fingerprint for an order whose answer was
  lost, and time-based paging.
- `brokerage_capabilities.py`: `alpaca` in `_CURATION` (`market_data`, `paper_trading`) and `_ORDER_TOOLS` (all
  `OrderMode.PAPER`). The status reads live in `paper_trading` beside the order tools, so the existing invariant that
  no grant passes an order it cannot settle holds without special-casing. There is no `account` group or `trading`
  rung: there is no real account to guard.
- `brokerages.py`: `Brokerage.operator_hosted`. The entry is listed and enableable **only once the operator has
  allowed its address**.
- Migration `063` frees the `alpaca` name. It deliberately has no workspace-tier half: migration 055 deleted those rows
  and installed a trigger that refuses writes to them.

An unanswered placement is `unknown`, never `failed`: a failed attempt reads as leave to place the order again, and
the order may exist. Reconciliation then finds it by what was ordered, as it does at IBKR.

## What was found while building it (and is not solved)

**A header-credential connector has no consent, no reconciliation and no connect flow.** The shipped brokers are all
OAuth connections, and a lot is keyed on that:

- Capability consent is stored on the OAuth connection. A row authenticated by headers is treated as consenting to
  **nothing**, which denies a curated vendor's whole curation (`egress_grants._header_policies`,
  `mcp_config` ~L530/566, `direct_tools._identity`, `mcp_catalog` ~L225/716).
- Order reconciliation looks up an OAuth connection and a grant by `connection_id`
  (`orders/reconcile.py::_reconcile_group`, `database/order_reconciliation.py::active_grant_for_connection`). A header
  grant has neither, so attempts would stay open.
- Shipped brokerage rows are not editable (`app/mcp_servers.py`), so there is nowhere for a user to put keys.

Until those three exist the Alpaca connector cannot work end to end, which is why it is hidden by default.

## Evaluation interplay (trading-agents-poc)

The agent must use its **own** Alpaca paper account, not the one `tapoc` trades. Sharing it would contaminate tapoc's
SPY comparison, drawdown halt and signal scorecard. Alpaca paper accounts have separate keys.

## Verification so far

- Sidecar: 41 tests through a real MCP client and the real ASGI app with only Alpaca's network stubbed; also run as a
  real process (`/healthz`, missing-credentials error).
- Adapter: 73 unit tests, mutation-checked on two behaviours; 8 contract tests run the real sidecar into the adapter.
- Egress allowance: 30 tests, mostly about what it refuses.
- Migration `063`: applied on a real Postgres 16 through the full chain `001 -> 063` with seeded rows (including a name
  collision and a tombstone), then inspected.
- Backend unit suite: 16,301 passed. One failure, `test_livefs_daemon::test_root_lays_the_links_as_the_folders_owner`,
  also fails on the untouched base because the container runs as root.

**Not verified:** anything against a live Alpaca account, the Docker stack, the frontend, or the approval flow end to
end. The Alpaca REST paths and shapes came from public documentation and the official server's source; the docs host
was not reachable when this was written.
