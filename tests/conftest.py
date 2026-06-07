"""Shared test fakes and fixtures.

The Trader takes its client, db, and advisor as injected dependencies, so we
test it against in-memory fakes rather than touching Alpaca or SQLite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

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
