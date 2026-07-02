"""Cross-sectional momentum ranking — pure signal logic for the weekly rotation.

Like ``algorithm.py`` this module is intentionally **pure**: it takes plain
price DataFrames and returns plain data, with no I/O, no Alpaca, no DB. That
keeps the strategy trivially unit-testable and matches the backtest exactly
(``bt_momentum.py`` / ``MOMENTUM_STRATEGY.md``).

The rule (locked 2026-07):
    * score each symbol by 12-1 momentum: return from ``lookback`` bars ago to
      ``skip`` bars ago (skipping the most recent week dodges short-term
      reversal) — i.e. ``close[-1-skip] / close[-1-lookback] - 1``.
    * a symbol is eligible only if its latest close is above its ``sma_window``
      simple moving average (absolute-trend filter — never hold a downtrend).
    * hold the top ``top_n`` eligible names by score.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def compute_scores(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    lookback: int = 126,
    skip: int = 5,
    sma_window: int = 200,
) -> dict[str, tuple[float, bool]]:
    """Return ``{symbol: (momentum, above_sma)}`` for every symbol with enough
    history. Symbols with fewer than ``max(lookback + 2, sma_window)`` bars (or
    bad data) are omitted."""
    need = max(lookback + 2, sma_window)
    out: dict[str, tuple[float, bool]] = {}
    for symbol, df in bars_by_symbol.items():
        if df is None or "close" not in getattr(df, "columns", []):
            continue
        close = df["close"].dropna()
        if len(close) < need:
            continue
        c = close.to_numpy(dtype=float)
        past = c[-1 - lookback]
        recent = c[-1 - skip]
        if past <= 0:
            continue
        momentum = recent / past - 1.0
        sma = c[-sma_window:].mean()
        out[symbol] = (momentum, bool(c[-1] > sma))
    return out


def select_top(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    lookback: int = 126,
    skip: int = 5,
    sma_window: int = 200,
    top_n: int = 15,
) -> list[str]:
    """Return the ``top_n`` symbols by momentum that are above their SMA,
    highest-momentum first."""
    scores = compute_scores(
        bars_by_symbol, lookback=lookback, skip=skip, sma_window=sma_window
    )
    eligible = [(sym, mom) for sym, (mom, above) in scores.items() if above]
    eligible.sort(key=lambda item: item[1], reverse=True)
    return [sym for sym, _ in eligible[:top_n]]
