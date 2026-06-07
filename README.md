# AI Trading Bot

A terminal-based algorithmic **paper trading** bot. It connects to the Alpaca
Paper Trading API, runs a combined RSI + EMA-crossover strategy over a
watchlist, persists every trade and account snapshot to a local SQLite
database, renders a live `rich` dashboard, and uses the Anthropic Claude API to
generate trade commentary and answer free-form questions from the terminal.

> ⚠️ **Paper trading only.** All order flow targets Alpaca's paper endpoint
> (`https://paper-api.alpaca.markets`). No real money is ever at risk. This is
> an educational project, not financial advice.

---

## Features

- **Live market data + paper order execution** via [alpaca-py](https://github.com/alpacahq/alpaca-py)
- **Momentum/mean-reversion strategy**: RSI(14) + EMA(9)/EMA(21) crossover
- **SQLite persistence** of trades and account snapshots
- **Live `rich` dashboard**: account, open positions, recent trades, AI commentary
- **AI advisor** (Claude): automatic per-trade commentary + an interactive
  "ask Claude" prompt that streams answers token-by-token
- **Resilient**: API failures are logged to the UI instead of crashing; the bot
  even runs in display-only mode if Alpaca keys are absent

---

## Requirements

- Python 3.10+
- An Alpaca **paper trading** account (free)
- An Anthropic API key (optional — without it, AI commentary is disabled but the
  bot still trades and renders)

---

## Setup

### 1. Install dependencies

From the `trading-bot/` directory:

```bash
python -m pip install -r requirements.txt
```

(Use of a virtual environment is recommended:
`python -m venv .venv` then activate it before installing.)

### 2. Get Alpaca paper trading keys

1. Sign up at <https://alpaca.markets/> (free).
2. In the dashboard, switch to **Paper Trading** (toggle near the top-left).
3. Open **Home → API Keys** (or **Generate New Keys**) in the paper environment.
4. Copy the **API Key ID** and **Secret Key**. The secret is shown only once —
   save it now.

> Make sure you're generating keys in the **paper** environment, not live.

### 3. Get an Anthropic API key (optional)

Create one at <https://console.anthropic.com/> → **API Keys**. Without it the
AI commentary and advisor are simply disabled.

### 4. Configure `.env`

Copy the example and fill in your keys:

```bash
cp .env.example .env
```

Then edit `.env`:

```ini
ALPACA_API_KEY=your_paper_trading_key
ALPACA_SECRET_KEY=your_paper_trading_secret
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ANTHROPIC_API_KEY=your_anthropic_key
WATCHLIST=AAPL,MSFT,NVDA,TSLA,SPY
TRADE_INTERVAL_SECONDS=60
MAX_POSITION_SIZE_USD=1000
```

| Variable | Meaning | Default |
| --- | --- | --- |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | Paper trading credentials | — (required to trade) |
| `ALPACA_BASE_URL` | Paper endpoint (kept for reference; the client forces paper mode) | paper URL |
| `ANTHROPIC_API_KEY` | Claude API key | — (optional) |
| `CLAUDE_MODEL` | Claude model id | `claude-sonnet-4-20250514` |
| `WATCHLIST` | Comma-separated tickers. Stocks (`AAPL`) and crypto pairs (`BTC/USD`) can be mixed; anything with a `/` is routed to Alpaca's crypto endpoints automatically. | `AAPL,MSFT,NVDA,TSLA,SPY` |
| `TRADE_INTERVAL_SECONDS` | Seconds between trading cycles | `60` |
| `MAX_POSITION_SIZE_USD` | Max dollars per position | `1000` |

`.env` and the SQLite database (`data/trades.db`) are gitignored.

---

## Running

From the `trading-bot/` directory:

```bash
python -m bot.main
```

This does two things from one command:

1. **Opens a browser dashboard** at <http://localhost:8501> — a dark-themed,
   auto-refreshing view with two tabs:
   - **Overview** — account metrics, equity curve, open positions, recent
     trades, P&L stats, latest AI commentary.
   - **Watchlist** — a table of every watchlist symbol with its latest polled
     price (live from Alpaca on each refresh).
   - **Chart** — pick any watchlist symbol, a **bar interval** (5 min / hourly
     / daily) and a history range to chart price with EMA(9)/EMA(21) overlaid
     and ▲ BUY / ▼ SELL markers where the strategy fired, plus an RSI(14) panel
     with the oversold (35) / overbought (65) bands and the current signal. The
     interval sets the EMA horizon — on daily bars EMA(9)/EMA(21) are 9-/21-day
     EMAs. (This tab is manual-refresh; use the 🔄 button. Note the chart
     interval is independent of the live bot, which trades on 5-minute bars.)
2. **Streams a scrolling log** in the terminal — each cycle start/complete,
   every trade, and any errors, as they happen.

Stop with **Ctrl-C**. On shutdown the bot cancels any open orders, closes the
browser dashboard, and prints a session summary.

If Alpaca keys are missing, the bot starts in **display-only mode**: the
dashboard renders saved history but no live data is fetched and no trades occur.

### Run modes

| Command | Browser dashboard | Terminal |
| --- | --- | --- |
| `python -m bot.main` | ✅ opens | scrolling log |
| `python -m bot.main --headless` | ❌ skipped | scrolling log |
| `python -m bot.main --tui` | ✅ opens | interactive full-screen dashboard |
| `python -m bot.main --headless --tui` | ❌ skipped | interactive full-screen dashboard |

- **`--headless`** — don't launch the browser (e.g. running on a server, or you
  just want the log).
