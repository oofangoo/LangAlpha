# Alpaca paper connector: handoff

Read `alpaca-paper-connector.md` first for the design and the reasons. This file is the state of the work and what to
do next. Branch: `alpaca-paper` on `oofangoo/langalpha` (a fork of `ginlix-ai/langalpha`). No pull request is open.

## State

| | |
|---|---|
| Sidecar `libs/alpaca-mcp` (tools, validation, Dockerfile, compose profile) | built, 41 tests |
| Private-origin allowance `EGRESS_PRIVATE_ALLOWLIST` (relay, probe, URL validator) | built, 30 tests |
| `AlpacaOrderAdapter`, capability map, registry entry, migration `063` | built, 73 + 8 tests, migration run on real Postgres |
| Header-credential **consent** for an operator-hosted brokerage (`header_consent`) | built; checked on real grant rows (0 Alpaca tools denied, a moomoo-host header row still 101) |
| Order **reconciliation** for a header-credential connection (`active_header_grant`) | built, 4 tests; the new SQL run on a real Postgres |
| Keys from `.env` (`ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`, sidecar fallback) | built, tested |
| Per-user keys in the vault via a credentials endpoint and UI | **not built**; only needed for a multi-user install (blocker 2, now optional) |
| Frontend (tile, credentials dialog, strings, icon) | **not built** |
| End to end against the Docker stack and a real Alpaca paper account | **never run** |

With the allowance unset, which is the default, nothing changes anywhere: the connector is not listed and cannot be
enabled.

### Single-user setup (what to do on your own machine)

Nothing below has been run end to end. It is the path the code was written for.

1. **Alpaca paper keys.** Use a paper account that is not the one `trading-agents-poc` trades. Paper key ids start with
   `PK`; a live key is refused by Alpaca on the paper host.
2. **Get the branch.** `git fetch origin && git checkout alpaca-paper`. If you pulled this branch before its history was
   rewritten, `git reset --hard origin/alpaca-paper`.
3. **Run `make config` first** (model, data, sandbox, search). It rewrites `COMPOSE_PROFILES` in `.env`
   (`scripts/configure.sh`), so anything you set for Alpaca before it is lost. Choose a model you can actually use: the
   agent needs one.
4. **Then add to `.env`:**
   ```bash
   COMPOSE_PROFILES=infra,alpaca            # keep "infra" if the wizard set it; add "alpaca"
   EGRESS_PRIVATE_ALLOWLIST=http://alpaca-mcp:8765
   ALPACA_API_KEY_ID=PK...
   ALPACA_API_SECRET_KEY=...
   EGRESS_RELAY_SECRET=<openssl rand -hex 32>
   ```
   `EGRESS_RELAY_SECRET` is required and the wizard does not set it. Without it the relay is disabled and the agent
   gets no direct tools at all, Alpaca's included.
5. **`make up`**, then open http://localhost:5173.
6. **Enable the connector** (no UI yet; the self-host API takes no sign-in):
   `curl -X PATCH localhost:8000/api/v1/mcp/brokerages/alpaca/enabled -H 'content-type: application/json' -d '{"enabled": true}'`.
   `GET localhost:8000/api/v1/mcp/brokerages` should list `alpaca` once the allowlist is set. If it does not, the
   allowlist did not reach the backend.
