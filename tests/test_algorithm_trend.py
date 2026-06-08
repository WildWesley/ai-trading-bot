"""Tests for the daily-trend classifier (fast vs slow daily EMA)."""

import pandas as pd

from bot import algorithm, config
from tests.conftest import falling_closes, make_bars, rising_closes


def test_uptrend_returns_up():
    bars = make_bars(rising_closes(260))
    assert algorithm.compute_daily_trend(bars) == "up"


def test_downtrend_returns_down():
    bars = make_bars(falling_closes(260))
    assert algorithm.compute_daily_trend(bars) == "down"


def test_insufficient_data_returns_unknown():
    # Clearly fewer bars than the slow EMA period needs.
    bars = make_bars(rising_closes(config.EMA_TREND_SLOW_PERIOD // 2))
    assert algorithm.compute_daily_trend(bars) == "unknown"


def test_empty_frame_returns_unknown():
    assert algorithm.compute_daily_trend(pd.DataFrame()) == "unknown"


def test_none_returns_unknown():
    assert algorithm.compute_daily_trend(None) == "unknown"


def test_exactly_slow_period_bars_is_decisive():
    # With exactly EMA_TREND_SLOW_PERIOD bars, EMA(slow) has a valid (non-NaN)
    # latest value, so the result must be a real verdict, not "unknown".
    from bot import config

    bars = make_bars(rising_closes(config.EMA_TREND_SLOW_PERIOD))
    assert algorithm.compute_daily_trend(bars) == "up"


def test_one_below_slow_period_is_unknown():
    from bot import config

    bars = make_bars(rising_closes(config.EMA_TREND_SLOW_PERIOD - 1))
    assert algorithm.compute_daily_trend(bars) == "unknown"


def test_dataframe_without_close_column_is_unknown():
    df = pd.DataFrame({"open": [1.0, 2.0, 3.0]})  # has rows, no "close"
    assert algorithm.compute_daily_trend(df) == "unknown"