- **`--tui`** — use the original full-screen terminal dashboard, which includes
  the interactive **"Ask Claude"** prompt (type a question + Enter to stream an
  answer; needs an Anthropic key). Note: while you're at that prompt the
  terminal display pauses — the trading loop keeps running, and the browser
  dashboard (if open) keeps updating.

You can also run the browser dashboard on its own — e.g. to view saved history
while the bot is stopped:

```bash
streamlit run dashboard.py
```

Open positions and the live account line require Alpaca keys; everything else
renders from the database alone.

---

## The algorithm

Each cycle, for every symbol in the watchlist, the bot fetches the last 50
five-minute candles and computes three indicators with the
[`ta`](https://github.com/bukosabino/ta) library:

- **RSI(14)** — Relative Strength Index
- **EMA(9)** — fast exponential moving average
- **EMA(21)** — slow exponential moving average

It then applies a combined momentum/mean-reversion rule:

| Signal | Condition |
| --- | --- |
| **BUY** | `RSI < 35` (oversold) **and** EMA(9) crosses **above** EMA(21) |
| **SELL** | `RSI > 65` (overbought) **and** EMA(9) crosses **below** EMA(21) |
| **HOLD** | otherwise |

A "cross" means the fast EMA was on one side of the slow EMA on the previous bar
and is on the other side on the latest bar.

Execution rules:

- **BUY** fires only when there's no existing position; size is
  `floor(MAX_POSITION_SIZE_USD / price)` shares (skipped if that's < 1 share).
- **SELL** fires only when a position exists; it liquidates the position and
  records the realized P&L.
- After each trade the AI advisor is asked for a short commentary, stored on the
  trade row.
- After each cycle an account snapshot (equity/cash/buying power/position count)
  is recorded.

> These BUY/SELL conditions are deliberately strict (oversold **and** a bullish
> cross is a reversal-entry setup), so in practice signals are **rare** — expect
> mostly HOLD. That's by design.

---

## Project structure

```
trading-bot/
├── CLAUDE.md            # context for future Claude Code sessions
├── .env.example         # template for your .env
├── requirements.txt
├── bot/
│   ├── main.py          # entry point: DB init, scheduled loop, TUI, shutdown
│   ├── config.py        # env loading + typed constants
│   ├── alpaca_client.py # alpaca-py wrapper (account, bars, orders) w/ backoff
│   ├── algorithm.py     # pure RSI + EMA-crossover signal logic
│   ├── trader.py        # one trading cycle: analyze → trade → commentary → snapshot
│   ├── database.py      # SQLite schema + helpers
│   ├── ai_advisor.py    # Claude commentary + streamed Q&A
│   └── tui.py           # rich live dashboard
└── data/
    └── trades.db        # created at runtime (gitignored)
```

---

## Notes & disclaimer

- This bot is for **education and experimentation with paper trading only**. It
  is not investment advice, and nothing it (or the AI advisor) outputs should be
  treated as a recommendation to trade real securities.
- The strategy is intentionally simple and is **not** tuned for profitability.
- Market data and trading require an active Alpaca paper account; outside US
  market hours, bar data may be limited and few/no signals will fire.

## Running on a Raspberry Pi (24/7)

To run the bot continuously on a Raspberry Pi with a public, read-only
dashboard accessible from anywhere, see [`deploy/README.md`](deploy/README.md).
It covers the one-shot setup script, systemd services, and a Cloudflare
Tunnel for remote access.
