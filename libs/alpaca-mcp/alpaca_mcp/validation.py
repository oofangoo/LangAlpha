"""Order arguments, checked before Alpaca sees them.

Alpaca's own rules are its to enforce, and this does not restate them all. What
it does is refuse the calls whose meaning is ambiguous or whose shape would let
one argument silently override another: both a quantity and a dollar amount, a
price on an order type that ignores it, an extended-hours flag on an order that
cannot carry one. An order the model did not mean to place is worse than one
that is refused with a reason it can act on.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

# The prefix says which system minted the id, so an order found on the account
# can be traced back here without guessing.
CLIENT_ORDER_PREFIX = "lamcp-"
_CLIENT_ORDER_ID_MAX = 128

_STOCK_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,14}$")
# A slash is how Alpaca spells a crypto pair. It is percent-encoded on its way
# into a path.
_POSITION_SYMBOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./\-]{0,19}$")

_STOCK_TYPES = frozenset({"market", "limit", "stop", "stop_limit", "trailing_stop"})
_CRYPTO_TYPES = frozenset({"market", "limit", "stop_limit"})
_STOCK_TIF = frozenset({"day", "gtc", "opg", "cls", "ioc", "fok"})
_CRYPTO_TIF = frozenset({"gtc", "ioc"})


class InvalidOrder(ValueError):
    """The call is refused before it reaches Alpaca, with a reason to act on."""


def new_client_order_id() -> str:
    return CLIENT_ORDER_PREFIX + uuid.uuid4().hex


def stock_symbol(value: str) -> str:
    symbol = (value or "").strip().upper()
    if not _STOCK_SYMBOL.match(symbol):
        raise InvalidOrder(
            f"{value!r} is not a stock symbol; crypto pairs go through "
            "place_crypto_order"
        )
    return symbol


def position_symbol(value: str) -> str:
    symbol = (value or "").strip().upper()
    if not _POSITION_SYMBOL.match(symbol):
        raise InvalidOrder(f"{value!r} is not a symbol")
    return symbol


def order_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError):
        raise InvalidOrder(f"{value!r} is not an Alpaca order id") from None


def client_order_id(value: str | None) -> str:
    if value is None or not value.strip():
        return new_client_order_id()
    text = value.strip()
    if len(text) > _CLIENT_ORDER_ID_MAX:
        raise InvalidOrder(
            f"client_order_id is longer than {_CLIENT_ORDER_ID_MAX} characters"
        )
    return text


def number(name: str, value: Any) -> str:
    """A positive finite number as the plain-digit string Alpaca takes."""
    if isinstance(value, bool):
        raise InvalidOrder(f"{name} must be a number")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise InvalidOrder(f"{name} must be a number") from None
    if not parsed.is_finite() or parsed <= 0:
        raise InvalidOrder(f"{name} must be greater than zero")
    return format(parsed.normalize(), "f")


def _optional(name: str, value: Any) -> str | None:
    return None if value is None else number(name, value)


def _size(qty: Any, notional: Any) -> dict[str, str]:
    if (qty is None) == (notional is None):
        raise InvalidOrder("give exactly one of qty (shares or units) and notional (dollars)")
    return {"qty": number("qty", qty)} if qty is not None else {
        "notional": number("notional", notional)
    }


def _forbid(order_type: str, **fields: Any) -> None:
    for name, value in fields.items():
        if value is not None:
            raise InvalidOrder(f"{name} does not apply to a {order_type} order")


def build_stock_order(
    *,
    symbol: str,
    side: str,
    qty: Any = None,
    notional: Any = None,
    order_type: str = "market",
    time_in_force: str = "day",
    limit_price: Any = None,
    stop_price: Any = None,
    trail_price: Any = None,
    trail_percent: Any = None,
    extended_hours: bool = False,
    take_profit_limit_price: Any = None,
    stop_loss_stop_price: Any = None,
    stop_loss_limit_price: Any = None,
    client_order_id_value: str | None = None,
) -> dict[str, Any]:
    if side not in ("buy", "sell"):
        raise InvalidOrder("side must be buy or sell")
    if order_type not in _STOCK_TYPES:
        raise InvalidOrder(f"order type {order_type!r} is not one of {sorted(_STOCK_TYPES)}")
    if time_in_force not in _STOCK_TIF:
        raise InvalidOrder(f"time_in_force {time_in_force!r} is not one of {sorted(_STOCK_TIF)}")

    body: dict[str, Any] = {
        "symbol": stock_symbol(symbol),
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
        **_size(qty, notional),
    }

    if notional is not None and (order_type != "market" or time_in_force != "day"):
        raise InvalidOrder("a notional (dollar) order must be a market order with time_in_force day")

    if order_type in ("limit", "stop_limit"):
        if limit_price is None:
            raise InvalidOrder(f"a {order_type} order needs limit_price")
        body["limit_price"] = number("limit_price", limit_price)
    else:
        _forbid(order_type, limit_price=limit_price)

    if order_type in ("stop", "stop_limit"):
        if stop_price is None:
            raise InvalidOrder(f"a {order_type} order needs stop_price")
        body["stop_price"] = number("stop_price", stop_price)
    else:
        _forbid(order_type, stop_price=stop_price)

    if order_type == "trailing_stop":
        if (trail_price is None) == (trail_percent is None):
            raise InvalidOrder("a trailing_stop order needs exactly one of trail_price and trail_percent")
        if trail_price is not None:
            body["trail_price"] = number("trail_price", trail_price)
        else:
            body["trail_percent"] = number("trail_percent", trail_percent)
    else:
        _forbid(order_type, trail_price=trail_price, trail_percent=trail_percent)

    if extended_hours:
        if order_type != "limit" or time_in_force != "day":
            raise InvalidOrder("extended_hours applies only to a limit order with time_in_force day")
        body["extended_hours"] = True

    take_profit = _optional("take_profit_limit_price", take_profit_limit_price)
    stop_loss = _optional("stop_loss_stop_price", stop_loss_stop_price)
    stop_loss_limit = _optional("stop_loss_limit_price", stop_loss_limit_price)
    if stop_loss_limit is not None and stop_loss is None:
        raise InvalidOrder("stop_loss_limit_price needs stop_loss_stop_price")
    if take_profit is not None or stop_loss is not None:
        if order_type not in ("market", "limit"):
            raise InvalidOrder("a bracket or one-triggers-other order must open with a market or limit order")
        if time_in_force not in ("day", "gtc"):
            raise InvalidOrder("a bracket or one-triggers-other order needs time_in_force day or gtc")
        if notional is not None:
            raise InvalidOrder("a bracket or one-triggers-other order is sized by qty, not notional")
        body["order_class"] = "bracket" if take_profit and stop_loss else "oto"
        if take_profit is not None:
            body["take_profit"] = {"limit_price": take_profit}
        if stop_loss is not None:
            leg: dict[str, str] = {"stop_price": stop_loss}
            if stop_loss_limit is not None:
                leg["limit_price"] = stop_loss_limit
            body["stop_loss"] = leg

    body["client_order_id"] = client_order_id(client_order_id_value)
    return body


def build_crypto_order(
    *,
    symbol: str,
    side: str,
    qty: Any = None,
    notional: Any = None,
    order_type: str = "market",
    time_in_force: str = "gtc",
    limit_price: Any = None,
    stop_price: Any = None,
    client_order_id_value: str | None = None,
) -> dict[str, Any]:
    if side not in ("buy", "sell"):
        raise InvalidOrder("side must be buy or sell")
    if order_type not in _CRYPTO_TYPES:
        raise InvalidOrder(f"order type {order_type!r} is not one of {sorted(_CRYPTO_TYPES)}")
    if time_in_force not in _CRYPTO_TIF:
        raise InvalidOrder(f"time_in_force {time_in_force!r} is not one of {sorted(_CRYPTO_TIF)}")
    pair = position_symbol(symbol)
    if "/" not in pair:
        raise InvalidOrder("a crypto symbol is a pair such as BTC/USD")

    body: dict[str, Any] = {
        "symbol": pair,
        "side": side,
        "type": order_type,
        "time_in_force": time_in_force,
        **_size(qty, notional),
    }
    if notional is not None and order_type != "market":
        raise InvalidOrder("a notional (dollar) order must be a market order")

    if order_type in ("limit", "stop_limit"):
        if limit_price is None:
            raise InvalidOrder(f"a {order_type} order needs limit_price")
        body["limit_price"] = number("limit_price", limit_price)
    else:
        _forbid(order_type, limit_price=limit_price)
    if order_type == "stop_limit":
        if stop_price is None:
            raise InvalidOrder("a stop_limit order needs stop_price")
        body["stop_price"] = number("stop_price", stop_price)
    else:
        _forbid(order_type, stop_price=stop_price)

    body["client_order_id"] = client_order_id(client_order_id_value)
    return body


def build_replace(
    *,
    qty: Any = None,
    time_in_force: str | None = None,
    limit_price: Any = None,
    stop_price: Any = None,
    trail: Any = None,
    client_order_id_value: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if qty is not None:
        body["qty"] = number("qty", qty)
    if time_in_force is not None:
        if time_in_force not in _STOCK_TIF | _CRYPTO_TIF:
            raise InvalidOrder(f"time_in_force {time_in_force!r} is not valid")
        body["time_in_force"] = time_in_force
    if limit_price is not None:
        body["limit_price"] = number("limit_price", limit_price)
    if stop_price is not None:
        body["stop_price"] = number("stop_price", stop_price)
    if trail is not None:
        body["trail"] = number("trail", trail)
    if client_order_id_value is not None:
        body["client_order_id"] = client_order_id(client_order_id_value)
    if not body:
        raise InvalidOrder("nothing to change: give at least one field to replace")
    return body


def close_params(qty: Any, percentage: Any) -> dict[str, str]:
    if qty is not None and percentage is not None:
        raise InvalidOrder("give qty or percentage, not both")
    if qty is not None:
        return {"qty": number("qty", qty)}
    if percentage is not None:
        value = number("percentage", percentage)
        if Decimal(value) > 100:
            raise InvalidOrder("percentage cannot exceed 100")
        return {"percentage": value}
    return {}
