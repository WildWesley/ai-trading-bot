"""The daily trend filter blocks BUYs on downtrending stocks, not crypto."""

from bot.trader import Trader
from tests.conftest import falling_closes, make_bars, rising_closes


def test_buy_skipped_when_stock_trend_down(fake_client, fake_db, monkeypatch):
    fake_client.bars_daily["AAPL"] = make_bars(falling_closes(260))
    trader = Trader(fake_client, fake_db, advisor=None, watchlist=["AAPL"])
    trader._stock_trading_allowed = True
    trader._warm_trend_cache_if_needed()
    assert trader._trend_cache["AAPL"] == "down"

    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    trader._process_symbol("AAPL")
    assert fake_db.trades == []  # BUY was blocked by the down trend


def test_buy_allowed_when_stock_trend_up(fake_client, fake_db, monkeypatch):
    fake_client.bars_daily["AAPL"] = make_bars(rising_closes(260))
    trader = Trader(fake_client, fake_db, advisor=None, watchlist=["AAPL"])
    trader._stock_trading_allowed = True
    trader._warm_trend_cache_if_needed()
    assert trader._trend_cache["AAPL"] == "up"

    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    trader._process_symbol("AAPL")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["side"] == "buy"


def test_crypto_buy_ignores_trend_filter(fake_client, fake_db, monkeypatch):
    trader = Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])
    trader._stock_trading_allowed = False  # market closed; crypto still trades
    trader._warm_trend_cache_if_needed()

    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    trader._process_symbol("BTC/USD")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["symbol"] == "BTC/USD"
