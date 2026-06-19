"""Combined RSI + EMA-crossover signal generation.

This module is intentionally **pure**: ``analyze`` takes a price DataFrame and
returns a plain dict. It performs no I/O and has no dependencies on Alpaca,
the database, or network access, which keeps it trivially unit-testable.

Strategy (per symbol, on 5-minute candles):
    BUY  : RSI(14) < RSI_BUY_THRESHOLD  AND EMA(9) above EMA(21) (uptrend state)
    SELL : RSI(14) > RSI_SELL_THRESHOLD (overbought) — take profit on the bounce
    HOLD : otherwise

The SELL previously also required a fresh EMA(9)-below-EMA(21) cross-down on the
same bar. Backtesting ~2 weeks of real 5-min data showed that combined condition
almost never fired (it's contradictory — "just overbought" means still rallying,
not rolling over), so stock positions only ever exited at the end-of-day flatten
and never took intraday profit. Selling on overbought ALONE captured profit far
more consistently, so the cross-down requirement was dropped and the threshold
lowered (65 -> 60). The cross is still computed for the HOLD diagnostic text.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator

from . import config

Signal = str  # "BUY" | "SELL" | "HOLD"


def _round(value: float | None, ndigits: int = 2) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), ndigits)


def _result(
    signal: Signal,
    *,
    rsi: float | None = None,
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    reason: str = "",
    price: float | None = None,
) -> dict[str, Any]:
    return {
        "signal": signal,
        "rsi": _round(rsi),
        "ema_fast": _round(ema_fast),
        "ema_slow": _round(ema_slow),
        "price": _round(price),
        "reason": reason,
    }


def indicator_frame(bars_df: pd.DataFrame) -> pd.DataFrame:
    """Return close + EMA(fast/slow) + RSI as full series, aligned to ``bars_df``.

    Same indicators and windows the bot trades on (see ``analyze``), but the
    *entire* series rather than just the latest value — for charting. The
    original index (timestamps) is preserved so callers can plot against time.
    Warm-up rows are left as NaN. Returns an empty frame when there's no usable
    ``close`` column.
    """
    cols = ["close", "ema_fast", "ema_slow", "rsi"]
    if bars_df is None or "close" not in bars_df.columns or bars_df.empty:
        return pd.DataFrame(columns=cols)
    close = bars_df["close"].astype(float)
    ema_fast = EMAIndicator(
        close=close, window=config.EMA_FAST_PERIOD
    ).ema_indicator()
    ema_slow = EMAIndicator(
        close=close, window=config.EMA_SLOW_PERIOD
    ).ema_indicator()
    rsi = RSIIndicator(close=close, window=config.RSI_PERIOD).rsi()
    return pd.DataFrame(
        {"close": close, "ema_fast": ema_fast, "ema_slow": ema_slow, "rsi": rsi},
        index=bars_df.index,
    )


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
        or "close" not in daily_bars_df.columns
        or len(daily_bars_df) < slow
    ):
        return "unknown"

    close = daily_bars_df["close"].astype(float).reset_index(drop=True)
    ema_fast = EMAIndicator(close=close, window=fast).ema_indicator().iloc[-1]
    ema_slow = EMAIndicator(close=close, window=slow).ema_indicator().iloc[-1]
    if math.isnan(ema_fast) or math.isnan(ema_slow):
        return "unknown"
    return "up" if ema_fast > ema_slow else "down"


def signal_series(ind: pd.DataFrame, symbol: str = "") -> pd.Series:
    """Compute the BUY/SELL/HOLD signal at *every* bar of an indicator frame.

    ``ind`` is the output of ``indicator_frame``. Uses the same ``decide`` rule
    the bot trades on, evaluated bar-by-bar (each bar compared with the prior
    bar for the EMA crossover). The first bar and any warm-up rows with NaN
    indicators come back as ``None``. The returned Series shares ``ind``'s
    index, so it can be joined straight back onto the chart data.
    """
    needed = {"ema_fast", "ema_slow", "rsi"}
    if ind is None or ind.empty or not needed.issubset(ind.columns):
        return pd.Series([], dtype=object)

    ema_fast = ind["ema_fast"].to_numpy()
    ema_slow = ind["ema_slow"].to_numpy()
    rsi = ind["rsi"].to_numpy()
    out: list[str | None] = [None] * len(ind)
    for i in range(1, len(ind)):
        window = (
            rsi[i],
            ema_fast[i],
            ema_slow[i],
            ema_fast[i - 1],
            ema_slow[i - 1],
        )
        if any(math.isnan(v) for v in window):
            continue
        out[i] = decide(
            symbol,
            rsi=rsi[i],
            ema_fast=ema_fast[i],
            ema_slow=ema_slow[i],
            ema_fast_prev=ema_fast[i - 1],
            ema_slow_prev=ema_slow[i - 1],
        )["signal"]
    return pd.Series(out, index=ind.index, dtype=object)


def analyze(symbol: str, bars_df: pd.DataFrame) -> dict[str, Any]:
    """Analyze a symbol's recent bars and return a signal dict.

    Parameters
    ----------
    symbol:
        Ticker, used only for human-readable ``reason`` strings.
    bars_df:
        DataFrame with at least a ``close`` column, indexed/ordered oldest to
        newest. Typically the output of ``AlpacaClient.get_bars``.

    Returns
    -------
    dict with keys: ``signal`` (BUY/SELL/HOLD), ``rsi``, ``ema_fast``,
    ``ema_slow``, ``price``, ``reason``.
    """
    rsi_period = config.RSI_PERIOD
    fast_period = config.EMA_FAST_PERIOD
    slow_period = config.EMA_SLOW_PERIOD
    buy_rsi = config.RSI_BUY_THRESHOLD
    sell_rsi = config.RSI_SELL_THRESHOLD

    # Need at least enough bars to compute the slow EMA / RSI plus one prior
    # bar to detect a crossover.
    min_bars = max(slow_period, rsi_period) + 1
    if bars_df is None or "close" not in bars_df.columns:
        return _result(
            "HOLD",
            reason=f"{symbol}: no price data available.",
        )
    if len(bars_df) < min_bars:
        return _result(
            "HOLD",
            reason=(
                f"{symbol}: insufficient data "
                f"({len(bars_df)}/{min_bars} bars needed)."
            ),
        )

    close = bars_df["close"].astype(float).reset_index(drop=True)

    rsi_series = RSIIndicator(close=close, window=rsi_period).rsi()
    ema_fast_series = EMAIndicator(
        close=close, window=fast_period
    ).ema_indicator()
    ema_slow_series = EMAIndicator(
        close=close, window=slow_period
    ).ema_indicator()

    rsi = float(rsi_series.iloc[-1])
    ema_fast = float(ema_fast_series.iloc[-1])
    ema_slow = float(ema_slow_series.iloc[-1])
    ema_fast_prev = float(ema_fast_series.iloc[-2])
    ema_slow_prev = float(ema_slow_series.iloc[-2])
    price = float(close.iloc[-1])

    # Guard against NaNs in the indicator warm-up region.
    if any(
        math.isnan(v)
        for v in (rsi, ema_fast, ema_slow, ema_fast_prev, ema_slow_prev)
    ):
        return _result(
            "HOLD",
            rsi=rsi,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            price=price,
            reason=f"{symbol}: indicators still warming up.",
        )

    return decide(
        symbol,
        rsi=rsi,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        ema_fast_prev=ema_fast_prev,
        ema_slow_prev=ema_slow_prev,
        price=price,
    )


def decide(
    symbol: str,
    *,
    rsi: float,
    ema_fast: float,
    ema_slow: float,
    ema_fast_prev: float,
    ema_slow_prev: float,
    price: float | None = None,
) -> dict[str, Any]:
    """Pure decision rule, separated from indicator computation.

    Given the latest and previous EMA values plus the latest RSI, return the
    signal dict. Kept independent of pandas/ta so the BUY/SELL/HOLD branches
    can be unit-tested directly without fabricating qualifying price series.
    """
    fast_period = config.EMA_FAST_PERIOD
    slow_period = config.EMA_SLOW_PERIOD
    buy_rsi = config.RSI_BUY_THRESHOLD
    sell_rsi = config.RSI_SELL_THRESHOLD

    crossed_up = ema_fast_prev <= ema_slow_prev and ema_fast > ema_slow
    crossed_down = ema_fast_prev >= ema_slow_prev and ema_fast < ema_slow
    # BUY uses the EMA *state* (fast above slow = short-term uptrend) rather than
    # a fresh crossover *event*, so an oversold pullback in an up-trending name
    # qualifies without having to land on the exact crossover bar.
    momentum_up = ema_fast > ema_slow

    common = dict(rsi=rsi, ema_fast=ema_fast, ema_slow=ema_slow, price=price)

    if rsi < buy_rsi and momentum_up:
        return _result(
            "BUY",
            reason=(
                f"{symbol}: BUY — RSI {rsi:.1f} < {buy_rsi:.0f} (pullback) and "
                f"EMA{fast_period} ({ema_fast:.2f}) above "
                f"EMA{slow_period} ({ema_slow:.2f}) (short-term uptrend)."
            ),
            **common,
        )

    # SELL on overbought ALONE (no EMA cross-down co-requirement). The old
    # combined rule was effectively inert — see the module docstring. Crypto
    # ignores this signal exit entirely (it uses _maybe_close_crypto); in
    # practice only stocks act on SELL (see trader._process_symbol).
    if rsi > sell_rsi:
        return _result(
            "SELL",
            reason=(
                f"{symbol}: SELL — RSI {rsi:.1f} > {sell_rsi:.0f} (overbought); "
                f"take profit on the bounce."
            ),
            **common,
        )

    # HOLD — explain which condition(s) failed for transparency.
    trend = "bullish" if ema_fast > ema_slow else "bearish"
    cross_note = (
        "fast crossed above slow"
        if crossed_up
        else "fast crossed below slow"
        if crossed_down
        else "no EMA crossover"
    )
    return _result(
        "HOLD",
        reason=(
            f"{symbol}: HOLD — RSI {rsi:.1f}, EMA{fast_period} {ema_fast:.2f} "
            f"vs EMA{slow_period} {ema_slow:.2f} ({trend}); {cross_note}."
        ),
        **common,
    )
