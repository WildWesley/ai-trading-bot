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

**Long-term filter (daily bars, 365-day lookback):** a stock is only eligible
for BUYs when EMA(50) > EMA(200) (golden cross). Computed once per day and
cached per symbol in the trader (`_warm_trend_cache_if_needed`). Crypto bypasses
this filter. See `algorithm.compute_daily_trend`.

**Short-term timing (5-minute candles, last 50 bars):**
- **BUY**: `RSI(14) < 35` **and** `EMA(9)` crosses above `EMA(21)` **and** the
  daily trend is not "down"
- **SELL**: `RSI(14) > 65` **and** `EMA(9)` crosses below `EMA(21)`
- **HOLD**: otherwise

**Market-hours guard (stocks only):** no new stock trades in the first/last 15
minutes of the session (`MARKET_BLACKOUT_MINUTES`) or while the market is
closed; all stock positions are flattened ~15 minutes before close to avoid
overnight gap risk. Crypto trades 24/7. Driven by Alpaca's `/clock` endpoint
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
