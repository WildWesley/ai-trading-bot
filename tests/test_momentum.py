"""Momentum rotation: pure ranking (momentum.select_top) + the trader's weekly
rebalance (sell leavers, buy new entrants equal-weight, hold winners)."""

import bot.config as config
from bot import momentum
from bot.trader import Trader

from .conftest import make_bars, rising_closes, falling_closes


# ---- pure ranking --------------------------------------------------------
def _bars(strength):
    # 30 bars; slope controls 12-1 momentum. Above its own short SMA when rising.
    return make_bars(rising_closes(30, 100.0, strength))


PARAMS = dict(lookback=10, skip=1, sma_window=20, top_n=2)


def test_select_top_ranks_by_momentum():
    bars = {"AAA": _bars(2.0), "BBB": _bars(0.5), "CCC": _bars(1.0)}
    # top_n=2 → the two steepest risers, highest first.
    assert momentum.select_top(bars, **PARAMS) == ["AAA", "CCC"]


def test_select_top_excludes_below_sma():
    bars = {"UP": _bars(2.0), "DOWN": make_bars(falling_closes(30, 200.0, 2.0))}
    picked = momentum.select_top(bars, **PARAMS)
    assert "UP" in picked and "DOWN" not in picked


def test_select_top_skips_short_history():
    # Only ~15 bars < max(lookback+2, sma_window)=20 → ineligible.
    bars = {"SHORT": make_bars(rising_closes(15, 100.0, 2.0)), "OK": _bars(1.0)}
    assert momentum.select_top(bars, **PARAMS) == ["OK"]


# ---- trader rebalance ----------------------------------------------------
def _mom_config(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY", "momentum")
    monkeypatch.setattr(config, "MOMENTUM_TOP_N", 2)
    monkeypatch.setattr(config, "MOMENTUM_LOOKBACK_DAYS", 10)
    monkeypatch.setattr(config, "MOMENTUM_SKIP_DAYS", 1)
    monkeypatch.setattr(config, "MOMENTUM_SMA_WINDOW", 20)
    monkeypatch.setattr(config, "MOMENTUM_BARS_LOOKBACK", 30)


def test_rebalance_sells_leavers_buys_entrants(fake_client, fake_db, monkeypatch):
    _mom_config(monkeypatch)
    fake_client.bars_daily = {
        "AAA": _bars(2.0),   # strongest → target
        "BBB": _bars(0.5),   # weak, above SMA → not top-2 here (CCC beats it)
        "CCC": _bars(1.0),   # second → target
    }
    # Hold CCC (a target — should ride) and OLD (a leaver — should be sold).
    fake_client.positions = {
        "CCC": {"symbol": "CCC", "qty": 10, "current_price": 129.0, "unrealized_pl": 8.0},
        "OLD": {"symbol": "OLD", "qty": 5, "current_price": 50.0, "unrealized_pl": -3.0},
    }
    t = Trader(fake_client, fake_db, advisor=None,
               watchlist=["AAA", "BBB", "CCC", "BTC/USD", "SPY"])
    t._rebalance_momentum()

    # target = [AAA, CCC]; OLD is out -> sold; CCC held -> rides (not re-bought).
    assert "OLD" in fake_client.closed
    assert "CCC" not in fake_client.closed
    bought = [o["symbol"] for o in fake_client.orders if o["side"] == "buy"]
    assert "AAA" in bought          # new entrant
    assert "CCC" not in bought      # already held, left to run
    assert "BBB" not in bought      # not in the top-2
    # Universe excludes benchmarks/crypto.
    assert "SPY" not in bought and "BTC/USD" not in bought
    # Equal-weight sizing: entrant bought with equity / TOP_N = 100k/2 = $50k notional.
    aaa = next(o for o in fake_client.orders if o["symbol"] == "AAA")
    assert aaa["notional"] == 50_000.0


def test_rebalance_fires_once_per_week(fake_client, fake_db, monkeypatch):
    _mom_config(monkeypatch)
    monkeypatch.setattr(config, "MOMENTUM_REBALANCE_WEEKDAY", 0)  # any weekday qualifies
    fake_client.bars_daily = {"AAA": _bars(2.0), "CCC": _bars(1.0)}
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["AAA", "CCC"])

    t._maybe_rebalance_momentum()
    orders_after_first = len(fake_client.orders)
    assert orders_after_first > 0                 # rebalanced
    t._maybe_rebalance_momentum()                 # same ISO week → no-op
    assert len(fake_client.orders) == orders_after_first


def test_rebalance_fires_on_startup_regardless_of_weekday(fake_client, fake_db, monkeypatch):
    _mom_config(monkeypatch)
    # Configure the recurring cadence for Sunday (weekday 6) — normally that
    # would block a Mon–Sat cycle. The FIRST rebalance after startup must fire
    # anyway (market is open in the fake clock), so a fresh deploy invests now.
    monkeypatch.setattr(config, "MOMENTUM_REBALANCE_WEEKDAY", 6)
    fake_client.bars_daily = {"AAA": _bars(2.0), "CCC": _bars(1.0)}
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["AAA", "CCC"])
    assert t._did_initial_rebalance is False
    t._maybe_rebalance_momentum()
    assert t._did_initial_rebalance is True
    assert len(fake_client.orders) > 0            # invested immediately on startup


def test_recurring_rebalance_waits_for_midday(fake_client, fake_db, monkeypatch):
    _mom_config(monkeypatch)
    monkeypatch.setattr(config, "MOMENTUM_REBALANCE_WEEKDAY", 0)  # any weekday qualifies
    monkeypatch.setattr(config, "MOMENTUM_REBALANCE_HOUR_ET", 24)  # never reached today
    fake_client.bars_daily = {"AAA": _bars(2.0), "CCC": _bars(1.0)}
    t = Trader(fake_client, fake_db, advisor=None, watchlist=["AAA", "CCC"])
    # Simulate a already-deployed bot (past the startup rebalance): a RECURRING
    # rebalance must wait for the midday hour, so nothing trades before then.
    t._did_initial_rebalance = True
    t._maybe_rebalance_momentum()
    assert len(fake_client.orders) == 0
