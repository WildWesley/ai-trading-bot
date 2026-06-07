"""Market-hours guard: blackouts, EOD flatten, crypto exemption."""

from datetime import datetime, timedelta, timezone

from bot.trader import Trader


def _trader(fake_client, fake_db, watchlist):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=watchlist)
    # Pre-seed the trend cache so warm-up isn't exercised in these tests.
    t._trend_cache = {s: "up" for s in watchlist}
    t._trend_cache_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return t


def test_market_closed_disables_stock_trading(fake_client, fake_db):
    fake_client.clock["is_open"] = False
    t = _trader(fake_client, fake_db, ["AAPL"])
    t._update_market_state()
    assert t._stock_trading_allowed is False


def test_market_open_outside_blackout_allows_stock_trading(fake_client, fake_db):
    now = datetime.now(timezone.utc)
    fake_client.clock["is_open"] = True
    fake_client.clock["next_close"] = now + timedelta(hours=4)
    t = _trader(fake_client, fake_db, ["AAPL"])
    t._market_was_open = True
    t._market_opened_at = now - timedelta(hours=2)
    t._update_market_state()
    assert t._stock_trading_allowed is True


def test_closing_blackout_flattens_stock_positions_once(fake_client, fake_db):
    now = datetime.now(timezone.utc)
    fake_client.clock["is_open"] = True
    fake_client.clock["next_close"] = now + timedelta(minutes=10)
    fake_client.positions = {
        "AAPL": {"symbol": "AAPL", "qty": 5, "current_price": 100.0,
                 "unrealized_pl": 12.5},
        "BTC/USD": {"symbol": "BTC/USD", "qty": 0.1, "current_price": 50000.0,
                    "unrealized_pl": 3.0},
    }
    t = _trader(fake_client, fake_db, ["AAPL", "BTC/USD"])
    t._market_was_open = True
    t._market_opened_at = now - timedelta(hours=6)
    t._update_market_state()

    assert "AAPL" in fake_client.closed
    assert "BTC/USD" not in fake_client.closed
    assert t._stock_trading_allowed is False
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["symbol"] == "AAPL"

    fake_client.closed.clear()
    t._update_market_state()
    assert fake_client.closed == []


def test_stock_skipped_but_crypto_trades_when_market_closed(
    fake_client, fake_db, monkeypatch
):
    fake_client.clock["is_open"] = False
    t = _trader(fake_client, fake_db, ["AAPL", "BTC/USD"])
    t._update_market_state()
    assert t._stock_trading_allowed is False

    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    t._process_symbol("AAPL")
    t._process_symbol("BTC/USD")
    symbols_traded = [tr["symbol"] for tr in fake_db.trades]
    assert "AAPL" not in symbols_traded
    assert "BTC/USD" in symbols_traded


def test_clock_failure_disables_stock_trading(fake_client, fake_db):
    def _boom():
        raise RuntimeError("clock down")
    fake_client.get_clock = _boom
    t = _trader(fake_client, fake_db, ["AAPL"])
    t._update_market_state()
    assert t._stock_trading_allowed is False
