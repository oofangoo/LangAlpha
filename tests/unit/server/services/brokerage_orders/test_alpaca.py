"""The Alpaca adapter against the shapes the sidecar passes through.

The order object is Alpaca's own (field names as its REST docs give them, ids
invented). The envelopes around it are ours and are a contract with
``libs/alpaca-mcp``: a list is ``{"orders": [...], "count": n}``, a cancel
answers ``{}``, and an error in Alpaca's words is a tool error carrying
``{"message", "http_status"}`` plus a numeric ``code`` when Alpaca sent one.
``test_alpaca_sidecar_contract`` holds the other half, by feeding the adapter
what the real server emits.

The arms worth locking are the ones a guess would have got wrong. A timeout on a
placement is ``unknown`` and never ``failed``, because a failed attempt reads as
leave to place the order again and the order may well exist. A refusal made
before anything was sent is terminal, because then it provably does not. And a
dollar-sized order lists without a quantity, so it still has to be recognised.
"""

import json
from decimal import Decimal

import pytest

from src.server.services.brokerage_capabilities import order_tool
from src.server.services.brokerage_orders import (
    AttemptStatus,
    BrokerOrder,
    CryptoRef,
    EquityRef,
    ListingIncomplete,
    OrderMode,
    StatusReadError,
    adapter_for,
)
from src.server.services.brokerage_orders.alpaca import (
    GET_ORDER,
    LIST_ORDERS,
    PAGE_SIZE,
    AlpacaOrderAdapter,
)

ALPACA = adapter_for("alpaca")

ORDER_ID = "61e69015-8549-4bfd-b9c3-01e75843f47d"

# What Alpaca answers a market order with, near enough: received, not yet routed.
ACCEPTED = {
    "id": ORDER_ID,
    "client_order_id": "lamcp-0f3c",
    "created_at": "2026-10-08T14:30:01.942282Z",
    "updated_at": "2026-10-08T14:30:01.942282Z",
    "submitted_at": "2026-10-08T14:30:01.937734Z",
    "filled_at": None,
    "asset_class": "us_equity",
    "notional": None,
    "qty": "1",
    "filled_qty": "0",
    "filled_avg_price": None,
    "order_class": "",
    "order_type": "market",
    "type": "market",
    "side": "buy",
    "time_in_force": "day",
    "limit_price": None,
    "stop_price": None,
    "status": "accepted",
    "extended_hours": False,
    "legs": None,
    "symbol": "AAPL",
}

FILLED = {
    **ACCEPTED,
    "status": "filled",
    "filled_qty": "1",
    "filled_avg_price": "187.52",
    "filled_at": "2026-10-08T14:30:02.100000000Z",
    "updated_at": "2026-10-08T14:30:02.100000000Z",
}

REJECTION_BODY = {
    "message": "insufficient buying power",
    "http_status": 403,
    "code": 40310000,
}


def blocks(payload) -> str:
    return json.dumps(payload)


def stock_order(**overrides) -> BrokerOrder:
    args = {"symbol": "aapl", "side": "buy", "qty": 1, **overrides}
    return ALPACA.parse_request("place_stock_order", args)


# ----- the registry --------------------------------------------------------


def test_the_adapter_is_registered_under_the_vendor_key():
    assert isinstance(adapter_for("alpaca"), AlpacaOrderAdapter)
    assert adapter_for(" ALPACA ") is not None


@pytest.mark.parametrize(
    "tool",
    [
        "place_stock_order",
        "place_crypto_order",
        "close_position",
        "replace_order_by_id",
        "cancel_order_by_id",
    ],
)
def test_every_mutating_tool_is_an_order_tool_in_paper_mode(tool):
    entry = order_tool("alpaca", tool)
    assert entry is not None and entry.mode is OrderMode.PAPER


@pytest.mark.parametrize(
    "tool",
    ["get_orders", "get_order_by_id", "get_account_info", "get_clock", "get_stock_bars"],
)
def test_reads_are_not_order_tools(tool):
    assert order_tool("alpaca", tool) is None
    assert ALPACA.parse_request(tool, {}) is None


# ----- the request ---------------------------------------------------------


