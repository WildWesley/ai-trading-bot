"""CRYPTO_TRADING_ENABLED master switch (2026-06-18). When off, the bot opens no
new crypto positions but still EXITS any it already holds, so the book winds down
to cash rather than being force-sold or re-bought."""

from datetime import datetime, timezone

from bot.trader import Trader


def _trader(fake_client, fake_db):
    return Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])


def test_disabled_blocks_new_crypto_entry(fake_client, fake_db, monkeypatch):
    monkeypatch.setattr("bot.config.CRYPTO_TRADING_ENABLED", False)
    t = _trader(fake_client, fake_db)
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    t._process_symbol("BTC/USD")
    assert [tr for tr in fake_db.trades if tr["side"] == "buy"] == []
    assert fake_client.orders == []


def test_enabled_still_allows_crypto_entry(fake_client, fake_db, monkeypatch):
    # Autouse fixture leaves it enabled; entry proceeds normally.
    t = _trader(fake_client, fake_db)
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    t._process_symbol("BTC/USD")
    assert [tr for tr in fake_db.trades if tr["side"] == "buy"]


def test_disabled_still_exits_held_crypto(fake_client, fake_db, monkeypatch):
    # Existing position at +6% must still take profit while entries are off.
    monkeypatch.setattr("bot.config.CRYPTO_TRADING_ENABLED", False)
    fake_client.positions = {
        "BTCUSD": {"symbol": "BTCUSD", "qty": 1.0, "avg_entry_price": 100.0,
                   "current_price": 106.0, "unrealized_pl": 6.0},
    }
    t = _trader(fake_client, fake_db)
    t._crypto_entry_times["BTC/USD"] = datetime.now(timezone.utc)
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "HOLD", "rsi": 50, "price": 106.0, "reason": "x"},
    )
    t.run_cycle()
    assert "BTCUSD" in fake_client.closed
    sells = [tr for tr in fake_db.trades if tr["side"] == "sell"]
    assert sells and "take-profit" in sells[-1]["signal_reason"]
