# Trading Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily golden-cross trend filter, a market-hours guard with opening/closing blackouts and end-of-day stock flattening, and a 100-symbol watchlist at a 5-minute cycle.

**Architecture:** No new modules. A pure `compute_daily_trend()` is added to `algorithm.py`; a `get_clock()` wrapper to `alpaca_client.py`; the `Trader` gains a per-day trend cache, a market-state check, and an EOD flatten. Config gains five constants plus an expanded watchlist. Crypto bypasses both the trend filter and the market-hours guard.

**Tech Stack:** Python 3.10+, pytest (new dev dependency), pandas, ta, alpaca-py.

**Working directory for all commands:** `C:\Users\natha\Programming\AI_Trading_Bot\trading-bot`. Run from a PowerShell prompt with the venv/interpreter that has the project deps. `git push` after each task (the user wants pushes throughout).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `requirements-dev.txt` | Dev/test deps | Create |
| `tests/__init__.py` | Make tests a package | Create |
| `tests/conftest.py` | Shared fakes (FakeClient, FakeDB) + fixtures | Create |
| `tests/test_algorithm_trend.py` | Tests for `compute_daily_trend` | Create |
| `tests/test_alpaca_clock.py` | Tests for `get_clock` | Create |
| `tests/test_trader_trend_filter.py` | Tests for trend filter | Create |
| `tests/test_trader_market_hours.py` | Tests for market guard + flatten | Create |
| `bot/config.py` | Constants + watchlist + interval | Modify |
| `bot/algorithm.py` | Add `compute_daily_trend()` | Modify |
| `bot/alpaca_client.py` | Add `get_clock()` | Modify |
| `bot/trader.py` | Trend cache, market guard, EOD flatten | Modify |

---

## Task 1: Test scaffolding

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Create `requirements-dev.txt`**

```
# Test dependencies (install with: pip install -r requirements-dev.txt)
-r requirements.txt
pytest>=8.0.0
```

- [ ] **Step 2: Install pytest**

Run: `python -m pip install -r requirements-dev.txt`
Expected: pytest installs successfully.

- [ ] **Step 3: Create `tests/__init__.py`** (empty file)

```python
```

- [ ] **Step 4: Create `tests/conftest.py` with shared fakes**

These fakes implement only the surface the `Trader` actually calls. Build trend DataFrames with a helper so tests can fabricate golden/death crosses.

```python
"""Shared test fakes and fixtures.

The Trader takes its client, db, and advisor as injected dependencies, so we
test it against in-memory fakes rather than touching Alpaca or SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytest


def make_bars(closes: list[float]) -> pd.DataFrame:
    """Build an OHLCV DataFrame from a list of close prices (oldest first)."""
    n = len(closes)
    idx = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000] * n,
        },
        index=idx,
    )


def rising_closes(n: int = 260, start: float = 100.0, step: float = 1.0) -> list[float]:
    """A steadily rising series → EMA(50) > EMA(200) → 'up'."""
    return [start + step * i for i in range(n)]


def falling_closes(n: int = 260, start: float = 400.0, step: float = 1.0) -> list[float]:
    """A steadily falling series → EMA(50) < EMA(200) → 'down'."""
    return [start - step * i for i in range(n)]


class FakeDB:
    """Records trades/snapshots in memory; returns incrementing trade ids."""

    def __init__(self) -> None:
        self.trades: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.commentary: dict[int, str] = {}

    def record_trade(self, trade: dict[str, Any]) -> int:
        self.trades.append(trade)
        return len(self.trades)

    def record_snapshot(self, snapshot: dict[str, Any]) -> int:
        self.snapshots.append(snapshot)
        return len(self.snapshots)

    def set_trade_commentary(self, trade_id: int, commentary: str) -> None:
        self.commentary[trade_id] = commentary


class FakeClient:
    """In-memory Alpaca stand-in. Configure attributes per test."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        # Default: market open, close 4 hours away (well outside blackout).
        self.clock: dict[str, Any] = {
            "is_open": True,
            "next_open": now + timedelta(days=1),
            "next_close": now + timedelta(hours=4),
            "timestamp": now,
        }
        self.bars_5min: dict[str, pd.DataFrame] = {}
        self.bars_daily: dict[str, pd.DataFrame] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []
        self.closed: list[str] = []

    def get_clock(self) -> dict[str, Any]:
        return self.clock

    def get_bars(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        if timeframe.lower().endswith("day"):
            return self.bars_daily.get(symbol, make_bars([100.0] * 5))
        return self.bars_5min.get(symbol, make_bars([100.0] * 5))

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        return self.positions.get(symbol)

    def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions.values())

    def get_account(self) -> dict[str, Any]:
        return {"equity": 100_000.0, "cash": 100_000.0, "buying_power": 100_000.0}

    def get_latest_price(self, symbol: str) -> float:
        return 100.0

    def place_market_order(self, symbol: str, qty: float, side: str) -> dict[str, Any]:
        order = {
            "symbol": symbol, "side": side, "qty": qty,
            "filled_qty": qty, "filled_avg_price": 100.0,
        }
        self.orders.append(order)
        return order

    def close_position(self, symbol: str) -> dict[str, Any]:
        self.closed.append(symbol)
        return {"symbol": symbol, "side": "sell", "filled_avg_price": 100.0}


@pytest.fixture
def fake_db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()
```