def test_a_market_order_that_leaned_on_the_defaults_still_says_what_it_is():
    order = stock_order()
    assert order.mode is OrderMode.PAPER
    assert order.asset_class == "equity"
    assert order.instrument == EquityRef(symbol="AAPL")
    assert order.side == "buy"
    assert order.qty == Decimal(1)
    assert order.order_type == "market"
    assert order.time_in_force == "day"
    assert order.account_ref == ""


def test_a_limit_order_keeps_its_prices():
    order = stock_order(type="limit", limit_price=10.5, time_in_force="gtc", qty=3)
    assert (order.order_type, order.limit_price, order.time_in_force) == (
        "limit",
        Decimal("10.5"),
        "gtc",
    )
    assert stock_order(type="stop", stop_price=9).stop_price == Decimal(9)


def test_a_dollar_order_carries_its_notional_and_no_quantity():
    order = stock_order(qty=None, notional=250)
    assert order.qty is None
    assert order.notional is not None
    assert order.notional.amount == Decimal(250)
    assert order.notional.currency == "USD"


def test_words_the_vocabulary_lacks_survive_in_extras():
    order = stock_order(type="trailing_stop", trail_percent=10, time_in_force="ioc")
    assert order.order_type is None and order.time_in_force is None
    assert order.extras["type"] == "trailing_stop"
    assert order.extras["time_in_force"] == "ioc"
    assert order.extras["trail_percent"] == 10


def test_bracket_legs_and_the_client_id_are_kept_not_dropped():
    order = stock_order(
        take_profit_limit_price=120, stop_loss_stop_price=90, client_order_id="mine-1"
    )
    assert order.extras["take_profit_limit_price"] == 120
    assert order.extras["stop_loss_stop_price"] == 90
    assert order.extras["client_order_id"] == "mine-1"


def test_extended_hours_is_a_session():
    assert stock_order(type="limit", limit_price=1, extended_hours=True).session == (
        "rth_plus_ext"
    )
    assert stock_order().session is None


def test_a_crypto_order_defaults_to_gtc_and_names_the_pair():
    order = ALPACA.parse_request(
        "place_crypto_order", {"symbol": "btc/usd", "side": "sell", "qty": 0.5}
    )
    assert order.asset_class == "crypto"
    assert order.instrument == CryptoRef(pair="BTC/USD")
    assert order.time_in_force == "gtc"
    assert order.order_type == "market"


def test_replace_and_cancel_name_the_order_they_act_on():
    replace = ALPACA.parse_request(
        "replace_order_by_id", {"order_id": ORDER_ID, "limit_price": 11, "qty": 2}
    )
    assert replace.target_ref == ORDER_ID
    assert (replace.limit_price, replace.qty) == (Decimal(11), Decimal(2))
    cancel = ALPACA.parse_request("cancel_order_by_id", {"order_id": ORDER_ID})
    assert cancel.target_ref == ORDER_ID
    assert cancel.mode is OrderMode.PAPER


def test_closing_a_position_claims_no_side_it_was_not_told():
    equity = ALPACA.parse_request("close_position", {"symbol": "AAPL", "qty": 5})
    assert equity.asset_class == "equity" and equity.side is None
    assert equity.instrument == EquityRef(symbol="AAPL")
    assert equity.qty == Decimal(5)
    crypto = ALPACA.parse_request("close_position", {"symbol": "BTC/USD", "percentage": 50})
    assert crypto.asset_class == "crypto"
    assert crypto.extras["percentage"] == 50


# ----- the answer ----------------------------------------------------------


def test_an_accepted_order_is_submitted_not_yet_working():
    outcome = ALPACA.parse_result(
        "place_stock_order", blocks(ACCEPTED), tool_status="success", request=stock_order()
    )
    assert outcome.status is AttemptStatus.SUBMITTED
    assert outcome.vendor_order_id == ORDER_ID
    assert outcome.raw_status == "accepted"
    assert outcome.filled_qty == Decimal(0)
    assert outcome.avg_fill_price is None
    assert outcome.created_at is not None


def test_a_filled_answer_carries_the_fill_and_survives_nanosecond_stamps():
    outcome = ALPACA.parse_result("place_stock_order", blocks(FILLED), tool_status="success")
    assert outcome.status is AttemptStatus.FILLED
    assert outcome.filled_qty == Decimal(1)
    assert outcome.avg_fill_price == Decimal("187.52")
    assert outcome.updated_at is not None