7. **Make paper orders ask first.** By default **paper orders do not ask for approval** (`order_approval_map`: "paper
   asks only when its own switch does"), so the agent can place them unprompted. To require an approval card:
   `curl -X PATCH localhost:8000/api/v1/mcp/servers/alpaca/binding -H 'content-type: application/json' -d '{"order_approval": {"paper": true}}'`.
8. In a workspace, ask the agent to read the account, then to place a small order.

Self-host has one local user and no sign-in, so anyone who can reach port 8000 acts as you. With the Alpaca keys in
`.env` that includes trading that paper account. Keep the stack on your machine or a private network.

## The platform gaps (1 and 3 are done; 2 became optional)

Every shipped broker is an OAuth connection, and a lot is keyed on that. A header-credential connector (Alpaca has no
OAuth) falls through three gaps. In this order:

### 1. Consent (done)

Kept for the record; the fix is `header_consent` in `brokerage_capabilities.py`, used at each site below.

A row authenticated by headers has no consent record, and every reader treats that as consent to **nothing**, which
denies a curated vendor's entire curation. So today every Alpaca tool would be refused.

Fix: a single helper in `brokerage_capabilities.py`, say `header_consent(vendor) -> tuple[str, ...]`, returning every
group key of an `operator_hosted` brokerage and `()` for all others (so no existing behaviour moves). Use it where the
`()` literal is passed for a connection-less row:

- `src/server/database/egress_grants.py`: `_header_policies` (the `_policy(vendor_for_url(row["url"]), (), row)` call
  near line 144) and the second header path near line 957.
- `src/server/services/mcp_config.py`: the no-connection branches near lines 530 and 566.
- `src/server/services/egress/direct_tools.py`: `_identity`, near line 329.
- `src/server/app/mcp_catalog.py`: near lines 225 and 716.

Keep the existing tests that assert a header row at a shipped OAuth vendor consents to nothing. Add tests that an
operator-hosted vendor's header row gets its groups. This is safe to grant fixed: the connector is paper-only by
construction and orders still go through the approval gate (`order_approval` switches on the row).

### 2. Credentials (optional now)

Keys can come from `.env` through the sidecar, which is enough for a single-user install. This section is for a shared
install, where one account for everyone is wrong and each user needs their own keys. Not built.

Shipped brokerage rows are not editable (`app/mcp_servers.py` ~L223 and ~L464), and `_create_brokerage_row` in
`app/mcp_brokerages.py` builds the row with no headers. There is nowhere for a user to put keys.

Fix:
- For an `operator_hosted` brokerage, create the row with header refs to vault secrets, e.g.
  `APCA-API-KEY-ID: ${vault:ALPACA_PAPER_KEY_ID}` and `APCA-API-SECRET-KEY: ${vault:ALPACA_PAPER_SECRET_KEY}`.
  Check `_validate_header_map` accepts them. `tools/list` on the sidecar needs no credentials, so
  `discovery_uses_secrets=False` should do.
- Add `PUT` and `DELETE /api/v1/mcp/brokerages/{name}/credentials`. `PUT` takes the key id and secret, verifies them
  once with a real read against `https://paper-api.alpaca.markets/v2/account` (reject with a clear 422 on 401/403 so a
  live key, which Alpaca refuses on the paper host, is caught here), writes both into the user vault (see
  `app/user_vault.py`, `database/user_vault_secrets.py`), and triggers discovery and a grant re-sync. Never log or
  return the secret.
- Rows become eligible for a relay grant when `enabled`, `transport = 'http'` and a URL are set
  (`egress_grants._upsert_header_grants`). Confirm that the probe verdict (`mcp_config._binding_plan`, `probe_ok`)
  turns green for the allowlisted URL, since it gates whether the direct tools bind.

### 3. Reconciliation (done)

Kept for the record; the fix is `OrderReconciler._reach` plus `active_header_grant`.

`orders/reconcile.py::_reconcile_group` calls `get_connection(user_id, server)` and
`database/order_reconciliation.py::active_grant_for_connection(user_id, connection_id)`. A header grant has no
connection, so every Alpaca attempt would be skipped and stay open forever.

Fix: when no connection exists, look up an active `header_mcp` grant by `(user_id, server_name)` in
`sandbox_egress_grants` and derive the vendor from the **grant's `destination_url`** with `brokerage_for_url` (never the
server name, which is the user's to choose). Check the rest of `order_reconciliation.py` (`list_stale_attempts` and
friends) for other `connection_id` joins. The relay accepts a header grant (`prepare_relay` skips the connection check
for it), so the read path itself already works.

## Then the frontend

