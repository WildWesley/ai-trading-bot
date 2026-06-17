"""Crypto exit policy (2026-06-17): take-profit / stop-loss / time cap, measured
against the position's average entry price and checked every cycle, independent
of the BUY/SELL signal. Crypto does NOT use the RSI signal exit.
"""

from datetime import datetime, timedelta, timezone

import bot.config as config
from bot.trader import Trader


def _pos(broker_symbol, avg, cur, *, upl=0.0, qty=1.0):
    """An Alpaca-style crypto position (broker symbol is slashless)."""
    return {
        "symbol": broker_symbol, "qty": qty, "avg_entry_price": avg,
        "current_price": cur, "unrealized_pl": upl,
    }


def _trader(fake_client, fake_db):
    return Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])


def _last_sell(db):
    sells = [t for t in db.trades if t["side"] == "sell"]
    return sells[-1] if sells else None


def test_take_profit_fires(fake_client, fake_db):
    t = _trader(fake_client, fake_db)
    pos = _pos("BTCUSD", 100.0, 106.0, upl=6.0)   # +6% >= 5% TP
    assert t._maybe_close_crypto("BTC/USD", pos, {"price": 106.0}) is True
    # Closed via the broker (slashless) symbol; recorded under canonical symbol.
    assert "BTCUSD" in fake_client.closed
    sell = _last_sell(fake_db)
    assert sell["symbol"] == "BTC/USD"
    assert "take-profit" in sell["signal_reason"]


def test_stop_loss_fires(fake_client, fake_db):
    t = _trader(fake_client, fake_db)
    pos = _pos("BTCUSD", 100.0, 91.0, upl=-9.0)   # -9% <= -8% SL
    assert t._maybe_close_crypto("BTC/USD", pos, {"price": 91.0}) is True
    assert "stop-loss" in _last_sell(fake_db)["signal_reason"]


def test_time_cap_fires(fake_client, fake_db):
    t = _trader(fake_client, fake_db)
    # In between TP/SL, but held longer than the cap.
    t._crypto_entry_times["BTC/USD"] = datetime.now(timezone.utc) - timedelta(
        hours=config.CRYPTO_MAX_HOLD_HOURS + 1
    )
    pos = _pos("BTCUSD", 100.0, 101.0)
    assert t._maybe_close_crypto("BTC/USD", pos, {"price": 101.0}) is True
    assert "time cap" in _last_sell(fake_db)["signal_reason"]


def test_holds_when_no_leg_triggers(fake_client, fake_db):
    t = _trader(fake_client, fake_db)
    # Just opened (entry now), small gain -> nothing fires.
    t._crypto_entry_times["BTC/USD"] = datetime.now(timezone.utc)
    pos = _pos("BTCUSD", 100.0, 102.0)
    assert t._maybe_close_crypto("BTC/USD", pos, {"price": 102.0}) is False
    assert fake_client.closed == []


def test_unknown_entry_time_starts_clock_not_close(fake_client, fake_db):
    # Position predates our records: first sighting starts the clock, no close
    # (price is in-band), and the entry time is now populated.
    t = _trader(fake_client, fake_db)
    pos = _pos("BTCUSD", 100.0, 101.0)
    assert t._maybe_close_crypto("BTC/USD", pos, {"price": 101.0}) is False
    assert "BTC/USD" in t._crypto_entry_times


def test_crypto_ignores_rsi_sell_signal(fake_client, fake_db, monkeypatch):
    # A held crypto with a SELL signal but no TP/SL/time trigger must NOT close.
    fake_client.positions = {"BTCUSD": _pos("BTCUSD", 100.0, 101.0)}
    t = _trader(fake_client, fake_db)
    t._crypto_entry_times["BTC/USD"] = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "SELL", "rsi": 80, "price": 101.0, "reason": "x"},
    )
    t.run_cycle()
    assert fake_client.closed == []
    assert _last_sell(fake_db) is None


def test_eod_flatten_skips_slashless_crypto(fake_client, fake_db):
    # The EOD stock flatten must NOT liquidate crypto (reported slashless).
    fake_client.positions = {
        "BTCUSD": _pos("BTCUSD", 100.0, 101.0),
        "AAPL": {"symbol": "AAPL", "qty": 10.0, "avg_entry_price": 100.0,
                 "current_price": 101.0, "unrealized_pl": 5.0},
    }
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD", "AAPL"])
    t._flatten_stock_positions()
    assert "AAPL" in fake_client.closed
    assert "BTCUSD" not in fake_client.closed


def test_entry_time_recorded_on_crypto_buy(fake_client, fake_db, monkeypatch):
    t = _trader(fake_client, fake_db)
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    t.run_cycle()
    assert "BTC/USD" in t._crypto_entry_times