@pytest.mark.parametrize(
    "word,expected",
    [
        ("pending_new", AttemptStatus.SUBMITTED),
        ("accepted", AttemptStatus.SUBMITTED),
        ("new", AttemptStatus.WORKING),
        ("accepted_for_bidding", AttemptStatus.WORKING),
        ("held", AttemptStatus.WORKING),
        ("pending_replace", AttemptStatus.WORKING),
        ("pending_cancel", AttemptStatus.WORKING),
        ("stopped", AttemptStatus.WORKING),
        ("done_for_day", AttemptStatus.WORKING),
        ("partially_filled", AttemptStatus.PARTIALLY_FILLED),
        ("filled", AttemptStatus.FILLED),
        ("canceled", AttemptStatus.CANCELLED),
        ("expired", AttemptStatus.CANCELLED),
        ("replaced", AttemptStatus.CANCELLED),
        ("rejected", AttemptStatus.REJECTED_BY_VENDOR),
        ("suspended", AttemptStatus.UNKNOWN),
        ("calculated", AttemptStatus.UNKNOWN),
        ("a_word_from_the_future", AttemptStatus.UNKNOWN),
    ],
)
def test_every_status_word_has_a_lifecycle_state(word, expected):
    outcome = ALPACA.parse_result(
        "place_stock_order", blocks({**ACCEPTED, "status": word}), tool_status="success"
    )
    assert outcome.status is expected
    assert outcome.raw_status == word


def test_a_rejected_order_says_it_was():
    outcome = ALPACA.parse_result(
        "place_stock_order", blocks({**ACCEPTED, "status": "rejected"}), tool_status="success"
    )
    assert outcome.failure is not None and outcome.failure.kind == "vendor"


def test_a_cancel_answers_with_an_empty_body_and_that_is_the_confirmation():
    request = ALPACA.parse_request("cancel_order_by_id", {"order_id": ORDER_ID})
    outcome = ALPACA.parse_result(
        "cancel_order_by_id", "{}", tool_status="success", request=request
    )
    assert outcome.status is AttemptStatus.CANCELLED
    assert outcome.vendor_order_id == ORDER_ID


def test_a_replace_answers_with_the_new_order():
    new = {**ACCEPTED, "id": "8f1d4b57-0c07-4a49-9d64-6b0f6b9d3c11", "status": "new"}
    outcome = ALPACA.parse_result("replace_order_by_id", blocks(new), tool_status="success")
    assert outcome.status is AttemptStatus.WORKING
    assert outcome.vendor_order_id == new["id"]


def test_an_answer_that_names_no_order_settles_nothing():
    outcome = ALPACA.parse_result("place_stock_order", "{}", tool_status="success")
    assert outcome.status is AttemptStatus.UNKNOWN
    prose = ALPACA.parse_result("place_stock_order", "ok then", tool_status="success")
    assert prose.status is AttemptStatus.UNKNOWN


def test_alpacas_refusal_on_the_error_channel_is_the_vendors_word():
    outcome = ALPACA.parse_result(
        "place_stock_order", blocks(REJECTION_BODY), tool_status="error"
    )
    assert outcome.status is AttemptStatus.REJECTED_BY_VENDOR
    assert outcome.failure.kind == "vendor"
    assert outcome.failure.code == "40310000"
    assert outcome.failure.message == "insufficient buying power"


def test_a_refusal_with_no_alpaca_code_falls_back_to_the_http_status():
    outcome = ALPACA.parse_result(
        "place_stock_order",
        blocks({"message": "forbidden", "http_status": 403}),
        tool_status="error",
    )
    assert outcome.status is AttemptStatus.REJECTED_BY_VENDOR
    assert outcome.failure.code == "403"


@pytest.mark.parametrize(
    "text",
    [
        "refused before sending: give exactly one of qty (shares or units) and notional (dollars)",
        "no Alpaca paper credentials were sent: the connection must carry APCA-API-KEY-ID",
    ],
)
def test_a_call_that_never_left_the_sidecar_is_terminally_refused(text):
    outcome = ALPACA.parse_result("place_stock_order", text, tool_status="error")
    assert outcome.status is AttemptStatus.REJECTED_BY_VENDOR
    assert outcome.failure.code == "not_sent"


