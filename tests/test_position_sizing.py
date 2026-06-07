"""Position sizing: fractionable stocks buy by notional dollars, others whole
shares, crypto stays fractional-by-quantity."""

from bot import config
from bot.trader import Trader


def _stock_trader(fake_client, fake_db, symbol):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=[symbol])
    t._stock_trading_allowed = True
    t._trend_cache = {symbol: "up"}
    return t


def _buy_analysis(price):
    return lambda symbol, bars: {
        "signal": "BUY", "rsi": 30, "price": price, "reason": "x"
    }


def test_fractionable_stock_buys_by_notional(fake_client, fake_db, monkeypatch):
    fake_client.assets["AAPL"] = {"symbol": "AAPL", "fractionable": True}
    t = _stock_trader(fake_client, fake_db, "AAPL")
    monkeypatch.setattr("bot.algorithm.analyze", _buy_analysis(190.0))

    t._process_symbol("AAPL")

    assert len(fake_client.orders) == 1
    order = fake_client.orders[0]
    assert order["notional"] == config.MAX_POSITION_SIZE_USD
    assert not order["qty"]  # no share qty sent on a notional order
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["side"] == "buy"


def test_non_fractionable_stock_buys_whole_shares(fake_client, fake_db, monkeypatch):
    fake_client.assets["BRKA"] = {"symbol": "BRKA", "fractionable": False}
    t = _stock_trader(fake_client, fake_db, "BRKA")
    monkeypatch.setattr("bot.algorithm.analyze", _buy_analysis(200.0))

    t._process_symbol("BRKA")

    order = fake_client.orders[0]
    assert order["notional"] is None
    assert order["qty"] == int(config.MAX_POSITION_SIZE_USD // 200.0)


def test_non_fractionable_priced_above_budget_is_skipped(
    fake_client, fake_db, monkeypatch
):
    fake_client.assets["EXP"] = {"symbol": "EXP", "fractionable": False}
    t = _stock_trader(fake_client, fake_db, "EXP")
    monkeypatch.setattr("bot.algorithm.analyze", _buy_analysis(5000.0))

    t._process_symbol("EXP")

    assert fake_client.orders == []
    assert fake_db.trades == []


def test_crypto_still_buys_fractional_quantity(fake_client, fake_db, monkeypatch):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])
    t._stock_trading_allowed = False
    t._trend_cache = {"BTC/USD": "unknown"}
    monkeypatch.setattr("bot.algorithm.analyze", _buy_analysis(50000.0))

    t._process_symbol("BTC/USD")

    order = fake_client.orders[0]
    assert order["notional"] is None
    assert order["qty"] == round(config.MAX_POSITION_SIZE_USD / 50000.0, 6)


def test_fractionable_check_is_cached(fake_client, fake_db):
    calls = {"n": 0}
    base = fake_client.get_asset

    def counting(symbol):
        calls["n"] += 1
        return base(symbol)

    fake_client.get_asset = counting
    t = _stock_trader(fake_client, fake_db, "AAPL")

    assert t._is_fractionable("AAPL") is True
    assert t._is_fractionable("AAPL") is True
    assert calls["n"] == 1  # second lookup served from cache


def test_fractionable_check_defaults_to_whole_shares_on_error(
    fake_client, fake_db, monkeypatch
):
    def boom(symbol):
        raise RuntimeError("asset lookup down")

    fake_client.get_asset = boom
    t = _stock_trader(fake_client, fake_db, "AAPL")
    monkeypatch.setattr("bot.algorithm.analyze", _buy_analysis(200.0))

    t._process_symbol("AAPL")

    # Lookup failed -> conservative whole-share order, not notional.
    order = fake_client.orders[0]
    assert order["notional"] is None
    assert order["qty"] == int(config.MAX_POSITION_SIZE_USD // 200.0)
