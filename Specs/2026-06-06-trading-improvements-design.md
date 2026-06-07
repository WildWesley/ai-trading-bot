# Trading Improvements Design

## Goal

Add three improvements to the trading bot: (1) a daily golden-cross trend filter that blocks BUY signals on downtrending stocks, (2) a market hours guard with opening/closing blackout windows and automatic end-of-day stock flattening, and (3) an expanded 100-symbol watchlist at a 5-minute cycle interval.

## Architecture

No new modules. All changes land in existing files, each of which already owns the right responsibility.

| File | Change |
|------|--------|
| `bot/config.py` | Add 5 new constants; update watchlist default to 100 symbols; change interval default to 300s |
| `bot/algorithm.py` | Add pure `compute_daily_trend(daily_bars_df)` function |
| `bot/alpaca_client.py` | Add `get_clock()` method |
| `bot/trader.py` | Add trend cache, market hours guard, daily stock flatten |

## Feature 1: Daily Trend Filter

### New constants in `config.py`

```python
EMA_TREND_FAST_PERIOD: int = 50    # daily EMA fast (golden cross)
EMA_TREND_SLOW_PERIOD: int = 200   # daily EMA slow
TREND_LOOKBACK_BARS: int = 365     # 1 year of daily bars
```

### New function in `algorithm.py`

```python
def compute_daily_trend(daily_bars_df: pd.DataFrame) -> str:
    """Return 'up', 'down', or 'unknown' based on EMA(50) vs EMA(200) on daily bars.

    'up'      = EMA(50) > EMA(200) on the latest bar (golden cross)
    'down'    = EMA(50) < EMA(200) (death cross)
    'unknown' = insufficient data (< 200 bars)
    """
```

Pure function, no I/O. Consistent with existing `algorithm.py` design.

### New state in `Trader`

```python
self._trend_cache: dict[str, str] = {}       # symbol -> "up"/"down"/"unknown"
self._trend_cache_date: str | None = None    # "YYYY-MM-DD" of last warm-up
```

### Warm-up flow

On the first `run_cycle()` call (and once per calendar day thereafter), `Trader._warm_trend_cache()`:
1. Fetches 365 daily bars per symbol via `client.get_bars(symbol, "1Day", 365)`
2. Calls `algorithm.compute_daily_trend(bars)` for each symbol
3. Stores result in `_trend_cache`
4. Logs progress (`"Warming trend cache: 45/100 symbols..."`)
5. On any per-symbol failure, stores `"unknown"` and continues

### Filter rule in `_process_symbol()`

```python
if signal == "BUY" and not has_position:
    if not is_crypto_symbol(symbol):   # crypto skips trend filter
        trend = self._trend_cache.get(symbol, "unknown")
        if trend == "down":
            self.log("debug", f"{symbol}: BUY skipped — daily trend bearish (death cross).")
            return
    self._open_long(symbol, analysis)
```

## Feature 2: Market Hours Guard

### New constant in `config.py`

```python
MARKET_BLACKOUT_MINUTES: int = 15
```

### New method in `AlpacaClient`

```python
def get_clock(self) -> dict[str, Any]:
    """Return market clock: is_open, next_open, next_close (UTC datetimes)."""
    clock = _with_backoff(self._trading.get_clock, what="get_clock")
    return {
        "is_open": bool(clock.is_open),
        "next_open": clock.next_open,
        "next_close": clock.next_close,
        "timestamp": clock.timestamp,
    }
```

### New state in `Trader`

```python
self._market_was_open: bool = False
self._market_opened_at: datetime | None = None
self._flattened_today: bool = False
```

### Market hours logic at top of `run_cycle()`

```python
clock = self.client.get_clock()
is_open = clock["is_open"]
now_utc = datetime.now(timezone.utc)

# Detect market open transition → reset flatten flag, record open time
if is_open and not self._market_was_open:
    self._market_opened_at = now_utc
    self._flattened_today = False
    self.log("info", "Market opened.")
self._market_was_open = is_open

# Compute blackout windows
in_opening_blackout = (
    is_open
    and self._market_opened_at is not None
    and (now_utc - self._market_opened_at).total_seconds()
    < config.MARKET_BLACKOUT_MINUTES * 60
)
next_close = clock["next_close"]
minutes_to_close = (next_close - now_utc).total_seconds() / 60
in_closing_blackout = is_open and minutes_to_close < config.MARKET_BLACKOUT_MINUTES

# Flatten stocks before close (once per day)
if in_closing_blackout and not self._flattened_today:
    self._flatten_stock_positions()
    self._flattened_today = True

stock_trading_allowed = (
    is_open and not in_opening_blackout and not in_closing_blackout
)
self._stock_trading_allowed = stock_trading_allowed
```

