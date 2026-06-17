# Crypto-regime gate (BTC) — design

**Date:** 2026-06-17
**Status:** Approved (pending implementation plan)

## Problem

Crypto longs currently bypass *every* trend protection the bot has. A crypto
BUY fires on only the 5-minute entry signal (`RSI(14) < 45` and `EMA9 > EMA21`)
with no macro context: `_warm_trend_cache_if_needed` force-records every crypto
symbol as `"unknown"`, and the regime/trend guards in `_process_symbol` live
inside `if not is_crypto_symbol(symbol):`. The 5-minute entry is a *pullback*
signal, so without a trend gate the bot buys oversold dips in coins that may be
in full downtrends — catching falling knives. This caused real losses on
2026-06-16.

Stocks, by contrast, have two gates: an intraday **red-vs-prior-close** market
regime check on SPY (`_update_market_regime`) and a per-symbol **daily EMA20/50
trend** check. Crypto has neither.

## Goal

Give the crypto book the same kind of macro protection stocks have, using
**Bitcoin** as the crypto-market proxy (the SPY analog — BTC is the most liquid,
least volatile crypto, and alts track it). Add **two independent gates** on BTC,
both consulted for every crypto BUY:

1. **Trend gate** — BTC daily `EMA20 > EMA50` must hold (slow, multi-week regime;
   reuses `algorithm.compute_daily_trend`).
2. **Intraday gate** — BTC must be trading **at or above its prior daily (UTC)
   close** (fast, within-session; reuses the stock `_prior_daily_close` path).

Both default ON (protection-first after the losses). Each is independently
toggleable so the new backtest scripts + incoming `trades.db` can decide which
combination actually helps.

## Non-goals