- [ ] **Step 5: Create `tests/test_smoke.py`**

```python
"""Smoke test: the package imports and fakes wire together."""

from bot import algorithm, config
from tests.conftest import FakeClient, FakeDB


def test_imports_and_fakes():
    assert hasattr(algorithm, "analyze")
    assert config.EMA_FAST_PERIOD == 9
    client = FakeClient()
    db = FakeDB()
    assert client.get_clock()["is_open"] is True
    assert db.record_trade({"symbol": "AAPL"}) == 1
```

- [ ] **Step 6: Run the smoke test**

Run: `python -m pytest tests/test_smoke.py -v`
Expected: PASS (1 passed).

- [ ] **Step 7: Add tests + dev deps to .gitignore-safe state and commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py tests/test_smoke.py
git commit -m "test: add pytest scaffolding and shared fakes

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 2: Config constants, interval, and 100-symbol watchlist

**Files:**
- Modify: `bot/config.py:92-107`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test** in `tests/test_config.py`

```python
"""Config defaults for the trading improvements."""

import importlib

import bot.config as config


def test_new_trend_and_market_constants_exist():
    importlib.reload(config)
    assert config.EMA_TREND_FAST_PERIOD == 50
    assert config.EMA_TREND_SLOW_PERIOD == 200
    assert config.TREND_LOOKBACK_BARS == 365
    assert config.MARKET_BLACKOUT_MINUTES == 15


def test_interval_default_is_five_minutes(monkeypatch):
    monkeypatch.delenv("TRADE_INTERVAL_SECONDS", raising=False)
    importlib.reload(config)
    assert config.TRADE_INTERVAL_SECONDS == 300


def test_watchlist_has_100_symbols(monkeypatch):
    monkeypatch.delenv("WATCHLIST", raising=False)
    importlib.reload(config)
    assert len(config.WATCHLIST) == 100
    # 7 crypto pairs included
    crypto = [s for s in config.WATCHLIST if "/" in s]
    assert len(crypto) == 7
    assert "BTC/USD" in config.WATCHLIST
    assert "DOGE/USD" not in config.WATCHLIST  # replaced with utility coins
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL (AttributeError: module 'bot.config' has no attribute 'EMA_TREND_FAST_PERIOD').

- [ ] **Step 3: Update the watchlist + interval defaults** — replace `bot/config.py` lines 92-96 (the `WATCHLIST` and `TRADE_INTERVAL_SECONDS` block)

Replace:
```python
WATCHLIST: list[str] = _get_list(
    "WATCHLIST", ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"]
)
TRADE_INTERVAL_SECONDS: int = _get_int("TRADE_INTERVAL_SECONDS", 60)
```
with:
```python
WATCHLIST: list[str] = _get_list(
    "WATCHLIST",
    [
        # Large cap tech
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "NFLX",
        "AMD", "CRM",
        # Semiconductors
        "INTC", "QCOM", "AVGO", "MU", "TSM",
        # Finance
        "JPM", "BAC", "GS", "WFC", "MS", "BLK", "V", "MA", "AXP", "C",
        "SCHW", "PYPL",
        # Healthcare
        "JNJ", "UNH", "PFE", "ABBV", "LLY", "MRK", "ABT", "CVS", "ISRG",
        "VRTX", "AMGN", "GILD",
        # Energy
        "XOM", "CVX", "COP", "SLB", "EOG", "OXY",
        # Consumer
        "WMT", "COST", "PG", "KO", "PEP", "HD", "TGT", "MCD", "SBUX",
        "NKE", "LOW",
        # Industrials
        "BA", "CAT", "DE", "HON", "UPS", "GE", "LMT", "RTX",
        # Cloud / growth
        "SNOW", "PLTR", "UBER", "SHOP", "DDOG", "NOW", "ADBE", "ORCL",
        "ZM", "COIN", "SQ", "ABNB",
        # Auto
        "F", "GM",
        # Telecom / media
        "DIS", "CMCSA", "T", "VZ",
        # REITs
        "AMT", "PLD", "EQIX",
        # ETFs
        "SPY", "QQQ", "IWM", "GLD", "TLT", "XLF", "XLE", "XLV",
        # Crypto (24/7 — bypass the market-hours guard and trend filter)
        "BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "AVAX/USD",
        "LINK/USD", "UNI/USD",
    ],
)
TRADE_INTERVAL_SECONDS: int = _get_int("TRADE_INTERVAL_SECONDS", 300)
```

- [ ] **Step 4: Add the new strategy constants** — in `bot/config.py`, after the existing strategy block (after `RSI_SELL_THRESHOLD: float = 65.0`, around line 107), add:

```python

