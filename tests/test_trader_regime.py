"""Market-regime filter: pause long stock buys while the market proxy (SPY) is
"red" intraday — trading below its prior daily close.

The gate in ``_process_symbol`` reads ``Trader._market_regime_down``, recomputed
each cycle by ``_update_market_regime`` (live proxy price vs its prior settled
daily close). Crypto is exempt; missing data / a closed market fail open.
"""

from bot.trader import Trader
from tests.conftest import make_bars


def _trader(fake_client, fake_db, watchlist, market_down=False, stock_trend="up"):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=watchlist)
    t._stock_trading_allowed = True
    t._trend_cache = {s: stock_trend for s in watchlist}
    t._market_regime_down = market_down
    return t


def _force_buy(monkeypatch):
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )


# -- gate behaviour in _process_symbol --------------------------------------

def test_stock_buy_blocked_when_market_regime_down(
    fake_client, fake_db, monkeypatch
):
    # AAPL's own trend is up, but the market is red intraday -> no buy.
    t = _trader(fake_client, fake_db, ["AAPL"], market_down=True)
    _force_buy(monkeypatch)
    t._process_symbol("AAPL")
    assert fake_db.trades == []


def test_stock_buy_allowed_when_market_regime_ok(
    fake_client, fake_db, monkeypatch
):
    t = _trader(fake_client, fake_db, ["AAPL"], market_down=False)
    _force_buy(monkeypatch)
    t._process_symbol("AAPL")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["side"] == "buy"


def test_crypto_buy_ignores_market_regime(fake_client, fake_db, monkeypatch):
    # Market red, but crypto isn't gated on the stock market.
    t = _trader(
        fake_client, fake_db, ["BTC/USD"], market_down=True, stock_trend="unknown"
    )
    t._stock_trading_allowed = False
    _force_buy(monkeypatch)
    t._process_symbol("BTC/USD")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["symbol"] == "BTC/USD"


# -- _update_market_regime computation (red-vs-prior-close) -----------------
# FakeClient.get_latest_price returns 100.0; we vary SPY's prior daily close.

def test_regime_down_when_proxy_below_prior_close(fake_client, fake_db):
    # Prior close 105 > live 100 -> red -> down.
    fake_client.bars_daily["SPY"] = make_bars([110.0, 108.0, 106.0, 105.0])
    t = _trader(fake_client, fake_db, ["AAPL"])
    t._update_market_regime()
    assert t._market_regime_down is True


def test_regime_ok_when_proxy_above_prior_close(fake_client, fake_db):
    # Prior close 95 < live 100 -> green. Also clears a stale True.
    fake_client.bars_daily["SPY"] = make_bars([90.0, 92.0, 94.0, 95.0])
    t = _trader(fake_client, fake_db, ["AAPL"], market_down=True)
    t._update_market_regime()
    assert t._market_regime_down is False


def test_regime_fails_open_when_stocks_closed(fake_client, fake_db):
    # Market closed -> don't bother computing; leave trading allowed.
    fake_client.bars_daily["SPY"] = make_bars([110.0, 108.0, 106.0, 105.0])
    t = _trader(fake_client, fake_db, ["AAPL"], market_down=True)
    t._stock_trading_allowed = False
    t._update_market_regime()
    assert t._market_regime_down is False


def test_regime_fails_open_when_live_price_unavailable(
    fake_client, fake_db, monkeypatch
):
    fake_client.bars_daily["SPY"] = make_bars([110.0, 108.0, 106.0, 105.0])
    monkeypatch.setattr(fake_client, "get_latest_price", lambda s: 0.0)
    t = _trader(fake_client, fake_db, ["AAPL"], market_down=True)
    t._update_market_regime()
    assert t._market_regime_down is False