- Per-coin daily trend filter (each alt's own EMA20/50). Deliberately excluded —
  thin alts whipsaw; BTC is the stable proxy. This is the SPY-style *shared*
  gate, not a per-symbol one.
- Hysteresis/buffer band on the EMA cross. Start with the plain hard cross.
- Rolling-24h reference for the intraday gate (see "Prior close" below).
- Any SELL-side or position-sizing changes.

## What "prior daily close" means for crypto

`_prior_daily_close(symbol)` fetches daily bars and returns the close of the most
recent candle **dated before today (UTC)**. Alpaca rolls crypto daily bars at
**00:00 UTC**, so the reference price is BTC's level at midnight UTC — a fixed
clock boundary, *not* a rolling 24h-ago window and *not* a real market close
(crypto never closes). This is an accepted approximation: it reuses the exact,
tested code stocks use with zero new data plumbing. A true rolling-24h reference
is a possible future refinement, explicitly out of scope here.

## Design

### 1. Config (`bot/config.py`)

Add next to `MARKET_REGIME_SYMBOL`:

```python
# Crypto-regime filter. Gates new long *crypto* entries on a single proxy
# (CRYPTO_REGIME_SYMBOL, default BTC/USD) — the SPY analog for the crypto book.
# Two independent gates, both consulted for every crypto BUY:
#   - trend gate:    proxy daily EMA20 > EMA50 (slow, multi-week regime)
#   - intraday gate: proxy trades at/above its prior daily (UTC) close
# Empty CRYPTO_REGIME_SYMBOL disables BOTH. Each gate has its own toggle.
# The trend gate needs the proxy in WATCHLIST (its trend is computed in the
# warm-up loop); the intraday gate fetches the proxy directly. Any missing data
# or error fails OPEN (crypto trading allowed).
CRYPTO_REGIME_SYMBOL: str = "BTC/USD"
CRYPTO_REGIME_USE_TREND: bool = True
CRYPTO_REGIME_USE_INTRADAY: bool = True
```

Optional (nice-to-have) `validate()` note: if `CRYPTO_REGIME_SYMBOL` is set and
`CRYPTO_REGIME_USE_TREND` is True but the symbol is not in `WATCHLIST`, append a
warning that the trend gate will silently fail open. Not fatal.

### 2. Trader state (`Trader.__init__`)

Mirror the stock regime fields:

```python
self._crypto_regime_down: bool = False
self._crypto_regime_prior_close: float | None = None
self._crypto_regime_prior_close_date: str | None = None
```

### 3. Trend-cache warm-up (`_warm_trend_cache_if_needed`)

Today: every crypto symbol → `"unknown"`. Change so the crypto regime symbol's
trend is actually computed (when the trend gate is enabled), via the same
`get_bars(sym, "1Day", TREND_LOOKBACK_BARS)` → `compute_daily_trend` path stocks
use (`get_bars` already routes `BTC/USD` to the crypto daily endpoint). All other
crypto stays `"unknown"`:

```python
is_regime = symbol == config.CRYPTO_REGIME_SYMBOL
skip_as_crypto = is_crypto_symbol(symbol) and not (
    is_regime and config.CRYPTO_REGIME_USE_TREND
)
if skip_as_crypto:
    new_cache[symbol] = "unknown"
    continue
# ... existing get_bars + compute_daily_trend path unchanged ...
```

### 4. Intraday crypto regime (`_update_crypto_regime`, new)

Parallel to `_update_market_regime`, but **not** gated on
`self._stock_trading_allowed` (crypto trades 24/7). Reuses `_prior_daily_close`
and `get_latest_price` as-is:

```python
def _update_crypto_regime(self) -> None:
    """Set self._crypto_regime_down from an intraday red-vs-prior-close check
    on the crypto proxy (CRYPTO_REGIME_SYMBOL). Fails OPEN. Prior close cached
    per UTC day. Not gated on market hours — crypto trades 24/7."""
    self._crypto_regime_down = False
    sym = config.CRYPTO_REGIME_SYMBOL
    if not sym or not config.CRYPTO_REGIME_USE_INTRADAY:
        return
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        if self._crypto_regime_prior_close_date != today:
            self._crypto_regime_prior_close = self._prior_daily_close(sym)
            self._crypto_regime_prior_close_date = today
        prior = self._crypto_regime_prior_close
        if prior is None:
            return
        live = self.client.get_latest_price(sym)
        if not live or live <= 0:
            return
        if live < prior:
            self._crypto_regime_down = True
            pct = (live / prior - 1) * 100
            self.log("info", f"Crypto regime risk-off: {sym} {live:.2f} below "
                     f"prior close {prior:.2f} ({pct:+.2f}%); pausing crypto buys.")
    except Exception as exc:  # noqa: BLE001 - never crash the cycle
        self.log("error", f"crypto-regime check failed (allowing trades): {exc}")
```

Call it in `run_cycle` right after `self._update_market_regime()`.

### 5. BUY gate (`_process_symbol`)

The existing stock gates live in `if not is_crypto_symbol(symbol):`, and
`self._open_long(...)` is reached after that block. Add an `else:` (crypto)
branch so both paths fall through to `_open_long`:

```python
if signal == "BUY" and not has_position:
    if not is_crypto_symbol(symbol):
        # ... existing SPY red-intraday + per-symbol daily-trend gates ...
    else:
        sym = config.CRYPTO_REGIME_SYMBOL
        if sym and config.CRYPTO_REGIME_USE_TREND \
                and self._trend_cache.get(sym) == "down":
            self.log("debug", f"{symbol}: BUY skipped — crypto regime "
                     f"({sym}) daily trend bearish.")
            return
        if sym and config.CRYPTO_REGIME_USE_INTRADAY and self._crypto_regime_down:
            self.log("debug", f"{symbol}: BUY skipped — crypto regime "
                     f"({sym}) red intraday.")
            return
    self._open_long(symbol, analysis)
```

## Fail-open contract (matches the stock gate)

- Trend gate blocks only on exactly `"down"`. `"up"`, `"unknown"`, or a cache
  miss all allow.
- Intraday gate keys off `_crypto_regime_down`, which defaults `False` and is
  reset to `False` at the top of every `_update_crypto_regime` (missing prior
  close, unavailable live price, or any exception → allow).
- `CRYPTO_REGIME_SYMBOL = ""` disables both gates entirely.
- The proxy gates itself too: when evaluating a BTC BUY, BTC's own
  down-trend/red-day blocks it — intended.

## Testing (`tests/test_trader_crypto_regime.py`, new)

Mirror `tests/test_trader_regime.py` structure (FakeClient/FakeDb fixtures,
`make_bars`, monkeypatched `bot.algorithm.analyze` forcing a BUY). Monkeypatch
the `bot.config` flags per test.

**BUY-gate behaviour:**
- trend `"down"` (USE_TREND on) → crypto BUY blocked.
- `_crypto_regime_down=True` (USE_INTRADAY on) → crypto BUY blocked.
- trend `"up"` and not red → crypto BUY allowed (one buy recorded).
- fail-open: trend `"unknown"` and not red → allowed.
- USE_TREND off → trend `"down"` does **not** block.
- USE_INTRADAY off → `_crypto_regime_down=True` does **not** block.
- `CRYPTO_REGIME_SYMBOL=""` → neither gate blocks.

**`_update_crypto_regime` computation:**
- BTC live < prior close → `_crypto_regime_down=True`.
- BTC live ≥ prior close → `False` (and clears a stale `True`).
- prior close `None` / live price 0 → fails open (`False`).
- computes even when `_stock_trading_allowed=False` (crypto is 24/7) — this is
  the key divergence from the stock regime test.

**Warm-up:**
- with USE_TREND on, `BTC/USD` gets a real `"up"`/`"down"` verdict (not forced
  `"unknown"`); a non-proxy crypto (e.g. `ETH/USD`) stays `"unknown"`.

**Regression:** existing `test_trader_regime.py` stays green — its crypto case
uses trend `"unknown"` and an unset `_crypto_regime_down`, so the new gates fail
open and the crypto buy still goes through.

## Docs

Update `CLAUDE.md` strategy section to document the crypto-regime gates
alongside the existing SPY description. No `.env` change (these are code
constants, like `MARKET_REGIME_SYMBOL`).
