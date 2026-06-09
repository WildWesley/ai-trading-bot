"""The per-cycle bulk positions snapshot (one get_positions call vs per-symbol).

run_cycle fetches all open positions once into self._positions; _process_symbol
reads from that. If the fetch fails, the cycle skips trading (so held positions
aren't mistaken for flat and double-bought).
"""

from datetime import datetime, timedelta, timezone

from bot.trader import Trader


def _ready_trader(fake_client, fake_db, watchlist):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=watchlist)
    t._trend_cache = {s: "up" for s in watchlist}
    t._trend_cache_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return t


def test_run_cycle_snapshots_positions_once(fake_client, fake_db, monkeypatch):
    calls = {"n": 0}
    base = fake_client.get_positions

    def counting():
        calls["n"] += 1
        return base()

    fake_client.get_positions = counting
    t = _ready_trader(fake_client, fake_db, ["BTC/USD", "ETH/USD"])
    t._stock_trading_allowed = True
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "HOLD", "rsi": 50, "price": 100.0, "reason": "x"},
    )
    t.run_cycle()
    # One snapshot for the loop + one in _record_snapshot = 2, NOT one-per-symbol.
    assert calls["n"] == 2


def test_sell_uses_the_positions_snapshot(fake_client, fake_db, monkeypatch):
    now = datetime.now(timezone.utc)
    fake_client.clock["is_open"] = True
    fake_client.clock["next_close"] = now + timedelta(hours=4)
    fake_client.positions = {
        "AAPL": {"symbol": "AAPL", "qty": 5, "current_price": 100.0,
                 "unrealized_pl": 10.0},
    }
    t = _ready_trader(fake_client, fake_db, ["AAPL"])
    t._market_was_open = True
    t._market_opened_at = now - timedelta(hours=2)
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "SELL", "rsi": 70, "price": 100.0, "reason": "x"},
    )
    t.run_cycle()
    assert "AAPL" in fake_client.closed  # recognized as held, then closed


def test_positions_fetch_failure_skips_trading(fake_client, fake_db, monkeypatch):
    def boom():
        raise RuntimeError("positions endpoint down")

    fake_client.get_positions = boom
    t = _ready_trader(fake_client, fake_db, ["BTC/USD"])
    t._stock_trading_allowed = False  # crypto would otherwise trade
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda s, b: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    t.run_cycle()
    assert fake_db.trades == []  # no trades placed when positions can't be read