# Daily trend filter (golden cross on daily bars). A BUY signal on a stock is
# skipped unless its medium-term trend is above its long-term trend.
EMA_TREND_FAST_PERIOD: int = 50    # daily EMA, "golden cross" fast leg
EMA_TREND_SLOW_PERIOD: int = 200   # daily EMA, "golden cross" slow leg
TREND_LOOKBACK_BARS: int = 365     # daily bars to fetch for the trend check

# Market-hours guard. Stocks are not traded in the first/last N minutes of the
# session; all stock positions are flattened before close. Crypto is exempt.
MARKET_BLACKOUT_MINUTES: int = 15
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Verify the count is exactly 100**

Run: `python -c "import bot.config as c; print(len(c.WATCHLIST))"`
Expected: `100`

- [ ] **Step 7: Commit**

```bash
git add bot/config.py tests/test_config.py
git commit -m "feat: add trend/market constants, 100-symbol watchlist, 5-min interval

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 3: `compute_daily_trend()` in algorithm.py

**Files:**
- Modify: `bot/algorithm.py` (add function near the other indicator helpers)
- Create: `tests/test_algorithm_trend.py`

- [ ] **Step 1: Write the failing test** in `tests/test_algorithm_trend.py`

```python
"""Tests for the daily golden-cross trend classifier."""

import pandas as pd

from bot import algorithm
from tests.conftest import falling_closes, make_bars, rising_closes


def test_uptrend_returns_up():
    bars = make_bars(rising_closes(260))
    assert algorithm.compute_daily_trend(bars) == "up"


def test_downtrend_returns_down():
    bars = make_bars(falling_closes(260))
    assert algorithm.compute_daily_trend(bars) == "down"


def test_insufficient_data_returns_unknown():
    bars = make_bars(rising_closes(50))  # < 200 bars
    assert algorithm.compute_daily_trend(bars) == "unknown"


def test_empty_frame_returns_unknown():
    assert algorithm.compute_daily_trend(pd.DataFrame()) == "unknown"


