from __future__ import annotations

import pytest
from alpaca_mcp import validation as v


def stock(**kw):
    base = {"symbol": "aapl", "side": "buy", "qty": 1}
    base.update(kw)
    return v.build_stock_order(**base)


def test_market_order_defaults_and_minted_client_id():
    body = stock()
    assert body["symbol"] == "AAPL"
    assert body["type"] == "market"
    assert body["time_in_force"] == "day"
    assert body["qty"] == "1"
    assert body["client_order_id"].startswith(v.CLIENT_ORDER_PREFIX)
    assert stock()["client_order_id"] != body["client_order_id"]


def test_caller_client_id_is_kept():
    assert stock(client_order_id_value="mine-1")["client_order_id"] == "mine-1"


def test_client_id_length_is_bounded():
    with pytest.raises(v.InvalidOrder, match="longer than"):
        stock(client_order_id_value="x" * 129)


@pytest.mark.parametrize("qty,notional", [(None, None), (1, 100)])
def test_exactly_one_size(qty, notional):
    with pytest.raises(v.InvalidOrder, match="exactly one"):
        stock(qty=qty, notional=notional)


def test_notional_must_be_a_day_market_order():
    assert stock(qty=None, notional=250)["notional"] == "250"
    with pytest.raises(v.InvalidOrder, match="notional"):
        stock(qty=None, notional=250, order_type="limit", limit_price=10)
    with pytest.raises(v.InvalidOrder, match="notional"):
        stock(qty=None, notional=250, time_in_force="gtc")


def test_prices_follow_the_order_type():
    with pytest.raises(v.InvalidOrder, match="needs limit_price"):
        stock(order_type="limit")
    with pytest.raises(v.InvalidOrder, match="needs stop_price"):
        stock(order_type="stop")
    with pytest.raises(v.InvalidOrder, match="limit_price does not apply"):
        stock(limit_price=10)
    with pytest.raises(v.InvalidOrder, match="stop_price does not apply"):
        stock(order_type="limit", limit_price=10, stop_price=9)
    body = stock(order_type="stop_limit", limit_price="10.50", stop_price=10)
    assert (body["limit_price"], body["stop_price"]) == ("10.5", "10")


def test_trailing_stop_needs_exactly_one_trail():
    assert stock(order_type="trailing_stop", trail_percent=10)["trail_percent"] == "10"
    with pytest.raises(v.InvalidOrder, match="exactly one"):
        stock(order_type="trailing_stop")
    with pytest.raises(v.InvalidOrder, match="exactly one"):
        stock(order_type="trailing_stop", trail_price=1, trail_percent=1)
    with pytest.raises(v.InvalidOrder, match="trail_percent does not apply"):
        stock(trail_percent=5)


def test_extended_hours_only_on_day_limit():
    assert stock(order_type="limit", limit_price=10, extended_hours=True)["extended_hours"]
    with pytest.raises(v.InvalidOrder, match="extended_hours"):
        stock(extended_hours=True)


def test_bracket_and_oto_classes():
    both = stock(take_profit_limit_price=120, stop_loss_stop_price=90, stop_loss_limit_price=89)
    assert both["order_class"] == "bracket"
    assert both["take_profit"] == {"limit_price": "120"}
    assert both["stop_loss"] == {"stop_price": "90", "limit_price": "89"}
    assert stock(stop_loss_stop_price=90)["order_class"] == "oto"
    with pytest.raises(v.InvalidOrder, match="stop_loss_stop_price"):
        stock(stop_loss_limit_price=89)
    with pytest.raises(v.InvalidOrder, match="market or limit"):
        stock(order_type="stop", stop_price=10, stop_loss_stop_price=9)
    with pytest.raises(v.InvalidOrder, match="qty, not notional"):
        stock(qty=None, notional=100, stop_loss_stop_price=9)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "abc"])
def test_numbers_must_be_positive_and_finite(bad):
    with pytest.raises(v.InvalidOrder):
        stock(qty=bad)


@pytest.mark.parametrize("symbol", ["", "BTC/USD", "A B", "../x", "AAPL?x=1"])
def test_stock_symbol_rejects_anything_that_could_reshape_a_path(symbol):
    with pytest.raises(v.InvalidOrder):
        v.stock_symbol(symbol)


def test_share_class_symbols_are_allowed():
    assert v.stock_symbol("brk.b") == "BRK.B"
    assert v.stock_symbol("BRK-B") == "BRK-B"


def test_crypto_order():
    body = v.build_crypto_order(symbol="btc/usd", side="buy", notional=100)
    assert body["symbol"] == "BTC/USD"
    assert body["time_in_force"] == "gtc"
    assert body["notional"] == "100"
    with pytest.raises(v.InvalidOrder, match="pair"):
        v.build_crypto_order(symbol="BTCUSD", side="buy", qty=1)
    with pytest.raises(v.InvalidOrder, match="time_in_force"):
        v.build_crypto_order(symbol="BTC/USD", side="buy", qty=1, time_in_force="day")
    with pytest.raises(v.InvalidOrder, match="notional"):
        v.build_crypto_order(
            symbol="BTC/USD", side="buy", notional=10, order_type="limit", limit_price=1
        )


def test_order_id_must_be_a_uuid():
    good = "61e69015-8549-4bfd-b9c3-01e75843f47d"
    assert v.order_id(good) == good
    with pytest.raises(v.InvalidOrder):
        v.order_id("../positions")


def test_replace_needs_a_change():
    assert v.build_replace(qty=2, limit_price=10.5) == {"qty": "2", "limit_price": "10.5"}
    with pytest.raises(v.InvalidOrder, match="nothing to change"):
        v.build_replace()


def test_close_params():
    assert v.close_params(None, None) == {}
    assert v.close_params(3, None) == {"qty": "3"}
    assert v.close_params(None, 50) == {"percentage": "50"}
    with pytest.raises(v.InvalidOrder, match="not both"):
        v.close_params(1, 50)
    with pytest.raises(v.InvalidOrder, match="exceed 100"):
        v.close_params(None, 101)
