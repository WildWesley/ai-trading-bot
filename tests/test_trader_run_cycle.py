"""End-to-end orchestration test for Trader.run_cycle.

Exercises the integrated path where the trend-cache warm-up, the market-hours
guard (closing-blackout flatten), the per-symbol loop, and the snapshot all run
together in one cycle — covering the interaction the per-method tests don't.
"""

from datetime import datetime, timedelta, timezone

from bot.trader import Trader
from tests.conftest import make_bars, rising_closes


def test_run_cycle_in_closing_blackout_flattens_stock_but_trades_crypto(
    fake_client, fake_db, monkeypatch
):
    now = datetime.now(timezone.utc)
    # Market open, but only 10 minutes to close -> closing blackout.
    fake_client.clock["is_open"] = True
    fake_client.clock["next_close"] = now + timedelta(minutes=10)
    # AAPL is in a confirmed uptrend (so trend filter would NOT block it) and
    # holds an open position to be flattened at EOD.
    fake_client.bars_daily["AAPL"] = make_bars(rising_closes(260))
    fake_client.positions = {
        "AAPL": {"symbol": "AAPL", "qty": 4, "current_price": 100.0,
                 "unrealized_pl": 8.0},
    }
    # Every symbol signals BUY this cycle.
    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0,
                              "reason": "x"},
    )

    trader = Trader(fake_client, fake_db, advisor=None,
                    watchlist=["AAPL", "BTC/USD"])
    # Market was already open earlier today (no opening blackout); the closing
    # blackout is what this cycle must react to.
    trader._market_was_open = True
    trader._market_opened_at = now - timedelta(hours=6)

    trader.run_cycle()

    # Trend cache was warmed during the cycle (AAPL classified up; crypto unknown).
    assert trader._trend_cache.get("AAPL") == "up"
    assert trader._trend_cache.get("BTC/USD") == "unknown"

    # Closing blackout flattened the stock once, left crypto alone, paused stocks.
    assert "AAPL" in fake_client.closed
    assert "BTC/USD" not in fake_client.closed
    assert trader._stock_trading_allowed is False

    # The only normal BUY this cycle is the crypto one (the stock was paused).
    buys = [t for t in fake_db.trades if t["side"] == "buy"]
    assert [t["symbol"] for t in buys] == ["BTC/USD"]

    # Exactly one end-of-day flatten sell, for the stock.
    eod = [t for t in fake_db.trades
           if "End-of-day" in (t.get("signal_reason") or "")]
    assert len(eod) == 1
    assert eod[0]["symbol"] == "AAPL"

    # A snapshot was recorded at the end of the cycle.
    assert len(fake_db.snapshots) == 1