def test_a_timeout_on_a_placement_is_unknown_never_failed():
    text = (
        "Alpaca did not answer in time. The order may or may not have been placed; look "
        "it up with get_order_by_client_id using client_order_id=lamcp-0f3c"
    )
    outcome = ALPACA.parse_result("place_stock_order", text, tool_status="error")
    assert outcome.status is AttemptStatus.UNKNOWN
    assert outcome.status in {AttemptStatus.UNKNOWN}  # open, so reconciliation asks
    assert outcome.failure.kind == "transport"


def test_any_other_tool_error_is_still_a_transport_failure():
    outcome = ALPACA.parse_result(
        "place_stock_order", "could not reach Alpaca (ConnectError)", tool_status="error"
    )
    assert outcome.status is AttemptStatus.FAILED
    assert outcome.failure.kind == "transport"


def test_a_policy_refusal_is_not_read_as_a_vendor_word():
    outcome = ALPACA.parse_result(
        "place_stock_order", "Refused: not allowed", tool_status="success"
    )
    assert outcome.status is AttemptStatus.REFUSED


# ----- reading an order back ----------------------------------------------


def test_status_reads_are_reads_and_pick_the_cheapest_that_can_answer():
    by_id = ALPACA.status_query(None, vendor_order_id=ORDER_ID)
    assert (by_id.tool, by_id.args) == (GET_ORDER, {"order_id": ORDER_ID})
    listing = ALPACA.status_query(stock_order())
    assert listing.tool == LIST_ORDERS
    assert listing.args["status"] == "all" and listing.args["limit"] == PAGE_SIZE
    # Reconciliation refuses to call an order tool as a status read.
    assert order_tool("alpaca", by_id.tool) is None
    assert order_tool("alpaca", listing.tool) is None


def test_a_single_order_read_returns_that_order():
    query = ALPACA.status_query(None, vendor_order_id=ORDER_ID)
    (listed,) = ALPACA.parse_status(query, blocks(FILLED))
    assert listed.vendor_order_id == ORDER_ID
    assert listed.outcome.status is AttemptStatus.FILLED
    assert listed.placed_at is not None


def test_alpaca_having_no_such_order_is_a_fact_not_a_failed_read():
    query = ALPACA.status_query(None, vendor_order_id=ORDER_ID)
    missing = blocks({"message": "order not found", "http_status": 404, "code": 40410000})
    assert ALPACA.parse_status(query, missing) == []


@pytest.mark.parametrize("status", [401, 429, 500])
def test_any_other_failed_read_is_not_evidence_of_absence(status):
    query = ALPACA.status_query(None, vendor_order_id=ORDER_ID)
    with pytest.raises(StatusReadError):
        ALPACA.parse_status(query, blocks({"message": "nope", "http_status": status}))


def test_a_listing_is_read_row_by_row():
    query = ALPACA.status_query(stock_order())
    body = blocks({"orders": [FILLED, ACCEPTED], "count": 2})
    listed = ALPACA.parse_status(query, body)
    assert [o.vendor_order_id for o in listed] == [ORDER_ID, ORDER_ID]
    assert listed[0].outcome.status is AttemptStatus.FILLED
    assert listed[0].match_key is not None


@pytest.mark.parametrize(
    "body",
    ["not json", "{}", blocks({"orders": "x"}), blocks({"orders": [{"symbol": "AAPL"}]})],
)
def test_an_unreadable_listing_is_an_error_not_an_empty_book(body):
    with pytest.raises(StatusReadError):
        ALPACA.parse_status(ALPACA.status_query(stock_order()), body)


# ----- recognising an order whose answer was lost --------------------------


def _key_of_listed(row) -> object:
    query = ALPACA.status_query(None)
    (listed,) = ALPACA.parse_status(query, blocks({"orders": [row], "count": 1}))
    return listed.match_key


def test_a_placement_matches_the_row_alpaca_lists_for_it():
    sent = ALPACA.match_key(stock_order())
    assert sent is not None
    assert sent.matches(_key_of_listed(ACCEPTED))