### `_flatten_stock_positions()` method

```python
def _flatten_stock_positions(self) -> None:
    self.log("info", "End-of-day: flattening all stock positions.")
    positions = self.client.get_positions()
    for pos in positions:
        symbol = pos["symbol"]
        if is_crypto_symbol(symbol):
            continue
        try:
            order = self.client.close_position(symbol)
            fill = order.get("filled_avg_price") or pos.get("current_price") or 0.0
            qty = pos.get("qty", 0)
            pnl = pos.get("unrealized_pl", 0.0)
            trade = {
                "symbol": symbol, "side": "sell", "qty": qty,
                "price": fill, "total_value": round(qty * fill, 2),
                "signal_reason": "End-of-day flatten (market close blackout).",
                "timestamp": _utc_now_iso(), "pnl_at_close": round(pnl, 2),
            }
            self.db.record_trade(trade)
            self.log("trade", f"EOD SELL {qty} {symbol} @ ${fill:.2f} (P&L ${pnl:+.2f}).")
        except Exception as exc:
            self.log("error", f"EOD flatten failed for {symbol}: {exc}")
```

### Guard in `_process_symbol()`

```python
if not is_crypto_symbol(symbol) and not self._stock_trading_allowed:
    return   # market closed or in blackout window
```

## Feature 3: Expanded Watchlist

### Updated defaults in `config.py`

```python
TRADE_INTERVAL_SECONDS: int = _get_int("TRADE_INTERVAL_SECONDS", 300)  # was 60

WATCHLIST: list[str] = _get_list("WATCHLIST", [
    # Large cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "NFLX", "AMD", "CRM",
    # Semiconductors
    "INTC", "QCOM", "AVGO", "MU", "TSM",
    # Finance
    "JPM", "BAC", "GS", "WFC", "MS", "BLK", "V", "MA", "AXP", "C", "SCHW", "PYPL",
    # Healthcare
    "JNJ", "UNH", "PFE", "ABBV", "LLY", "MRK", "ABT", "CVS", "ISRG", "VRTX",
    "AMGN", "GILD",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY",
    # Consumer
    "WMT", "COST", "PG", "KO", "PEP", "HD", "TGT", "MCD", "SBUX", "NKE", "LOW",
    # Industrials
    "BA", "CAT", "DE", "HON", "UPS", "GE", "LMT", "RTX",
    # Cloud / growth
    "SNOW", "PLTR", "UBER", "SHOP", "DDOG", "NOW", "ADBE", "ORCL", "ZM", "COIN",
    "SQ", "ABNB",
    # Auto
    "F", "GM",
    # Telecom / media
    "DIS", "CMCSA", "T", "VZ",
    # REITs
    "AMT", "PLD", "EQIX",
    # ETFs
    "SPY", "QQQ", "IWM", "GLD", "TLT", "XLF", "XLE", "XLV",
    # Crypto (24/7, no market hours guard)
    "BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "AVAX/USD", "LINK/USD", "UNI/USD",
])
```

Total: 100 symbols (93 stocks/ETFs + 7 crypto).

## Known Limitation

The dashboard Watchlist tab fetches prices for all symbols sequentially on each auto-refresh. At ~0.5s per symbol, 100 symbols takes ~50s per refresh. The tab will be sluggish. This is acceptable for now and can be optimized in a future pass (e.g., cached price polling, batch requests).

## Data Flow Summary

```
startup / new calendar day
  └─ Trader._warm_trend_cache()
       for each symbol: get_bars("1Day", 365) → compute_daily_trend() → _trend_cache

run_cycle() every 300s
  ├─ client.get_clock()
  │    ├─ market closed         → skip all stocks, crypto continues
  │    ├─ opening blackout      → skip stocks (< 15 min since open)
  │    └─ closing blackout      → flatten stocks + skip new stock trades
  └─ for each symbol:
       ├─ [stock, market closed/blackout] → skip
       ├─ get_bars("5Min", 50) → algorithm.analyze()
       ├─ BUY + no position:
       │    ├─ [stock] check _trend_cache → skip if "down"
       │    └─ place order, record trade, get AI commentary
       └─ SELL + position → close, record trade, get AI commentary
```

## Testing Approach

- `compute_daily_trend()` is pure — test directly with fabricated DataFrames (golden cross, death cross, insufficient data).
- `get_clock()` — test with a mock trading client.
- Market hours guard — test `_process_symbol()` with `_stock_trading_allowed = False`; verify stock symbols are skipped, crypto are not.
- Trend filter — test `_process_symbol()` with `_trend_cache = {"AAPL": "down"}`; verify BUY is skipped.
- `_flatten_stock_positions()` — test with mock positions; verify crypto positions are not closed.