def test_none_returns_unknown():
    assert algorithm.compute_daily_trend(None) == "unknown"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_algorithm_trend.py -v`
Expected: FAIL (AttributeError: module 'bot.algorithm' has no attribute 'compute_daily_trend').

- [ ] **Step 3: Implement `compute_daily_trend`** — in `bot/algorithm.py`, add after the `indicator_frame` function (after line 78):

```python


def compute_daily_trend(daily_bars_df: pd.DataFrame | None) -> str:
    """Classify a symbol's long-term trend from daily bars.

    Uses a "golden cross": the medium-term EMA vs the long-term EMA on daily
    closes (windows from ``config.EMA_TREND_FAST_PERIOD`` /
    ``EMA_TREND_SLOW_PERIOD``).

    Returns
    -------
    "up"      EMA(fast) > EMA(slow) on the latest bar (uptrend; BUYs allowed).
    "down"    EMA(fast) < EMA(slow) (downtrend; BUYs skipped).
    "unknown" not enough data to decide (fewer than slow-period bars, or no
              usable ``close`` column). Treated as "don't block" by callers.
    """
    fast = config.EMA_TREND_FAST_PERIOD
    slow = config.EMA_TREND_SLOW_PERIOD
    if (
        daily_bars_df is None
        or "close" not in getattr(daily_bars_df, "columns", [])
        or len(daily_bars_df) < slow
    ):
        return "unknown"

    close = daily_bars_df["close"].astype(float).reset_index(drop=True)
    ema_fast = EMAIndicator(close=close, window=fast).ema_indicator().iloc[-1]
    ema_slow = EMAIndicator(close=close, window=slow).ema_indicator().iloc[-1]
    if math.isnan(ema_fast) or math.isnan(ema_slow):
        return "unknown"
    return "up" if ema_fast > ema_slow else "down"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_algorithm_trend.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/algorithm.py tests/test_algorithm_trend.py
git commit -m "feat: add compute_daily_trend golden-cross classifier

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 4: `get_clock()` in alpaca_client.py

**Files:**
- Modify: `bot/alpaca_client.py` (add method to `AlpacaClient`, after `get_account`)
- Create: `tests/test_alpaca_clock.py`

- [ ] **Step 1: Write the failing test** in `tests/test_alpaca_clock.py`

```python
"""Tests for AlpacaClient.get_clock — verified with a stubbed trading client."""

from datetime import datetime, timezone

from bot.alpaca_client import AlpacaClient


class _FakeClock:
    def __init__(self):
        self.is_open = True
        self.next_open = datetime(2026, 6, 8, 13, 30, tzinfo=timezone.utc)
        self.next_close = datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc)
        self.timestamp = datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)


class _FakeTrading:
    def get_clock(self):
        return _FakeClock()


def _make_client_without_network() -> AlpacaClient:
    # Bypass __init__ (which builds real alpaca clients) and inject a fake.
    client = AlpacaClient.__new__(AlpacaClient)
    client._trading = _FakeTrading()
    return client


def test_get_clock_normalizes_fields():
    client = _make_client_without_network()
    clock = client.get_clock()
    assert clock["is_open"] is True
    assert clock["next_open"] == datetime(2026, 6, 8, 13, 30, tzinfo=timezone.utc)
    assert clock["next_close"] == datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc)
    assert clock["timestamp"] == datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_alpaca_clock.py -v`
Expected: FAIL (AttributeError: 'AlpacaClient' object has no attribute 'get_clock').

- [ ] **Step 3: Implement `get_clock`** — in `bot/alpaca_client.py`, add inside `AlpacaClient` right after the `get_account` method (after line 222):

