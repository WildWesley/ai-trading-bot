"""Config defaults for the trading improvements."""

import importlib

import bot.config as config


def test_new_trend_and_market_constants_exist():
    importlib.reload(config)
    assert config.EMA_TREND_FAST_PERIOD == 50
    assert config.EMA_TREND_SLOW_PERIOD == 200
    assert config.TREND_LOOKBACK_BARS == 365
    assert config.MARKET_BLACKOUT_MINUTES == 15


def test_interval_default_is_five_minutes(monkeypatch):
    # Delete from os.environ AND prevent load_dotenv from re-populating it from
    # the .env file during reload. config.py does `from dotenv import load_dotenv`
    # which re-binds from dotenv's __init__ on reload, so patch dotenv itself.
    monkeypatch.delenv("TRADE_INTERVAL_SECONDS", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    importlib.reload(config)
    assert config.TRADE_INTERVAL_SECONDS == 300


def test_watchlist_has_100_symbols(monkeypatch):
    # Delete from os.environ AND prevent load_dotenv from re-populating it from
    # the .env file during reload. config.py does `from dotenv import load_dotenv`
    # which re-binds from dotenv's __init__ on reload, so patch dotenv itself.
    monkeypatch.delenv("WATCHLIST", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: False)
    importlib.reload(config)
    assert len(config.WATCHLIST) == 100
    crypto = [s for s in config.WATCHLIST if "/" in s]
    assert len(crypto) == 7
    assert "BTC/USD" in config.WATCHLIST
    assert "DOGE/USD" not in config.WATCHLIST
