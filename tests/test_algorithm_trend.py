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
