"""Market-regime filter: no long stock buys while SPY's daily trend is down.

Gated on config.MARKET_REGIME_SYMBOL ("SPY"), read from the daily trend cache.
Crypto is exempt; an unknown/absent regime fails open (allows the buy).
"""

from bot.trader import Trader


def _trader(fake_client, fake_db, watchlist, spy_trend, stock_trend="up"):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=watchlist)
    t._stock_trading_allowed = True
    cache = {s: stock_trend for s in watchlist}
    cache["SPY"] = spy_trend
    t._trend_cache = cache
    return t


def _force_buy(monkeypatch):
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )


def test_stock_buy_blocked_when_market_regime_down(
    fake_client, fake_db, monkeypatch
):
    # AAPL's own trend is up, but the market (SPY) is down -> no buy.
    t = _trader(fake_client, fake_db, ["AAPL"], spy_trend="down", stock_trend="up")
    _force_buy(monkeypatch)
    t._process_symbol("AAPL")
    assert fake_db.trades == []


def test_stock_buy_allowed_when_market_regime_up(
    fake_client, fake_db, monkeypatch
):
    t = _trader(fake_client, fake_db, ["AAPL"], spy_trend="up", stock_trend="up")
    _force_buy(monkeypatch)
    t._process_symbol("AAPL")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["side"] == "buy"


def test_crypto_buy_ignores_market_regime(fake_client, fake_db, monkeypatch):
    # SPY down, but crypto isn't gated on the stock market.
    t = _trader(
        fake_client, fake_db, ["BTC/USD"], spy_trend="down", stock_trend="unknown"
    )
    t._stock_trading_allowed = False
    _force_buy(monkeypatch)
    t._process_symbol("BTC/USD")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["symbol"] == "BTC/USD"


def test_unknown_market_regime_does_not_block(fake_client, fake_db, monkeypatch):
    # If SPY's trend is unknown (e.g. data hiccup), don't halt all stock buys.
    t = _trader(
        fake_client, fake_db, ["AAPL"], spy_trend="unknown", stock_trend="up"
    )
    _force_buy(monkeypatch)
    t._process_symbol("AAPL")
    assert len(fake_db.trades) == 1