```python

    # -- Market clock ----------------------------------------------------
    def get_clock(self) -> dict[str, Any]:
        """Return the market clock: is_open plus next open/close timestamps.

        ``next_open`` / ``next_close`` are timezone-aware datetimes straight
        from alpaca-py. Authoritative for trading hours and holidays, so the
        trader uses this rather than computing Eastern time locally.
        """
        clock = _with_backoff(self._trading.get_clock, what="get_clock")
        return {
            "is_open": bool(getattr(clock, "is_open", False)),
            "next_open": getattr(clock, "next_open", None),
            "next_close": getattr(clock, "next_close", None),
            "timestamp": getattr(clock, "timestamp", None),
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_alpaca_clock.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add bot/alpaca_client.py tests/test_alpaca_clock.py
git commit -m "feat: add AlpacaClient.get_clock market-hours wrapper

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 5: Trader trend cache + BUY filter

**Files:**
- Modify: `bot/trader.py` (`__init__`, `run_cycle`, `_process_symbol`; add `_warm_trend_cache_if_needed`)
- Create: `tests/test_trader_trend_filter.py`

- [ ] **Step 1: Write the failing test** in `tests/test_trader_trend_filter.py`

```python
"""The daily trend filter blocks BUYs on downtrending stocks, not crypto."""

from bot import config
from bot.trader import Trader
from tests.conftest import falling_closes, make_bars, rising_closes


def _buy_bars():
    # A short 5-min series; the test forces the signal via monkeypatch below.
    return make_bars([100.0] * 30)


def test_buy_skipped_when_stock_trend_down(fake_client, fake_db, monkeypatch):
    fake_client.bars_daily["AAPL"] = make_bars(falling_closes(260))
    trader = Trader(fake_client, fake_db, advisor=None, watchlist=["AAPL"])
    trader._stock_trading_allowed = True
    trader._warm_trend_cache_if_needed()
    assert trader._trend_cache["AAPL"] == "down"

    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    trader._process_symbol("AAPL")
    assert fake_db.trades == []  # BUY was blocked by the down trend


def test_buy_allowed_when_stock_trend_up(fake_client, fake_db, monkeypatch):
    fake_client.bars_daily["AAPL"] = make_bars(rising_closes(260))
    trader = Trader(fake_client, fake_db, advisor=None, watchlist=["AAPL"])
    trader._stock_trading_allowed = True
    trader._warm_trend_cache_if_needed()
    assert trader._trend_cache["AAPL"] == "up"

    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    trader._process_symbol("AAPL")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["side"] == "buy"


def test_crypto_buy_ignores_trend_filter(fake_client, fake_db, monkeypatch):
    # No daily bars for crypto; trend stays "unknown" but must not block.
    trader = Trader(fake_client, fake_db, advisor=None, watchlist=["BTC/USD"])
    trader._stock_trading_allowed = False  # market closed; crypto still trades
    trader._warm_trend_cache_if_needed()

    monkeypatch.setattr(
        "bot.algorithm.analyze",
        lambda symbol, bars: {"signal": "BUY", "rsi": 30, "price": 100.0, "reason": "x"},
    )
    trader._process_symbol("BTC/USD")
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["symbol"] == "BTC/USD"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_trader_trend_filter.py -v`
Expected: FAIL (AttributeError: 'Trader' object has no attribute '_warm_trend_cache_if_needed').

- [ ] **Step 3: Add trend-cache state to `Trader.__init__`** — in `bot/trader.py`, after the `self.last_cycle_at` line (line 72), add:

```python
        # Daily trend cache (symbol -> "up"/"down"/"unknown"), refreshed once
        # per calendar day. Crypto entries stay "unknown" and bypass the filter.
        self._trend_cache: dict[str, str] = {}
        self._trend_cache_date: str | None = None
        # Market-state flags, set each cycle by _update_market_state (Task 6).
        # Default True so trading works before the first market check runs.
        self._stock_trading_allowed: bool = True
