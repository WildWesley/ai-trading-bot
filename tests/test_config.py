"""Config defaults for the trading improvements."""

import importlib

import pytest

import bot.config as config


@pytest.fixture(autouse=True)
def _restore_config_after_test():
    yield
    importlib.reload(config)  # restore real .env-backed state for other tests


@pytest.fixture
def config_defaults(monkeypatch):
    """Reload config with env overrides removed so coded DEFAULTS are seen."""
    monkeypatch.delenv("WATCHLIST", raising=False)
    monkeypatch.delenv("TRADE_INTERVAL_SECONDS", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    importlib.reload(config)
    return config


def test_new_trend_and_market_constants_exist():
    assert config.EMA_TREND_FAST_PERIOD == 20
    assert config.EMA_TREND_SLOW_PERIOD == 50
    assert config.TREND_LOOKBACK_BARS == 365
    assert config.MARKET_BLACKOUT_MINUTES == 15


def test_interval_default_is_five_minutes(config_defaults):
    assert config_defaults.TRADE_INTERVAL_SECONDS == 300


def test_watchlist_default_symbol_counts(config_defaults):
    watchlist = config_defaults.WATCHLIST
    assert len(watchlist) == 341
    assert len(watchlist) == len(set(watchlist))  # no duplicates
    stocks = [s for s in watchlist if "/" not in s]
    assert len(stocks) == 325
    crypto = [s for s in watchlist if "/" in s]
    assert len(crypto) == 16
    assert "BTC/USD" in watchlist
    assert "AAVE/USD" in watchlist
    assert "DOGE/USD" not in watchlist
    assert "MKR/USD" not in watchlist  # dropped: not on Alpaca's feed
    assert "TMUS" in watchlist
