"""Crypto-regime filter: pause long crypto buys when the crypto proxy (BTC) is
either in a daily downtrend (EMA20/50) or "red" intraday (trading below its
prior daily UTC close). The SPY analog for the crypto book.

Two independent, individually-toggleable gates. The trend gate reads
``Trader._trend_cache`` (populated for the proxy in the warm-up); the intraday
gate reads ``Trader._crypto_regime_down``, recomputed each cycle by
``_update_crypto_regime``. Both fail open. Unlike the stock regime, the intraday
check runs even when the stock market is closed — crypto trades 24/7.
"""

import pytest

import bot.config as config
from bot.trader import Trader
from tests.conftest import make_bars


@pytest.fixture(autouse=True)
def _enable_crypto_gates(monkeypatch):
    """Exercise the crypto-regime gate LOGIC. The gates ship disabled by
    default (2026-06-17 — backtested as harmful in the current BTC downtrend),
    so enable them for these tests; the toggle-off tests re-disable explicitly.
    """
    monkeypatch.setattr(config, "CRYPTO_REGIME_USE_TREND", True)
    monkeypatch.setattr(config, "CRYPTO_REGIME_USE_INTRADAY", True)


def _trader(
    fake_client,
    fake_db,
    watchlist=("BTC/USD",),
    btc_trend="up",
    crypto_down=False,
):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=list(watchlist))
    t._stock_trading_allowed = True
    t._trend_cache = {s: btc_trend for s in watchlist}
    t._crypto_regime_down = crypto_down
    return t


def _force_buy(monkeypatch):
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )


# -- gate behaviour in _process_symbol --------------------------------------

def test_crypto_buy_blocked_when_proxy_trend_down(
    fake_client, fake_db, monkeypatch
):
    t = _trader(fake_client, fake_db, btc_trend="down")
    _force_buy(monkeypatch)
    t._process_symbol("BTC/USD")
    assert fake_db.trades == []


def test_crypto_buy_blocked_when_proxy_red_intraday(
    fake_client, fake_db, monkeypatch
):
    # BTC's daily trend is up, but it's red on the day -> no buy.
    t = _trader(fake_client, fake_db, btc_trend="up", crypto_down=True)
    _force_buy(monkeypatch)
    t._process_symbol("BTC/USD")
    assert fake_db.trades == []


def test_crypto_buy_allowed_when_proxy_up_and_green(
    fake_client, fake_db, monkeypatch
):
    t = _trader(fake_client, fake_db, btc_trend="up", crypto_down=False)
    _force_buy(monkeypatch)
    t._process_symbol("BTC/USD")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["side"] == "buy"


def test_crypto_buy_fails_open_when_trend_unknown(
    fake_client, fake_db, monkeypatch
):
    # No trend data and not red -> allow (fail open).
    t = _trader(fake_client, fake_db, btc_trend="unknown", crypto_down=False)
    _force_buy(monkeypatch)
    t._process_symbol("ETH/USD")
    assert len(fake_db.trades) == 1


def test_trend_gate_toggle_off_ignores_downtrend(
    fake_client, fake_db, monkeypatch
):
    monkeypatch.setattr(config, "CRYPTO_REGIME_USE_TREND", False)
    t = _trader(fake_client, fake_db, btc_trend="down", crypto_down=False)
    _force_buy(monkeypatch)
    t._process_symbol("BTC/USD")
    assert len(fake_db.trades) == 1


def test_intraday_gate_toggle_off_ignores_red_day(
    fake_client, fake_db, monkeypatch
):
    monkeypatch.setattr(config, "CRYPTO_REGIME_USE_INTRADAY", False)
    t = _trader(fake_client, fake_db, btc_trend="up", crypto_down=True)
    _force_buy(monkeypatch)
    t._process_symbol("BTC/USD")
    assert len(fake_db.trades) == 1


def test_empty_proxy_symbol_disables_both_gates(
    fake_client, fake_db, monkeypatch
):
    monkeypatch.setattr(config, "CRYPTO_REGIME_SYMBOL", "")
    t = _trader(fake_client, fake_db, btc_trend="down", crypto_down=True)
    _force_buy(monkeypatch)
    t._process_symbol("ETH/USD")
    assert len(fake_db.trades) == 1


# -- _update_crypto_regime computation (red-vs-prior-close) -----------------
# FakeClient.get_latest_price returns 100.0; we vary BTC's prior daily close.

def test_crypto_regime_down_when_proxy_below_prior_close(fake_client, fake_db):
    fake_client.bars_daily["BTC/USD"] = make_bars([110.0, 108.0, 106.0, 105.0])
    t = _trader(fake_client, fake_db)
    t._update_crypto_regime()
    assert t._crypto_regime_down is True


def test_crypto_regime_ok_when_proxy_above_prior_close(fake_client, fake_db):
    # Prior close 95 < live 100 -> green. Also clears a stale True.
    fake_client.bars_daily["BTC/USD"] = make_bars([90.0, 92.0, 94.0, 95.0])
    t = _trader(fake_client, fake_db, crypto_down=True)
    t._update_crypto_regime()
    assert t._crypto_regime_down is False


def test_crypto_regime_computes_when_stock_market_closed(fake_client, fake_db):
    # Key divergence from the stock regime: crypto is 24/7, so a closed stock
    # market must NOT stop the crypto regime from being evaluated.
    fake_client.bars_daily["BTC/USD"] = make_bars([110.0, 108.0, 106.0, 105.0])
    t = _trader(fake_client, fake_db, crypto_down=False)
    t._stock_trading_allowed = False
    t._update_crypto_regime()
    assert t._crypto_regime_down is True


def test_crypto_regime_fails_open_when_no_prior_close(fake_client, fake_db):
    fake_client.bars_daily["BTC/USD"] = make_bars([])  # empty -> no prior close
    t = _trader(fake_client, fake_db, crypto_down=True)
    t._update_crypto_regime()
    assert t._crypto_regime_down is False


def test_crypto_regime_fails_open_when_live_price_unavailable(
    fake_client, fake_db, monkeypatch
):
    fake_client.bars_daily["BTC/USD"] = make_bars([110.0, 108.0, 106.0, 105.0])
    monkeypatch.setattr(fake_client, "get_latest_price", lambda s: 0.0)
    t = _trader(fake_client, fake_db, crypto_down=True)
    t._update_crypto_regime()
    assert t._crypto_regime_down is False


# -- warm-up computes the proxy's trend (not forced "unknown") --------------

def test_warmup_computes_proxy_trend_not_unknown(fake_client, fake_db):
    from tests.conftest import rising_closes

    fake_client.bars_daily["BTC/USD"] = make_bars(rising_closes())
    t = _trader(fake_client, fake_db, watchlist=("BTC/USD", "ETH/USD"))
    t._trend_cache = {}
    t._trend_cache_date = None
    t._warm_trend_cache_if_needed()
    assert t._trend_cache["BTC/USD"] == "up"      # proxy: real verdict
    assert t._trend_cache["ETH/USD"] == "unknown"  # non-proxy crypto: bypassed