Backend order and receipt rendering reads the neutral `BrokerOrder` shape, and a grep found no vendor-specific
branches in `web/src` beyond comments, so approval cards, receipts and the Orders page may need little or nothing.
Verify that rather than assume it. What is certainly needed:

- A way to enter the paper key pair and see its state (`web/src/pages/Plugins/components/McpCatalogRow.tsx`,
  `BrokerageConsentDialog.tsx`, `web/src/pages/Plugins/brokerages.ts` for the connection state,
  `web/src/pages/ChatAgent/utils/api/brokerages.ts`, `hooks/useConnectedBrokerage.ts`).
  The existing Connect action starts OAuth, which Alpaca does not have. The consent dialog's group toggles should not
  appear for a fixed-consent connector.
- Strings in `web/src/locales/en-US.json` and `zh-CN.json`, and an icon (`web/src/lib/brandArt.ts`; the registry's
  `site` is `alpaca.markets`).

## Then run it for real (nothing here has been)

The repo's own rule is to verify with real calls first and pin with tests last. Do this before writing more tests.

```bash
cp .env.example .env     # set EGRESS_RELAY_SECRET, COMPOSE_PROFILES=infra,alpaca,
                         # EGRESS_PRIVATE_ALLOWLIST=http://alpaca-mcp:8765
make up
```