def test_a_different_side_or_size_or_symbol_does_not_match():
    sent = ALPACA.match_key(stock_order())
    assert not sent.matches(_key_of_listed({**ACCEPTED, "side": "sell"}))
    assert not sent.matches(_key_of_listed({**ACCEPTED, "qty": "2"}))
    assert not sent.matches(_key_of_listed({**ACCEPTED, "symbol": "MSFT"}))


def test_a_limit_price_is_compared_only_where_both_sides_name_it():
    sent = ALPACA.match_key(stock_order(type="limit", limit_price=10.50))
    assert sent.matches(_key_of_listed({**ACCEPTED, "type": "limit", "limit_price": "10.5"}))
    assert not sent.matches(_key_of_listed({**ACCEPTED, "type": "limit", "limit_price": "11"}))


def test_a_dollar_order_is_recognised_though_it_lists_with_no_quantity():
    sent = ALPACA.match_key(stock_order(qty=None, notional=250))
    row = {**ACCEPTED, "qty": None, "notional": "250"}
    assert sent is not None and sent.matches(_key_of_listed(row))
    assert not sent.matches(_key_of_listed({**row, "notional": "300"}))


def test_a_crypto_pair_matches_across_spellings_of_case():
    order = ALPACA.parse_request(
        "place_crypto_order", {"symbol": "btc/usd", "side": "buy", "qty": 0.5}
    )
    row = {**ACCEPTED, "symbol": "BTC/USD", "qty": "0.5", "asset_class": "crypto",
           "time_in_force": "gtc"}
    assert ALPACA.match_key(order).matches(_key_of_listed(row))


def test_what_names_its_own_order_or_too_little_has_no_fingerprint():
    assert ALPACA.match_key(None) is None
    assert ALPACA.match_key(ALPACA.parse_request("cancel_order_by_id", {"order_id": ORDER_ID})) is None
    assert ALPACA.match_key(
        ALPACA.parse_request("replace_order_by_id", {"order_id": ORDER_ID, "qty": 2})
    ) is None
    assert ALPACA.match_key(ALPACA.parse_request("close_position", {"symbol": "AAPL"})) is None
    # No size at all.
    assert ALPACA.match_key(
        ALPACA.parse_request("place_stock_order", {"symbol": "AAPL", "side": "buy"})
    ) is None


# ----- paging --------------------------------------------------------------


def _page(n: int, *, start: int = 0) -> str:
    rows = [
        {**ACCEPTED, "id": f"{i:08d}-0000-0000-0000-000000000000",
         "created_at": f"2026-10-08T14:{59 - (i % 60):02d}:00Z"}
        for i in range(start, start + n)
    ]
    return blocks({"orders": rows, "count": n})


def test_a_short_page_is_the_whole_list():
    query = ALPACA.status_query(None)
    assert ALPACA.next_page(query, _page(3)) is None


def test_a_full_page_continues_from_where_its_oldest_row_began():
    query = ALPACA.status_query(None)
    body = _page(PAGE_SIZE)
    nxt = ALPACA.next_page(query, body)
    assert nxt is not None and nxt.tool == LIST_ORDERS
    assert nxt.args["until"] == json.loads(body)["orders"][-1]["created_at"]
    assert nxt.args["status"] == "all"


def test_a_full_page_that_names_no_time_to_continue_from_is_incomplete():
    query = ALPACA.status_query(None)
    rows = [{**ACCEPTED, "created_at": None} for _ in range(PAGE_SIZE)]
    with pytest.raises(ListingIncomplete):
        ALPACA.next_page(query, blocks({"orders": rows}))


def test_a_full_page_that_repeats_the_cursor_is_incomplete_not_endless():
    query = ALPACA.status_query(None)
    body = _page(PAGE_SIZE)
    oldest = json.loads(body)["orders"][-1]["created_at"]
    repeated = type(query)(query.tool, {**query.args, "until": oldest})
    with pytest.raises(ListingIncomplete):
        ALPACA.next_page(repeated, body)


def test_a_single_order_read_never_pages():
    assert ALPACA.next_page(ALPACA.status_query(None, vendor_order_id=ORDER_ID), blocks(FILLED)) is None
