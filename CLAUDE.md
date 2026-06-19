# CLAUDE.md

Context for future Claude Code sessions working on this repository.

## Purpose

A terminal-based algorithmic **paper trading** bot. It connects to the Alpaca
Paper Trading API, runs a combined RSI + EMA-crossover strategy over a
watchlist, persists every trade and account snapshot to a local SQLite
database, renders a live `rich` dashboard, and uses the Anthropic Claude API to
generate trade commentary and answer free-form questions from the terminal.

**This is paper trading only.** All order flow targets Alpaca's paper endpoint
(`https://paper-api.alpaca.markets`). No real money is ever at risk.

## Stack

- **Python 3.10+**
- **alpaca-py** — Alpaca market data + trading SDK
- **anthropic** — Claude API SDK (model `claude-sonnet-4-20250514`)
- **rich** — live terminal UI
- **pandas / numpy** — data wrangling
- **ta** — technical indicators (RSI, EMA)
- **python-dotenv** — environment loading
- **schedule** — periodic trading-loop runner
- **SQLite** (stdlib `sqlite3`) — persistence

## Module map (`bot/`)

| Module | Responsibility |
| ------ | -------------- |
| `main.py` | Entry point. Loads env, inits DB, starts the trader loop on a background thread, launches the TUI on the main thread, handles clean shutdown. |
| `config.py` | Loads env vars via dotenv and exposes typed constants (keys, watchlist, interval, position sizing). |
| `alpaca_client.py` | Wraps alpaca-py: account, positions, bars (OHLCV DataFrame), latest price, market orders, order history. Retries with exponential backoff. |
| `algorithm.py` | Pure signal logic. `analyze(symbol, bars_df)` → `{signal, rsi, ema_fast, ema_slow, reason}`. |
| `trader.py` | Orchestrates a trading cycle: analyze each symbol, place/close orders within risk limits, record trades + AI commentary, snapshot the account. |
| `database.py` | SQLite schema + helpers: `record_trade`, `record_snapshot`, `get_trades`, `get_net_pnl`, `get_trade_stats`. |
| `ai_advisor.py` | Claude integration: `get_commentary(...)` for per-trade insight, `ask_advisor(...)` for streamed free-form Q&A. |
| `tui.py` | `rich` dashboard: header, account + positions, recent trades, AI commentary, advisor input bar. Refreshes every 5s. Used only with `--tui`. |
| `dashboard.py` (project root) | Streamlit browser dashboard (read-only over the SQLite DB + optional live Alpaca calls). Launched automatically by `main.py` unless `--headless`. |

## Run modes (main.py)

- Default (`python -m bot.main`): launches the Streamlit browser dashboard as a
  subprocess **and** streams a scrolling event log in the terminal.
- `--headless`: no browser dashboard; terminal scrolling log only.
- `--tui`: use the interactive full-screen `rich` TUI in the terminal instead of
  the scrolling log (combine with `--headless` for the old no-browser behavior).

## Stocks vs crypto

Watchlist symbols containing `/` (e.g. `BTC/USD`) are crypto: `alpaca_client`
routes them to the crypto data endpoints, uses `GTC` order TIF, and the trader
sizes them fractionally. `is_crypto_symbol()` is the single switch. "No
position" for crypto comes back as a bare 404 "Not Found" (vs stocks' "position
does not exist") — `get_position` treats both as flat.

## Data flow

```
schedule tick ─► trader.run_cycle()
                   ├─ alpaca_client.get_bars()      (market data)
                   ├─ algorithm.analyze()           (signal)
                   ├─ alpaca_client.place_market_order()  (execution)
                   ├─ ai_advisor.get_commentary()   (Claude insight)
                   └─ database.record_trade() / record_snapshot()

TUI (main thread) ─► reads database + alpaca_client every 5s ─► renders
                  └─ advisor prompt ─► ai_advisor.ask_advisor() (streamed)
```

## Strategy summary

Two-timeframe strategy:

**Trend filter (daily bars):** a stock is only eligible for BUYs when its
faster daily EMA is above its slower one — currently EMA(20) > EMA(50), a
medium-term trend tuned for this short-term style (looser than the classic
50/200 golden cross, so more setups fire). Periods are config constants
(`EMA_TREND_FAST_PERIOD` / `EMA_TREND_SLOW_PERIOD`). Computed once per day and
cached per symbol in the trader (`_warm_trend_cache_if_needed`). Individual
crypto symbols bypass this filter (the crypto proxy is the exception — see the
crypto-regime filter below). See `algorithm.compute_daily_trend`.

**Short-term timing (5-minute candles, last 50 bars):**
- **BUY**: `RSI(14) < 45` (pullback) **and** `EMA(9)` is **above** `EMA(21)`
  (short-term uptrend *state*, not a fresh cross) **and** the daily trend is not
  "down" **and** the market regime is not "down" (see below)
- **SELL**: `RSI(14) > 60` (overbought) — take profit on the bounce. (Stocks
  only; crypto ignores the signal exit.)
- **HOLD**: otherwise

History: the original BUY needed RSI<35 + a fresh crossover *event*, which fired
almost never. We briefly tried RSI<35 + EMA *state*, but backtesting on real
trade data showed RSI<35 (and even <40) fired on <1% of setups — near-zero
trading — while not improving the win rate. Settled on RSI<45 + EMA *state*,
relying on the intraday market-regime filter below to guard downtrends rather
than choking entries with a tight RSI.

