"""Alpaca paper orders, as the sidecar in ``libs/alpaca-mcp`` answers them.

The server is ours, so unlike the other vendors the shapes below are a contract
rather than something read off a live call: an order is Alpaca's own order
object passed through untouched, a list is ``{"orders": [...], "count": n}``, a
cancel answers ``{}``, and an error in Alpaca's own words is a tool error whose
body is ``{"message", "http_status"}`` plus Alpaca's numeric ``code`` when it
sent one. Three consequences drive the file.

Every order is in the paper account, so every tool is ``OrderMode.PAPER`` and
there is no account argument or account in any answer: ``account_ref`` is empty
by construction. A cancel answers nothing, so the empty body is its
confirmation, as it is for the other vendors that do the same.

The sidecar mints the ``client_order_id`` of a placement, so the host never
holds one for a call whose answer was lost. The sidecar says as much in the
error it returns when Alpaca does not answer ("may or may not have been
placed"), and this adapter reads that sentence as ``unknown`` rather than as a
failure: a failed attempt is what a person takes as leave to place the order
again, and here it may well exist. Reconciliation then finds it by what was
ordered, the way it does at IBKR.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.server.services.brokerage_capabilities import OrderAction, order_tool
from src.server.services.brokerage_orders._coerce import (
    as_datetime,
    as_decimal,
    as_text,
    extras,
    norm_number,
)
from src.server.services.brokerage_orders.base import (
    ListingIncomplete,
    MatchKey,
    OrderAdapter,
    ResultBody,
    StatusQuery,
    StatusReadError,
    VendorOrder,
    body_parts,
)
from src.server.services.brokerage_orders.models import (
    AttemptStatus,
    BrokerOrder,
    CryptoRef,
    EquityRef,
    Failure,
    InstrumentRef,
    Money,
    OrderMode,
    OrderOutcome,
    OrderType,
    Side,
    TimeInForce,
)

VENDOR = "alpaca"

LIST_ORDERS = "get_orders"
GET_ORDER = "get_order_by_id"

# One page. The sidecar caps a page at 500, which is also the most Alpaca returns.
PAGE_SIZE = 500

# The sidecar's own words, from ``alpaca_mcp.server``. Both mean nothing reached
# Alpaca, so the order does not exist and the refusal is terminal.
_NOT_SENT = (
    "refused before sending:",
    "no alpaca paper credentials were sent",
)
# And these mean the opposite: the call left and no answer came back.
_UNANSWERED = "may or may not have been placed"

# Alpaca's order statuses. ``accepted`` and ``pending_new`` are the order having
# been received but not yet on the book, which is ``submitted``; the rest of the
# open ones are on it. ``calculated`` and ``suspended`` are states the venue
# does not clearly end or continue, so they stay ``unknown`` and reconciliation
# keeps asking. ``expired`` and ``replaced`` end the order without a fill and
# map to ``cancelled``, with the raw word kept beside it.
_STATUSES: dict[str, AttemptStatus] = {
    "pending_new": AttemptStatus.SUBMITTED,
    "accepted": AttemptStatus.SUBMITTED,
    "new": AttemptStatus.WORKING,
    "accepted_for_bidding": AttemptStatus.WORKING,
    "held": AttemptStatus.WORKING,
    "pending_replace": AttemptStatus.WORKING,
    "pending_cancel": AttemptStatus.WORKING,
    "stopped": AttemptStatus.WORKING,
    "done_for_day": AttemptStatus.WORKING,
    "partially_filled": AttemptStatus.PARTIALLY_FILLED,
    "filled": AttemptStatus.FILLED,
    "canceled": AttemptStatus.CANCELLED,
    "expired": AttemptStatus.CANCELLED,
    "replaced": AttemptStatus.CANCELLED,
    "rejected": AttemptStatus.REJECTED_BY_VENDOR,
}

_SIDES: dict[str, Side] = {"buy": "buy", "sell": "sell"}

# ``trailing_stop`` has no neutral word, so it stays in ``extras`` instead of
# being flattened onto a type that would describe a different order.
_ORDER_TYPES: dict[str, OrderType] = {
    "market": "market",
    "limit": "limit",
    "stop": "stop",
    "stop_limit": "stop_limit",
}

# ``ioc``, ``fok`` and ``cls`` likewise.
_TIF: dict[str, TimeInForce] = {"day": "day", "gtc": "gtc", "opg": "at_the_open"}

_STOCK_DEFAULTS = {"type": "market", "time_in_force": "day"}
_CRYPTO_DEFAULTS = {"type": "market", "time_in_force": "gtc"}


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


def _instrument(symbol: str | None) -> InstrumentRef | None:
    if not symbol:
        return None
    text = symbol.strip().upper()
    return CryptoRef(pair=text) if "/" in text else EquityRef(symbol=text)


def _fingerprint(
    symbol: Any,
    side: str | None,
    qty: Any,
    notional: Any,
    order_type: str | None,
    limit_price: Any,
    tif: str | None,
) -> MatchKey | None:
    """One order's identity when its own id is unknown.

    None unless the instrument, the side and a size are all there: those are what
    make two orders different rather than one. A dollar order lists with no
    quantity until it has filled, so the size is whichever of the two the side
    names, and a key never mixes them. The kind, the price and the duration are
    compared only where both sides name them.
    """
    code = str(symbol or "").strip().upper()
    size_qty = norm_number(qty)
    size_notional = norm_number(notional)
    if not code or not side or not (size_qty or size_notional):
        return None
    return MatchKey.of(
        symbol=code,
        side=side,
        qty=size_qty,
        notional=size_notional,
        order_type=order_type,
        limit_price=norm_number(limit_price),
        tif=tif,
    )


def _place(args: Mapping[str, Any], *, crypto: bool) -> BrokerOrder:
    defaults = _CRYPTO_DEFAULTS if crypto else _STOCK_DEFAULTS
    order_type = _lower(args.get("type") or defaults["type"])
    tif = _lower(args.get("time_in_force") or defaults["time_in_force"])
    neutral_type = _ORDER_TYPES.get(order_type)
    neutral_tif = _TIF.get(tif)
    consumed = {
        "symbol",
        "side",
        "qty",
        "notional",
        "limit_price",
        "stop_price",
        "extended_hours",
    }
    # A value the vocabulary has no word for is not consumed, so it reaches
    # ``extras`` rather than vanishing between a None field and a key nobody kept.
    if neutral_type is not None:
        consumed.add("type")
    if neutral_tif is not None:
        consumed.add("time_in_force")
    notional = as_decimal(args.get("notional"))
    symbol = as_text(args.get("symbol"))
    return BrokerOrder(
        vendor=VENDOR,
        account_ref="",
        mode=OrderMode.PAPER,
        asset_class="crypto" if crypto else "equity",
        instrument=_instrument(symbol),
        side=_SIDES.get(_lower(args.get("side"))),
        qty=as_decimal(args.get("qty")),
        notional=Money(amount=notional, currency="USD") if notional else None,
        order_type=neutral_type,
        limit_price=as_decimal(args.get("limit_price")),
        stop_price=as_decimal(args.get("stop_price")),
        time_in_force=neutral_tif,
        session="rth_plus_ext" if args.get("extended_hours") else None,
        currency="USD",
        extras={
            # The effective values, so a placement that relied on the sidecar's
            # defaults still carries what it was.
            **({} if neutral_type else {"type": order_type}),
            **({} if neutral_tif else {"time_in_force": tif}),
            **extras(args, consumed),
        },
        raw=dict(args),
    )


def _replace(args: Mapping[str, Any]) -> BrokerOrder:
    target = as_text(args.get("order_id"))
    tif = _lower(args.get("time_in_force"))
    neutral_tif = _TIF.get(tif)
    consumed = {"order_id", "qty", "limit_price", "stop_price"}
    if neutral_tif is not None or not tif:
        consumed.add("time_in_force")
    return BrokerOrder(
        vendor=VENDOR,
        account_ref="",
        mode=OrderMode.PAPER,
        target_ref=target,
        qty=as_decimal(args.get("qty")),
        limit_price=as_decimal(args.get("limit_price")),
        stop_price=as_decimal(args.get("stop_price")),
        time_in_force=neutral_tif,
        extras={
            **({"order_id": target} if target else {}),
            **extras(args, consumed),
        },
        raw=dict(args),
    )


def _cancel(args: Mapping[str, Any]) -> BrokerOrder:
    target = as_text(args.get("order_id"))
    return BrokerOrder(
        vendor=VENDOR,
        account_ref="",
        mode=OrderMode.PAPER,
        target_ref=target,
        extras={
            **({"order_id": target} if target else {}),
            **extras(args, {"order_id"}),
        },
        raw=dict(args),
    )


def _close(args: Mapping[str, Any]) -> BrokerOrder:
    """Closing a position is an order the account places for you.

    Its side is the opposite of a position this call does not name, so it is left
    unclaimed rather than guessed, and with it the fingerprint: an unanswered
    close stays unresolved instead of being paired with somebody else's order.
    """
    symbol = as_text(args.get("symbol"))
    crypto = bool(symbol and "/" in symbol)
    return BrokerOrder(
        vendor=VENDOR,
        account_ref="",
        mode=OrderMode.PAPER,
        asset_class="crypto" if crypto else "equity",
        instrument=_instrument(symbol),
        qty=as_decimal(args.get("qty")),
        order_type="market",
        currency="USD",
        extras=extras(args, {"symbol", "qty"}),
        raw=dict(args),
    )


def _rejection(
    message: str | None, *, code: str | None, status: int | None = None
) -> Failure:
    return Failure(
        kind="vendor",
        code=code if code is not None else (str(status) if status else None),
        message=message,
    )


def _vendor_failure(envelope: Mapping[str, Any] | None) -> Failure | None:
    """Alpaca's error object as the sidecar relays it, or None when it is not one.

    ``http_status`` is what marks it: an order never carries one, and neither does
    any list or account answer, so a body that has it beside a ``message`` is the
    sidecar speaking for Alpaca and nothing else.
    """
    if not envelope:
        return None
    status = envelope.get("http_status")
    message = envelope.get("message")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    if not isinstance(message, str):
        return None
    raw_code = envelope.get("code")
    code = str(raw_code) if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    return _rejection(as_text(message), code=code, status=status)


def _row_outcome(
    row: Mapping[str, Any], *, status: AttemptStatus | None = None
) -> OrderOutcome:
    """One Alpaca order object, in the vocabulary both a command and a list use.

    The fill is the same two fields on a resting order and a done one, so it is
    read for every state: a cancel after a partial keeps the shares it did get.
    """
    raw_status = row.get("status")
    settled = status or _STATUSES.get(_lower(raw_status), AttemptStatus.UNKNOWN)
    return OrderOutcome(
        status=settled,
        raw_status=as_text(raw_status),
        vendor_order_id=as_text(row.get("id")),
        filled_qty=as_decimal(row.get("filled_qty")),
        avg_fill_price=as_decimal(row.get("filled_avg_price")),
        failure=(
            _rejection("Alpaca rejected the order", code="rejected")
            if settled is AttemptStatus.REJECTED_BY_VENDOR
            else None
        ),
        created_at=as_datetime(as_text(row.get("created_at"))),
        updated_at=as_datetime(as_text(row.get("updated_at"))),
        raw=dict(row),
    )


def _listed(row: Any) -> VendorOrder:
    order_id = as_text(row.get("id")) if isinstance(row, Mapping) else None
    if order_id is None:
        raise StatusReadError("listed order with no id")
    outcome = _row_outcome(row)
    return VendorOrder(
        vendor_order_id=order_id,
        outcome=outcome,
        match_key=_fingerprint(
            row.get("symbol"),
            _SIDES.get(_lower(row.get("side"))),
            row.get("qty"),
            row.get("notional"),
            _ORDER_TYPES.get(_lower(row.get("type") or row.get("order_type"))),
            row.get("limit_price"),
            _TIF.get(_lower(row.get("time_in_force"))),
        ),
        placed_at=outcome.created_at,
    )


class AlpacaOrderAdapter(OrderAdapter):
    """The one place that knows an unanswered placement may still exist."""

    vendor = VENDOR

    def parse_result(
        self,
        tool: str,
        body: ResultBody,
        *,
        tool_status: str,
        request: BrokerOrder | None = None,
    ) -> OrderOutcome:
        # Before the shared rules, because they would file this as a failed
        # attempt, and a failed attempt is a licence to place the order again.
        if (tool_status or "").strip().casefold() == "error":
            _, text = body_parts(body)
            if _UNANSWERED in text.casefold():
                return OrderOutcome(
                    status=AttemptStatus.UNKNOWN,
                    raw_status=as_text(text),
                    failure=Failure(kind="transport", message=as_text(text)),
                    raw={"text": text},
                )
        return super().parse_result(tool, body, tool_status=tool_status, request=request)

    def parse_request(self, tool: str, args: Mapping[str, Any]) -> BrokerOrder | None:
        entry = order_tool(VENDOR, tool)
        if entry is None:
            return None
        data = dict(args or {})
        if entry.action is OrderAction.CANCEL:
            return _cancel(data)
        if entry.action is OrderAction.REPLACE:
            return _replace(data)
        if tool == "close_position":
            return _close(data)
        return _place(data, crypto=entry.asset_class == "crypto")

    def vendor_outcome(
        self,
        tool: str,
        envelope: dict[str, Any] | None,
        text: str,
        request: BrokerOrder | None,
    ) -> OrderOutcome:
        failure = _vendor_failure(envelope)
        if failure is not None:
            return OrderOutcome(
                status=AttemptStatus.REJECTED_BY_VENDOR,
                failure=failure,
                raw=dict(envelope or {}),
            )
        entry = order_tool(VENDOR, tool)
        action = entry.action if entry else None
        if envelope is None:
            return OrderOutcome(status=AttemptStatus.UNKNOWN, raw_status=as_text(text))
        if action is OrderAction.CANCEL:
            # A cancel answers ``{}``: the empty body is the confirmation, and
            # the id it removed exists only in the call that named it.
            return OrderOutcome(
                status=AttemptStatus.CANCELLED,
                vendor_order_id=(request.target_ref if request else None),
                raw=dict(envelope),
            )
        if as_text(envelope.get("id")) is None:
            # An answer that names no order is a shape we have not met, and
            # reading "submitted" out of it would settle an attempt on nothing.
            return OrderOutcome(status=AttemptStatus.UNKNOWN, raw=dict(envelope))
        # A placement or a replace is trusted only as far as submitted unless
        # Alpaca already says more: a market order answers ``accepted`` and fills
        # a moment later, and reconciliation is what carries it from there.
        return _row_outcome(envelope)

    def vendor_error(
        self, envelope: Mapping[str, Any] | None, text: str
    ) -> Failure | None:
        """Alpaca's refusal, or the sidecar's refusal to send, on the error channel."""
        failure = _vendor_failure(envelope)
        if failure is not None:
            return failure
        lowered = (text or "").strip().casefold()
        if any(lowered.startswith(prefix) for prefix in _NOT_SENT):
            return Failure(kind="vendor", code="not_sent", message=as_text(text))
        return None

    def status_query(
        self,
        order: BrokerOrder | None,
        *,
        vendor_order_id: str | None = None,
        route: Mapping[str, str] | None = None,
    ) -> StatusQuery | None:
        if vendor_order_id:
            return StatusQuery(GET_ORDER, {"order_id": vendor_order_id})
        return StatusQuery(
            LIST_ORDERS,
            {"status": "all", "limit": PAGE_SIZE, "direction": "desc"},
        )

    def parse_status(self, query: StatusQuery, body: ResultBody) -> list[VendorOrder]:
        envelope, text = body_parts(body)
        if envelope is None:
            raise StatusReadError(text or "no envelope")
        failure = _vendor_failure(envelope)
        if failure is not None:
            if query.tool == GET_ORDER and envelope.get("http_status") == 404:
                # Alpaca answered, and what it said is that it has no such
                # order. That is a fact, unlike a read that failed.
                return []
            raise StatusReadError(failure.message or "vendor error")
        if query.tool == GET_ORDER:
            return [_listed(envelope)]
        rows = envelope.get("orders")
        if not isinstance(rows, list):
            # An answer with no list is not an account with no orders, and
            # reading it that way would report an open attempt as gone.
            raise StatusReadError(text or "no order list")
        return [_listed(row) for row in rows]

    def next_page(self, query: StatusQuery, body: ResultBody) -> StatusQuery | None:
        """Alpaca pages by time: the next page ends where this one's oldest began."""
        if query.tool != LIST_ORDERS:
            return None
        envelope, _ = body_parts(body)
        rows = (envelope or {}).get("orders")
        if not isinstance(rows, list) or len(rows) < int(query.args.get("limit") or PAGE_SIZE):
            return None
        oldest = as_text(rows[-1].get("created_at")) if isinstance(rows[-1], Mapping) else None
        if not oldest or query.args.get("until") == oldest:
            raise ListingIncomplete(
                f"{query.tool} returned a full page but named no earlier time to continue from"
            )
        return StatusQuery(query.tool, {**query.args, "until": oldest})

    def match_key(self, order: BrokerOrder | None) -> MatchKey | None:
        """A placement's fingerprint, comparable with a listed order.

        A replace and a cancel carry none of these fields and answer None, which
        is honest: they name the order they act on and need no search.
        """
        if order is None or order.target_ref:
            return None
        symbol = getattr(order.instrument, "symbol", None) or getattr(
            order.instrument, "pair", None
        )
        return _fingerprint(
            symbol,
            order.side,
            order.qty,
            order.notional.amount if order.notional else None,
            order.order_type,
            order.limit_price,
            order.time_in_force,
        )