```

- [ ] **Step 4: Add the import for `datetime` date string** — `bot/trader.py` already imports `from datetime import datetime, timezone` (line 23). No change needed.

- [ ] **Step 5: Add `_warm_trend_cache_if_needed`** — in `bot/trader.py`, add as a new method right before `_process_symbol` (before line 112):

```python
    # -- Trend cache -----------------------------------------------------
    def _warm_trend_cache_if_needed(self) -> None:
        """Populate the daily-trend cache once per calendar day.

        Fetches ``TREND_LOOKBACK_BARS`` daily bars per stock and classifies the
        trend via ``algorithm.compute_daily_trend``. Crypto is recorded as
        "unknown" (it bypasses the filter). Per-symbol failures degrade to
        "unknown" rather than aborting the warm-up.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trend_cache_date == today and self._trend_cache:
            return
        self.log("info", "Warming daily-trend cache...")
        new_cache: dict[str, str] = {}
        total = len(self.watchlist)
        for i, symbol in enumerate(self.watchlist, start=1):
            if is_crypto_symbol(symbol):
                new_cache[symbol] = "unknown"
                continue
            try:
                daily = self.client.get_bars(
                    symbol, "1Day", config.TREND_LOOKBACK_BARS
                )
                new_cache[symbol] = algorithm.compute_daily_trend(daily)
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol faults
                new_cache[symbol] = "unknown"
                self.log("error", f"{symbol}: trend warm-up failed: {exc}")
            if i % 25 == 0:
                self.log("info", f"Trend cache: {i}/{total} symbols...")
        self._trend_cache = new_cache
        self._trend_cache_date = today
        up = sum(1 for v in new_cache.values() if v == "up")
        down = sum(1 for v in new_cache.values() if v == "down")
        self.log("info", f"Trend cache ready: {up} up, {down} down.")
```

- [ ] **Step 6: Wire the warm-up into `run_cycle`** — in `bot/trader.py`, replace the body of `run_cycle` (lines 74-88) with:

```python
    def run_cycle(self) -> None:
        """Run one full trading cycle. Never raises — errors become events."""
        self.cycle_count += 1
        self.log("info", f"Cycle {self.cycle_count} started.")
        self._warm_trend_cache_if_needed()
        for symbol in self.watchlist:
            try:
                self._process_symbol(symbol)
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol faults
                self.log("error", f"{symbol}: {exc}")
        try:
            self._record_snapshot()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"snapshot failed: {exc}")
        self.last_cycle_at = _utc_now_iso()
        self.log("info", f"Cycle {self.cycle_count} complete.")
```

- [ ] **Step 7: Add the BUY trend filter in `_process_symbol`** — in `bot/trader.py`, replace the BUY branch (lines 132-138) with:

```python
        if signal == "BUY" and not has_position:
            if not is_crypto_symbol(symbol):
                trend = self._trend_cache.get(symbol, "unknown")
                if trend == "down":
                    self.log(
                        "debug",
                        f"{symbol}: BUY skipped — daily trend bearish "
                        f"(death cross).",
                    )
                    return
            self._open_long(symbol, analysis)
        elif signal == "SELL" and has_position:
            self._close_long(symbol, analysis, position)
        else:
            # Quietly hold; avoid flooding the event log on every HOLD.
            pass
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `python -m pytest tests/test_trader_trend_filter.py -v`
Expected: PASS (3 passed).

- [ ] **Step 9: Run the full suite to check for regressions**

Run: `python -m pytest -v`
Expected: all PASS.

- [ ] **Step 10: Commit**

```bash
git add bot/trader.py tests/test_trader_trend_filter.py
git commit -m "feat: trend cache warm-up and BUY filter in Trader

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 6: Trader market-hours guard + end-of-day flatten

**Files:**
- Modify: `bot/trader.py` (`__init__`, `run_cycle`, `_process_symbol`; add `_update_market_state`, `_flatten_stock_positions`)
- Create: `tests/test_trader_market_hours.py`

- [ ] **Step 1: Write the failing test** in `tests/test_trader_market_hours.py`

```python
"""Market-hours guard: blackouts, EOD flatten, crypto exemption."""

from datetime import datetime, timedelta, timezone

from bot.trader import Trader
from tests.conftest import make_bars


def _trader(fake_client, fake_db, watchlist):
    t = Trader(fake_client, fake_db, advisor=None, watchlist=watchlist)
    # Skip the trend warm-up path for these tests.
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
    # Simulate a prior cycle where the market was already open (no opening
    # blackout): set _market_opened_at well in the past.
    t._market_was_open = True
    t._market_opened_at = now - timedelta(hours=2)
    t._update_market_state()
    assert t._stock_trading_allowed is True


def test_closing_blackout_flattens_stock_positions_once(fake_client, fake_db):
    now = datetime.now(timezone.utc)
    fake_client.clock["is_open"] = True
    fake_client.clock["next_close"] = now + timedelta(minutes=10)  # inside 15-min
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

    assert "AAPL" in fake_client.closed       # stock flattened
    assert "BTC/USD" not in fake_client.closed  # crypto left alone
    assert t._stock_trading_allowed is False    # in closing blackout
    assert len(fake_db.trades) == 1
    assert fake_db.trades[0]["symbol"] == "AAPL"

    # Running again the same day must NOT flatten a second time.
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
    t._process_symbol("AAPL")     # stock — should be skipped
    t._process_symbol("BTC/USD")  # crypto — should trade
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_trader_market_hours.py -v`
Expected: FAIL (AttributeError: 'Trader' object has no attribute '_update_market_state').

- [ ] **Step 3: Add market-state flags to `Trader.__init__`** — in `bot/trader.py`, extend the block added in Task 5 (after `self._stock_trading_allowed`) with:

```python
        # Market-open transition tracking. _market_was_open starts None
        # (unknown) so a bot started mid-session does NOT impose an opening
        # blackout — that only fires on an observed closed->open transition.
        self._market_was_open: bool | None = None
        self._market_opened_at: datetime | None = None
        self._flattened_today: bool = False
```

- [ ] **Step 4: Add `_update_market_state` and `_flatten_stock_positions`** — in `bot/trader.py`, add both methods right before `_warm_trend_cache_if_needed` (added in Task 5):

```python
    # -- Market hours ----------------------------------------------------
    def _update_market_state(self) -> None:
        """Set ``self._stock_trading_allowed`` from the market clock.

        Rules (stocks only — crypto always trades):
          * market closed                -> stock trading off
          * within MARKET_BLACKOUT_MINUTES of open  -> off (opening blackout)
          * within MARKET_BLACKOUT_MINUTES of close -> off, and flatten all
            stock positions once for the day (overnight-gap protection)
        On any clock failure, stock trading is disabled for this cycle (safe
        default); crypto is unaffected.
        """
        try:
            clock = self.client.get_clock()
        except Exception as exc:  # noqa: BLE001 - degrade safely
            self.log(
                "error",
                f"market clock unavailable; pausing stock trades: {exc}",
            )
            self._stock_trading_allowed = False
            return

        is_open = bool(clock.get("is_open"))
        now_utc = datetime.now(timezone.utc)

        # Observed closed -> open transition: record open time, reset flatten.
        if is_open and self._market_was_open is False:
            self._market_opened_at = now_utc
            self._flattened_today = False
            self.log("info", "Market opened.")
        self._market_was_open = is_open

        if not is_open:
            self._stock_trading_allowed = False
            return

        blackout = config.MARKET_BLACKOUT_MINUTES * 60

        in_opening_blackout = (
            self._market_opened_at is not None
            and (now_utc - self._market_opened_at).total_seconds() < blackout
        )

        next_close = clock.get("next_close")
        in_closing_blackout = False
        if next_close is not None:
            secs_to_close = (next_close - now_utc).total_seconds()
            in_closing_blackout = 0 <= secs_to_close < blackout

        if in_closing_blackout and not self._flattened_today:
            self._flatten_stock_positions()
            self._flattened_today = True

        self._stock_trading_allowed = not (
            in_opening_blackout or in_closing_blackout
        )

    def _flatten_stock_positions(self) -> None:
        """Close every open *stock* position (crypto is left running 24/7)."""
        self.log("info", "End-of-day: flattening all stock positions.")
        try:
            positions = self.client.get_positions()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"EOD flatten: could not list positions: {exc}")
            return
        for pos in positions:
            symbol = str(pos.get("symbol", ""))
            if not symbol or is_crypto_symbol(symbol):
                continue
            try:
                order = self.client.close_position(symbol)
                fill = (
                    order.get("filled_avg_price")
                    or pos.get("current_price")
                    or 0.0
                )
                qty = float(pos.get("qty", 0))
                pnl = float(pos.get("unrealized_pl", 0.0))
                trade = {
                    "symbol": symbol,
                    "side": "sell",
                    "qty": qty,
                    "price": fill,
                    "total_value": round(qty * fill, 2),
                    "signal_reason": "End-of-day flatten (market-close blackout).",
                    "timestamp": _utc_now_iso(),
                    "pnl_at_close": round(pnl, 2),
                }
                self.db.record_trade(trade)
                self.log(
                    "trade",
                    f"EOD SELL {qty} {symbol} @ ${fill:.2f} "
                    f"(P&L ${pnl:+.2f}).",
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol faults
                self.log("error", f"EOD flatten failed for {symbol}: {exc}")
```

- [ ] **Step 5: Call `_update_market_state` in `run_cycle`** — in `bot/trader.py`, in the `run_cycle` body edited in Task 5, add the market-state call immediately after the warm-up call:

```python
        self._warm_trend_cache_if_needed()
        self._update_market_state()
```

- [ ] **Step 6: Add the stock skip guard at the top of `_process_symbol`** — in `bot/trader.py`, the first lines of `_process_symbol` currently fetch bars (line 113). Insert the guard as the very first statements of the method body, before the `bars = ...` line:

```python
    def _process_symbol(self, symbol: str) -> None:
        # Stocks pause when the market is closed or in a blackout window;
        # crypto trades around the clock.
        if not is_crypto_symbol(symbol) and not self._stock_trading_allowed:
            return
        bars = self.client.get_bars(
            symbol, f"{config.BAR_TIMEFRAME_MINUTES}Min", config.BARS_LOOKBACK
        )
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `python -m pytest tests/test_trader_market_hours.py -v`
Expected: PASS (5 passed).

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest -v`
Expected: all PASS.

- [ ] **Step 9: Commit**

```bash
git add bot/trader.py tests/test_trader_market_hours.py
git commit -m "feat: market-hours guard with blackouts and EOD stock flatten

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Task 7: Update CLAUDE.md documentation

**Files:**
- Modify: `trading-bot/CLAUDE.md`

- [ ] **Step 1: Update the strategy summary section** — in `CLAUDE.md`, replace the "Strategy summary" section (lines 72-77) with:

```markdown
## Strategy summary

Two-timeframe strategy:

**Long-term filter (daily bars, 365-day lookback):** a stock is only eligible
for BUYs when EMA(50) > EMA(200) (golden cross). Computed once per day and
cached per symbol in the trader. Crypto bypasses this filter.

**Short-term timing (5-minute candles, last 50 bars):**
- **BUY**: `RSI(14) < 35` **and** `EMA(9)` crosses above `EMA(21)` **and** the
  daily trend is not "down"
- **SELL**: `RSI(14) > 65` **and** `EMA(9)` crosses below `EMA(21)`
- **HOLD**: otherwise

**Market-hours guard (stocks only):** no new stock trades in the first/last 15
minutes of the session or while the market is closed; all stock positions are
flattened ~15 minutes before close to avoid overnight gap risk. Crypto trades
24/7. Driven by Alpaca's `/clock` endpoint.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document two-timeframe strategy and market-hours guard

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push
```

---

## Final verification

- [ ] **Run the entire test suite one last time**

Run: `python -m pytest -v`
Expected: all tests PASS (smoke, config, algorithm_trend, alpaca_clock, trader_trend_filter, trader_market_hours).

- [ ] **Confirm the bot still imports and config validates**

Run: `python -c "from bot import main, trader, algorithm, alpaca_client, config; print('imports OK; watchlist=', len(config.WATCHLIST))"`
Expected: `imports OK; watchlist= 100`