Use an Alpaca **paper** account that is **not** the one `trading-agents-poc` trades; sharing it would corrupt that
project's 90-day measurement. Then, in order: enable the connector, enter keys, confirm discovery lists the tools,
place a paper order from chat and watch the approval card, check the ledger row, let a market order fill and watch
reconciliation move it, place and cancel a limit order, and force an unanswered placement (block the sidecar's egress)
to see it land as `unknown` and reconcile.

### Alpaca facts that came from documentation or memory, not a live call

Check each against a real paper account; fix `libs/alpaca-mcp` or the adapter where they differ.

- Paths and verbs: `GET /v2/account`, `/v2/positions[/{symbol}]`, `/v2/orders`, `/v2/orders/{id}`,
  `/v2/orders:by_client_order_id`, `POST/PATCH/DELETE` on orders, `DELETE /v2/positions/{symbol}` with `qty` or
  `percentage`, `/v2/clock`, `/v2/calendar`, and the data paths under `https://data.alpaca.markets` (`/v2/stocks/{sym}/bars`,
  `/quotes/latest`, `/snapshot`). A crypto symbol in a position path is sent percent-encoded (`BTC%2FUSD`); Alpaca's docs
  also use `BTCUSD`.
- A cancel answers 204 and the sidecar turns that into `{}`.
- The error body is `{"code": int, "message": str}`; some errors may carry only `message`.
- **Listing paging.** The adapter continues with `until=<created_at of the oldest row>`. If `until` is inclusive the
  boundary order is listed twice and reconciliation will see two identical candidates and call it ambiguous. If so,
  dedupe by id or step the cursor by a microsecond.
- `limit` maximum 500 on `/v2/orders`; timestamps may carry nanoseconds (the adapter's `as_datetime` handles that on
  Python 3.11+, and a test covers it).
- Notional orders must be market and `day`; `extended_hours` only on a `day` limit. The validator enforces both, so a
  wrong belief here makes it reject a valid order.
- The free data plan serves IEX only; the sidecar defaults `feed=iex`.

## Open decisions

- **Who owns `client_order_id`.** The sidecar mints it, so the host never holds one for a placement whose answer was
  lost and relies on the fingerprint instead (like IBKR). Having the host choose it before approval would make
  recovery exact, but the relay hashes the arguments at approval, so it means changing the proposal step in
  `order_governance` / `order_ledger`. Not done; worth deciding after the first real run shows how often it matters.
- **`account_ref`** is empty because the adapter cannot see an account id in any answer. Fine for one paper account per
  user; revisit if a user connects two.
- **Offering this upstream.** The sidecar and allowance are generic enough that `ginlix-ai/langalpha` may want them,
  but the allowance is security-sensitive and should be argued for on its own.

## How to run what exists

```bash
uv sync --frozen
uv run pytest tests/unit -q --ignore=tests/unit/plugins/skills          # openpyxl is not installed here
uv run pytest tests/unit/server/services/brokerage_orders tests/unit/server/utils -q
cd libs/alpaca-mcp && PYTHONPATH=. uv run --with pytest --with pytest-asyncio pytest -c pyproject.toml --rootdir . tests -q
```

`tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_root_lays_the_links_as_the_folders_owner` fails when the
suite runs as root (it did in the authoring container) and also fails on the untouched base. It is not this work.

To test a migration for real without Docker: Postgres 16 server binaries were present, but the server refuses to run as
root, so run it as another user, and initialise it with `-E UTF8 --locale=C.UTF-8` (the default SQL_ASCII encoding makes
psycopg return bytes and breaks alembic). Then `alembic upgrade head` with `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`
and `DB_SSLMODE=disable`.

## Conventions that bit

- `asyncio_mode = "strict"`: every async test needs `@pytest.mark.asyncio`.
- Tests that stub `pin_public_url` must accept `**kwargs` now (it takes `allow_operator_private`).
- `set(_CURATION) == brokerage_names()` is enforced, so a curated vendor must be in `BROKERAGES`. That is why the
  entry is always present but hidden unless the operator allows its address.
- `AGENTS.md`: docstrings explain why, not what; do not reword the pinned agent-facing docstrings; third-party MCP
  servers launch isolated; the backend is multi-worker (no module-level state a request path consults).

---

## Prompt for the next session

Paste this into a new session on `oofangoo/langalpha` (it needs push access to that repo, on branch `alpaca-paper`).

```text
You are continuing work on the Alpaca paper-trading connector in oofangoo/langalpha (a fork of ginlix-ai/langalpha).
Work on branch `alpaca-paper`; fetch it first. Do not open a pull request unless I ask.

Start by reading, in this order: AGENTS.md, docs/design/alpaca-paper-connector.md, docs/design/alpaca-paper-handoff.md.
They state the goal, what is built and verified, what is not, and why. Do not re-litigate the design: the sidecar
approach and the narrow EGRESS_PRIVATE_ALLOWLIST are settled. Do not widen that allowance (no paths, wildcards, CIDRs,
no use from OAuth hops). The connector is paper-only and must stay unable to reach a live Alpaca account.

The backend is built and unit-tested: the sidecar (keys can come from .env), the adapter, fixed consent for the
operator-hosted connector, and reconciliation through a header grant. What has NEVER been done is run it. So:
  1. Bring up the stack (see "Then run it for real" in the handoff) with my Alpaca PAPER keys in .env, enable the
     connector, and walk the real flow: discovery lists the tools, an order from chat shows the approval card, the
     ledger row appears, a market order fills and reconciliation moves it, a limit order places and cancels, and an
     unanswered placement lands as `unknown` and reconciles. Fix what breaks. This is the main job and it will find
     things the unit tests could not.
  2. Check each item under "Alpaca facts that came from documentation or memory" against the real account and fix
     libs/alpaca-mcp or the adapter where they differ. The listing-paging cursor is the one most likely to matter.
  3. Then the frontend: the Alpaca tile, a state for a connector with no OAuth, strings (en-US, zh-CN) and the icon.
  4. Only if I say it is a shared install: per-user keys via a credentials endpoint (handoff section 2).
If this session has no Docker daemon, say so plainly: you can do the frontend and read-only review, but step 1 is still
owed and must not be reported as done.

Rules: follow AGENTS.md (verify with real calls first, pin with tests last; docstrings explain why). Use an Alpaca
paper account that is not the one the trading-agents-poc project trades. Run the unit suite and the sidecar tests before
every push, and report any failure with its output; the root-uid livefs test failure is known and unrelated. Commit in
small logical commits and push to `alpaca-paper` only. Update the State table in the handoff as things become true.

Tell me at the end: what you ran, what broke, what you fixed, and what you could not verify.
```
