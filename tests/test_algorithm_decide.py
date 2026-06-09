"""Tests for the BUY/SELL/HOLD decision rule (algorithm.decide).

Covers the looser entry: BUY fires on an oversold-ish pullback while the 5-min
EMA state is up (fast above slow), without needing a fresh crossover bar.
"""

from bot import algorithm, config


def _decide(rsi, fast, slow, fast_prev, slow_prev):
    return algorithm.decide(
        "TEST", rsi=rsi, ema_fast=fast, ema_slow=slow,
        ema_fast_prev=fast_prev, ema_slow_prev=slow_prev, price=100.0,
    )["signal"]


def test_buy_threshold_is_45():
    assert config.RSI_BUY_THRESHOLD == 45.0


def test_buy_fires_on_pullback_with_uptrend_state_no_fresh_cross():
    # Fast already above slow on the PRIOR bar (no fresh crossover), RSI < 45.
    assert _decide(40.0, 101.0, 100.0, 101.0, 100.0) == "BUY"


def test_buy_fires_exactly_on_a_fresh_cross_too():
    # A fresh cross-up still qualifies (it's a subset of "fast above slow").
    assert _decide(40.0, 101.0, 100.0, 99.0, 100.0) == "BUY"


def test_no_buy_when_momentum_is_down_even_if_oversold():
    # Deeply oversold but fast is BELOW slow (still falling) -> no buy.
    assert _decide(25.0, 99.0, 100.0, 99.0, 100.0) == "HOLD"


def test_no_buy_when_rsi_at_or_above_threshold():
    # Uptrend state but RSI not below 45 -> not a pullback entry.
    assert _decide(50.0, 101.0, 100.0, 101.0, 100.0) == "HOLD"


def test_sell_still_requires_overbought_plus_cross_down():
    # SELL unchanged: RSI > 65 and a fresh cross-down.
    assert _decide(70.0, 99.0, 100.0, 101.0, 100.0) == "SELL"


def test_overbought_without_cross_down_holds():
    assert _decide(70.0, 99.0, 100.0, 99.0, 100.0) == "HOLD"
