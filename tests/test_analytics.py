"""Tests for round-trip pairing of recorded trades."""

from bot.analytics import pair_round_trips


def _t(symbol, side, qty, price, ts):
    return {"symbol": symbol, "side": side, "qty": qty, "price": price,
            "timestamp": ts}


def test_single_buy_sell_pairs_into_one_trip():
    trades = [
        _t("AAPL", "buy", 5, 100.0, "2026-06-08T13:35:00"),
        _t("AAPL", "sell", 5, 110.0, "2026-06-08T18:00:00"),
    ]
    trips = pair_round_trips(trades)
    assert len(trips) == 1
    trip = trips[0]
    assert trip["symbol"] == "AAPL"
    assert trip["entry_price"] == 100.0
    assert trip["exit_price"] == 110.0
    assert trip["qty"] == 5
    assert trip["pnl"] == 50.0          # (110-100)*5
    assert trip["pnl_pct"] == 10.0
    assert trip["entry_time"] == "2026-06-08T13:35:00"
    assert trip["exit_time"] == "2026-06-08T18:00:00"


def test_input_order_does_not_matter():
    # Sell listed before buy; sorting by timestamp still pairs correctly.
    trades = [
        _t("AAPL", "sell", 5, 110.0, "2026-06-08T18:00:00"),
        _t("AAPL", "buy", 5, 100.0, "2026-06-08T13:35:00"),
    ]
    trips = pair_round_trips(trades)
    assert len(trips) == 1
    assert trips[0]["pnl"] == 50.0


def test_symbols_do_not_cross_match():
    trades = [
        _t("AAPL", "buy", 5, 100.0, "2026-06-08T13:35:00"),
        _t("MSFT", "buy", 2, 200.0, "2026-06-08T13:40:00"),
        _t("MSFT", "sell", 2, 210.0, "2026-06-08T18:00:00"),
    ]
    trips = pair_round_trips(trades)
    assert len(trips) == 1                 # only MSFT closed; AAPL still open
    assert trips[0]["symbol"] == "MSFT"
    assert trips[0]["pnl"] == 20.0


def test_open_buy_with_no_sell_is_omitted():
    trades = [_t("AAPL", "buy", 5, 100.0, "2026-06-08T13:35:00")]
    assert pair_round_trips(trades) == []


def test_unmatched_sell_is_ignored_without_error():
    trades = [_t("AAPL", "sell", 5, 110.0, "2026-06-08T18:00:00")]
    assert pair_round_trips(trades) == []


def test_two_buys_one_sell_fifo_splits_into_two_trips():
    trades = [
        _t("AAPL", "buy", 5, 100.0, "2026-06-08T13:35:00"),
        _t("AAPL", "buy", 5, 104.0, "2026-06-08T14:00:00"),
        _t("AAPL", "sell", 10, 110.0, "2026-06-08T18:00:00"),
    ]
    trips = pair_round_trips(trades)
    assert len(trips) == 2
    # newest-exit-first, but both share the exit; check both entries present.
    entries = sorted(t["entry_price"] for t in trips)
    assert entries == [100.0, 104.0]
    assert sum(t["pnl"] for t in trips) == (110 - 100) * 5 + (110 - 104) * 5


def test_partial_sells_pair_against_one_buy():
    trades = [
        _t("AAPL", "buy", 10, 100.0, "2026-06-08T13:35:00"),
        _t("AAPL", "sell", 4, 105.0, "2026-06-08T15:00:00"),
        _t("AAPL", "sell", 6, 108.0, "2026-06-08T17:00:00"),
    ]
    trips = pair_round_trips(trades)
    assert len(trips) == 2
    assert sum(t["qty"] for t in trips) == 10
    assert sum(t["pnl"] for t in trips) == (105 - 100) * 4 + (108 - 100) * 6


def test_newest_exit_is_first():
    trades = [
        _t("AAPL", "buy", 1, 100.0, "2026-06-08T13:00:00"),
        _t("AAPL", "sell", 1, 101.0, "2026-06-08T14:00:00"),
        _t("MSFT", "buy", 1, 200.0, "2026-06-08T15:00:00"),
        _t("MSFT", "sell", 1, 202.0, "2026-06-08T16:00:00"),
    ]
    trips = pair_round_trips(trades)
    assert [t["symbol"] for t in trips] == ["MSFT", "AAPL"]
