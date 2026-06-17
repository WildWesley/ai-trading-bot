"""Crypto position-recognition fix (2026-06-17).

Alpaca reports crypto positions slashless ("BTCUSD") while the watchlist/order
path uses the canonical "BTC/USD". The trader maps slashless -> canonical when
snapshotting positions so the per-symbol lookup in _process_symbol recognizes
crypto we already hold. This stops the runaway re-buying (#1).

Crypto signal-driven SELLs remain intentionally deferred for now (#2): the fix
recognizes positions but does not yet let the RSI>65 exit fire on crypto, since
crypto's exit policy is still being designed.
"""

from bot.trader import Trader


def _buys(db):
    return [t for t in db.trades if t["side"] == "buy"]


def _sells(db):
    return [t for t in db.trades if t["side"] == "sell"]


def _force(monkeypatch, signal, rsi):
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": signal, "rsi": rsi, "price": 100.0, "reason": "x"},
    )


def test_held_crypto_recognized_blocks_rebuy(fake_client, fake_db, monkeypatch):
    # Alpaca returns the position slashless; watchlist uses the slash form.
    fake_client.positions = {
        "BTCUSD": {"symbol": "BTCUSD", "qty": 0.5,
                   "unrealized_pl": 0.0, "current_price": 100.0}
    }
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])
    _force(monkeypatch, "BUY", 30)
    t.run_cycle()
    # Position is recognized -> no duplicate buy.
    assert _buys(fake_db) == []


def test_unheld_crypto_still_buys(fake_client, fake_db, monkeypatch):
    # No position at all -> a normal crypto BUY still goes through.
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])
    _force(monkeypatch, "BUY", 30)
    t.run_cycle()
    assert len(_buys(fake_db)) == 1
    assert _buys(fake_db)[0]["symbol"] == "BTC/USD"


def test_crypto_signal_sell_deferred(fake_client, fake_db, monkeypatch):
    # Held crypto + a SELL signal: the exit is deferred (crypto is not closed).
    fake_client.positions = {
        "BTCUSD": {"symbol": "BTCUSD", "qty": 0.5,
                   "unrealized_pl": 5.0, "current_price": 100.0}
    }
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])
    _force(monkeypatch, "SELL", 80)
    t.run_cycle()
    assert fake_client.closed == []
    assert _sells(fake_db) == []


def test_stock_signal_sell_still_fires(fake_client, fake_db, monkeypatch):
    # Control: stock exits are unchanged — a held stock still sells on signal.
    fake_client.positions = {
        "AAPL": {"symbol": "AAPL", "qty": 10.0,
                 "unrealized_pl": 5.0, "current_price": 100.0}
    }
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["AAPL"])
    _force(monkeypatch, "SELL", 80)
    t.run_cycle()
    assert "AAPL" in fake_client.closed