SELL history: the SELL once also required a fresh EMA(9)-below-EMA(21) cross-down
*and* RSI>65. A 2-week analysis (2026-06-18) found that combo NEVER fired on
stocks — every stock exited at the EOD flatten, taking no intraday profit.
Backtesting exit variants on real 5-min bars, selling on overbought ALONE
captured profit ~4x better; the cross-down requirement was dropped and the
threshold lowered 65→60. (Held at 60, not lower: P&L kept rising as the threshold
dropped — an overfitting tell that lower = "sell on any tiny pop", which cuts
winners in a trending tape.)

**Crypto trading is DISABLED by default** (`CRYPTO_TRADING_ENABLED=False`, set
2026-06-18). The same 2-week analysis showed short-timeframe crypto loses to
buy-and-hold once the ~0.15–0.25%/side taker fee + slippage is paid (Alpaca paper
DOES simulate the crypto fee, so it transfers to live): net of cost over the
window, buy&hold +$65 vs the bot's RSI mean-reversion −$1,478. The identical
strategy is +EV on STOCKS, where execution is free — so the edge is real but only
where trading is free. With the switch off the bot opens no new crypto but still
exits any it holds (the book winds down to cash). Re-enable only with a plan for
the cost (e.g. limit/maker orders + far lower turnover).

**Market-regime filter (stocks only, `MARKET_REGIME_SYMBOL`, default "SPY"):**
no long stock buys while the broad-market proxy is **"red" intraday** — trading
*below its prior daily close*. This is an intraday check (live proxy price vs its
last settled daily close), so it reacts within the session to sharp broad-market
drops, unlike the slow daily-EMA trend. Recomputed once per cycle in
`trader._update_market_regime` (the proxy's prior close is cached per day); the
per-symbol BUY guard in `trader._process_symbol` just reads the resulting
`_market_regime_down` flag. The proxy does **not** need to be in the watchlist —
the trader fetches its bars/price directly. Crypto is exempt, and any missing
data or error **fails open** (trading allowed). Chosen over the earlier
daily-trend version after backtesting regime gates on real trade data: a daily
gate lagged the 2-day drops that caused the losses, while the intraday red-vs-
close gate caught them without strangling stock participation on up days.

**Crypto-regime filter (crypto only, `CRYPTO_REGIME_SYMBOL`, default "BTC/USD"):**
the SPY analog for the crypto book. Individual crypto previously bypassed every
trend guard; now a crypto BUY must clear **two** gates on the BTC proxy, each
independently toggleable (`CRYPTO_REGIME_USE_TREND` / `CRYPTO_REGIME_USE_INTRADAY`,
both default on):
- **Trend gate:** BTC daily `EMA20 > EMA50` (slow, multi-week). Reuses
  `compute_daily_trend` via the warm-up — so the proxy (and only the proxy) gets
  its real trend computed instead of "unknown"; it must be in `WATCHLIST`.
- **Intraday gate:** BTC trading at/above its prior daily **UTC** close (fast,
  within-session). Recomputed each cycle in `trader._update_crypto_regime` →
  `_crypto_regime_down`; reuses `_prior_daily_close`. Unlike the stock regime
  it is **not** gated on market hours (crypto is 24/7). "Prior close" is the
  midnight-UTC daily boundary, a clock convention, not a real market close.

Both gates **fail open** (missing data / error → trade allowed); empty
`CRYPTO_REGIME_SYMBOL` disables both. The BUY guard lives in the crypto branch
of `trader._process_symbol`. Rationale: individual alts are too volatile for
their own EMA cross, so BTC is used as the shared proxy (like SPY for stocks).

**Market-hours guard (stocks only):** no new stock trades in the last 15 minutes
of the session (`MARKET_BLACKOUT_MINUTES`) or while the market is closed; all
stock positions are flattened ~15 minutes before close to avoid overnight gap
risk. The OPENING pause is wider — `MARKET_OPEN_SKIP_MINUTES` (default 90, set
2026-06-18): a 2-week time-of-day analysis found entries in the first ~1.5h
(9:30–11:00 ET) net −$519 vs +$329 midday/afternoon, so the bot sits out the
volatile open. Effective opening pause = `max(MARKET_BLACKOUT_MINUTES,
MARKET_OPEN_SKIP_MINUTES)`; closing/flatten timing is unchanged. Crypto trades
24/7 (when enabled). Driven by Alpaca's `/clock` endpoint
(`alpaca_client.get_clock`, `trader._update_market_state`).

## Position sizing (`trader._open_long`)

Each BUY targets a dollar budget, `MAX_POSITION_SIZE_USD` (default $1000).
- **Crypto**: fractional quantity = `round(budget / price, 6)`.
- **Fractionable stocks**: a **notional** order spends the whole budget
  regardless of share price (`place_market_order(..., notional=budget)`).
  Fractionability is checked once per symbol via `alpaca_client.get_asset` and
  cached (`_fractionable_cache`); on lookup failure it falls back to whole shares.
- **Non-fractionable stocks**: whole shares = `floor(budget / price)`; the BUY
  is skipped if even one share exceeds the budget.

There is no global cap on concurrent positions — each BUY is sized independently.

## Conventions

- Keep `algorithm.py` pure and side-effect free so it's unit-testable.
- All secrets come from `.env` (gitignored); never hardcode keys.
- The DB lives at `data/trades.db` (gitignored, created at runtime).
- Run with `python -m bot.main` from the `trading-bot/` directory.

## Build status

Built in phases. See the phase list in the original spec. Each module's
docstring notes which phase implements it.
