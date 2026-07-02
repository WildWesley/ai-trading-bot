"""Loads environment variables and exposes them as typed constants.

All configuration comes from the environment (typically populated from a local
`.env` file via python-dotenv). Sensible defaults are provided for everything
except secrets so the module imports cleanly even when keys are absent — the
absence of keys is surfaced explicitly via ``validate()`` rather than at import
time, which keeps the module testable.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a hard runtime dependency
    # Allows the module (and unit tests) to import even if python-dotenv is
    # not installed yet. Real env vars are still read via os.getenv.
    def load_dotenv(*_args, **_kwargs) -> bool:  # type: ignore[misc]
        return False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# config.py lives at: trading-bot/bot/config.py
#   parents[0] -> bot/
#   parents[1] -> trading-bot/   (project root)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "trades.db"
ENV_PATH = PROJECT_ROOT / ".env"

# Load .env from the project root if present. Real environment variables take
# precedence over .env values (override=False).
load_dotenv(dotenv_path=ENV_PATH, override=False)


# ---------------------------------------------------------------------------
# Typed env helpers
# ---------------------------------------------------------------------------
def _get_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value is not None and value.strip() else default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _get_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Alpaca (paper trading only)
# ---------------------------------------------------------------------------
ALPACA_API_KEY: str = _get_str("ALPACA_API_KEY")
ALPACA_SECRET_KEY: str = _get_str("ALPACA_SECRET_KEY")
ALPACA_BASE_URL: str = _get_str(
    "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
)

# ---------------------------------------------------------------------------
# Anthropic / Claude
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY: str = _get_str("ANTHROPIC_API_KEY")
CLAUDE_MODEL: str = _get_str("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ---------------------------------------------------------------------------
# Trading parameters
# ---------------------------------------------------------------------------
WATCHLIST: list[str] = _get_list(
    "WATCHLIST",
    [
        # Large cap tech
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "NFLX",
        "AMD", "CRM",
        # Semiconductors
        "INTC", "QCOM", "AVGO", "MU", "TSM",
        # Finance
        "JPM", "BAC", "GS", "WFC", "MS", "BLK", "V", "MA", "AXP", "C",
        "SCHW", "PYPL",
        # Healthcare
        "JNJ", "UNH", "PFE", "ABBV", "LLY", "MRK", "ABT", "CVS", "ISRG",
        "VRTX", "AMGN", "GILD",
        # Energy
        "XOM", "CVX", "COP", "SLB", "EOG", "OXY",
        # Consumer
        "WMT", "COST", "PG", "KO", "PEP", "HD", "TGT", "MCD", "SBUX",
        "NKE", "LOW",
        # Industrials
        "BA", "CAT", "DE", "HON", "UPS", "GE", "LMT", "RTX",
        # Cloud / growth
        "SNOW", "PLTR", "UBER", "SHOP", "DDOG", "NOW", "ADBE", "ORCL",
        "ZM", "COIN", "SQ", "ABNB",
        # Auto
        "F", "GM",
        # Telecom / media
        "DIS", "CMCSA", "T", "VZ", "TMUS",
        # REITs
        "AMT", "PLD", "EQIX",
        # ETFs
        "SPY", "QQQ", "IWM", "GLD", "TLT", "XLF", "XLE", "XLV",
        # Expanded coverage — more tech / semis
        "CSCO", "IBM", "TXN", "AMAT", "LRCX", "ADI", "MRVL", "PANW",
        "CRWD", "ANET", "DELL", "INTU",
        # Expanded coverage — more finance
        "USB", "PNC", "COF", "SPGI", "CME", "ICE", "PGR", "CB",
        # Expanded coverage — more healthcare
        "TMO", "DHR", "BMY", "MDT", "SYK", "BSX", "ELV", "CI", "REGN", "ZTS",
        # Expanded coverage — more consumer
        "PM", "MO", "MDLZ", "CL", "EL", "DG", "TJX", "CMG", "BKNG", "MAR",
        # Expanded coverage — more industrials / energy / utilities
        "MMM", "ETN", "ITW", "NSC", "CSX", "FDX", "MPC", "VLO", "NEE", "SO",
        # Wave 2 — more tech / semis / software
        "GOOG", "KLAC", "NXPI", "MCHP", "ON", "MPWR", "SWKS", "TER", "ASML",
        "ARM", "SMCI", "WDC", "STX", "HPE", "JNPR", "FFIV", "AKAM", "CDW",
        "ZS", "FTNT", "NET", "OKTA", "TWLO", "DOCU", "HUBS",
        # Wave 2 — more finance
        "MET", "PRU", "AON", "MMC", "TRV", "ALL", "AFL", "FIS", "GPN", "DFS",
        "SYF", "FITB", "HBAN", "NTRS", "BK",
        # Wave 2 — more healthcare
        "BIIB", "MRNA", "IDXX", "IQV", "GEHC", "RMD", "DXCM", "ZBH", "BDX",
        "HUM", "CNC", "MCK",
        # Wave 2 — more consumer
        "KHC", "GIS", "HSY", "STZ", "KDP", "KR", "SYY", "MNST", "CHD", "CLX",
        "ORLY", "AZO", "ULTA", "LULU", "YUM",
        # Wave 2 — more industrials / energy / utilities
        "UNP", "GD", "NOC", "ROK", "PCAR", "CMI", "URI", "FAST", "PAYX",
        "ADP", "WM", "CTAS", "GWW", "OKE", "D",
        # Wave 2 — more ETFs
        "DIA", "VTI", "XLK", "XLY", "XLI", "XLP", "XLU", "SMH",
        # Wave 3 (2026-06-18) — +91 liquid names to 325 stocks. Focus is the
        # free, liquid stock book (active crypto is disabled); all validated
        # tradable on Alpaca at add time.
        # software / internet
        "SNAP", "PINS", "RBLX", "DASH", "ROKU", "SPOT", "TEAM", "MDB", "SNPS",
        "CDNS", "WDAY", "ADSK", "DOCN", "GTLB", "S", "PATH", "BILL", "DT",
        "PAYC", "MELI", "SE", "TTD", "EA", "TTWO", "APP",
        # semiconductors
        "GFS", "ENTG", "QRVO", "LSCC", "AMKR", "OLED", "COHR", "RMBS", "SLAB",
        # finance / fintech
        "HOOD", "SOFI", "AFRM", "NU", "TOST", "FOUR", "ALLY", "KEY", "RF",
        "CFG", "MTB", "CBOE", "NDAQ", "TROW", "RJF", "STT", "HIG", "BRO",
        "ACGL", "NWG", "ZION",
        # healthcare
        "HCA", "DVA", "CAH", "COR", "A", "WAT", "MTD", "ALNY", "BMRN", "INCY",
        "PODD", "ALGN", "DGX", "LH", "WST", "BAX", "COO", "MOH",
        # consumer / retail / autos
        "DKNG", "DPZ", "WING", "TXRH", "DRI", "CAVA", "DECK", "CROX", "ONON",
        "ROST", "DLTR", "FIVE", "WSM", "BBY", "TSCO", "GPC", "KMX", "RIVN",
        # Crypto (24/7 — bypass the market-hours guard and trend filter)
        "BTC/USD", "ETH/USD", "SOL/USD", "LTC/USD", "AVAX/USD",
        "LINK/USD", "UNI/USD", "AAVE/USD", "BCH/USD", "DOT/USD",
        "CRV/USD", "XTZ/USD", "GRT/USD", "SUSHI/USD",
        "YFI/USD", "BAT/USD",
    ],
)
TRADE_INTERVAL_SECONDS: int = _get_int("TRADE_INTERVAL_SECONDS", 300)
MAX_POSITION_SIZE_USD: float = _get_float("MAX_POSITION_SIZE_USD", 1000.0)

# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------
# Which strategy the trader runs each cycle:
#   "momentum" — weekly concentrated cross-sectional momentum rotation (the
#                2026-07 flagship; see MOMENTUM_STRATEGY.md). RECOMMENDED default.
#   "rsi"      — the legacy 5-minute RSI+EMA intraday strategy.
# Takes effect on the next process start. Set STRATEGY=rsi in .env to revert.
STRATEGY: str = _get_str("STRATEGY", "momentum").lower()

# Momentum rotation parameters (LOCKED — see MOMENTUM_STRATEGY.md). The universe
# is the non-crypto WATCHLIST minus the SPY/QQQ benchmarks. Each weekly
# rebalance holds the top-N names by 12-1 momentum that are above their 200-day
# SMA, equal weight; a name is sold when it drops out of the top-N or below its
# 200-day SMA. Winners already held are left to run (low turnover). No leverage,
# no market-timing filter, no crypto — those were tested and rejected.
MOMENTUM_TOP_N: int = _get_int("MOMENTUM_TOP_N", 15)
MOMENTUM_LOOKBACK_DAYS: int = _get_int("MOMENTUM_LOOKBACK_DAYS", 126)  # ~6 months
MOMENTUM_SKIP_DAYS: int = _get_int("MOMENTUM_SKIP_DAYS", 5)            # skip last week
MOMENTUM_SMA_WINDOW: int = _get_int("MOMENTUM_SMA_WINDOW", 200)        # trend filter
MOMENTUM_BARS_LOOKBACK: int = _get_int("MOMENTUM_BARS_LOOKBACK", 260)  # daily bars/symbol
# Weekly rebalance cadence: 0=Mon .. 4=Fri. The rotation rebalances once per ISO
# week, on the first market-open cycle on/after this weekday.
MOMENTUM_REBALANCE_WEEKDAY: int = _get_int("MOMENTUM_REBALANCE_WEEKDAY", 0)

# ---------------------------------------------------------------------------
# Strategy constants (per the spec)
# ---------------------------------------------------------------------------
BARS_LOOKBACK: int = 50          # number of candles to fetch per analysis
BAR_TIMEFRAME_MINUTES: int = 5   # 5-minute candles
RSI_PERIOD: int = 14
EMA_FAST_PERIOD: int = 9
EMA_SLOW_PERIOD: int = 21
RSI_BUY_THRESHOLD: float = 45.0   # buy pullbacks; regime filter guards downtrends
# SELL fires on overbought ALONE (no EMA cross-down co-requirement — see
# algorithm.decide). Lowered 65 -> 60 after backtesting ~2 wk of real 5-min data:
# the old RSI>65 + cross-down combo never fired, so stocks only exited at the EOD
# flatten and never took intraday profit. RSI>60-alone captured profit ~4x better
# in-sample; kept at 60 (NOT lower) — the gains kept rising toward "sell on any
# tiny pop", an overfitting red flag that would cut winners in a trending tape.
RSI_SELL_THRESHOLD: float = 60.0

# Daily trend filter. A BUY on a stock is skipped unless its faster daily EMA
# is above its slower daily EMA. Tuned to a medium-term (20/50) trend rather
# than the slower year-long golden cross (50/200): more responsive and yields
# more setups, a better fit for this short-term, intraday style.
EMA_TREND_FAST_PERIOD: int = 20    # daily EMA, medium-term trend fast leg
EMA_TREND_SLOW_PERIOD: int = 50    # daily EMA, medium-term trend slow leg
TREND_LOOKBACK_BARS: int = 365     # daily bars to fetch for the trend check

# Market-regime filter. No long *stock* trades while this proxy symbol's own
# daily trend is "down" — i.e., don't fight a falling market. The proxy must be
# in WATCHLIST so its trend gets computed. Empty string disables the filter.
# Crypto is exempt (it doesn't track the stock market).
MARKET_REGIME_SYMBOL: str = "SPY"

# Crypto-regime filter. Gates new long *crypto* entries on a single proxy
# (CRYPTO_REGIME_SYMBOL, default BTC/USD) — the SPY analog for the crypto book.
# Two independent gates, both consulted for every crypto BUY:
#   - trend gate:    proxy daily EMA20 > EMA50 (slow, multi-week regime)
#   - intraday gate: proxy trades at/above its prior daily (UTC) close
# Empty CRYPTO_REGIME_SYMBOL disables BOTH. Each gate has its own toggle.
# The trend gate needs the proxy in WATCHLIST (its trend is computed in the
# warm-up loop); the intraday gate fetches the proxy directly. Any missing data
# or error fails OPEN (crypto trading allowed).
# NOTE (2026-06-17): both gates DISABLED after backtesting. BTC (and every alt)
# has been in a daily EMA20<EMA50 downtrend the whole window, so the trend gate
# blocks 100% of crypto buys — a kill-switch, not a filter — and the intraday
# gate blocked the better-performing dip entries (crypto here is mean-reverting:
# buying weakness is the edge). Re-enable only if a BTC-up regime changes that.
CRYPTO_REGIME_SYMBOL: str = "BTC/USD"
CRYPTO_REGIME_USE_TREND: bool = False
CRYPTO_REGIME_USE_INTRADAY: bool = False

# Crypto exit policy (2026-06-17, backtested). Crypto has NO signal-based exit:
# the RSI>65 sell rarely fires in a downtrend and just lets bags accumulate. Each
# crypto position instead exits on whichever fires first, checked every cycle vs
# the position's average entry price:
#   - take-profit: price >= avg_entry * (1 + CRYPTO_TAKE_PROFIT_PCT)
#   - stop-loss  : price <= avg_entry * (1 - CRYPTO_STOP_LOSS_PCT)   (crash guard)
#   - time cap   : held >= CRYPTO_MAX_HOLD_HOURS
# Backtest over the real entries (8 days): TP5% + 24h cap ≈ +$1,935 / 73% win;
# the 8% stop never triggered in that window (≈free crash insurance) and a stop
# is a deliberate hedge against a regime where dips stop mean-reverting. Set any
# leg to 0 to disable it.
CRYPTO_TAKE_PROFIT_PCT: float = 0.05
CRYPTO_STOP_LOSS_PCT: float = 0.08
CRYPTO_MAX_HOLD_HOURS: float = 24.0

# Master switch for opening NEW crypto positions. DISABLED 2026-06-18 after a
# 2-week analysis: short-timeframe crypto trading loses to buy-and-hold once the
# ~0.15-0.25%/side taker fee + slippage is paid (paper simulates the fee, so it's
# real). Backtest, net of cost, over the window: buy&hold +$65 vs the bot's RSI
# mean-reversion -$1,478. The same strategy is +EV on STOCKS (free execution),
# so the edge is real but only viable where trading is free. When False, the bot
# stops opening crypto positions but STILL manages/exits any it already holds
# (take-profit / stop-loss / time cap), so the existing book winds down to cash
# rather than being force-sold. Set True to resume crypto entries.
CRYPTO_TRADING_ENABLED: bool = False

# Market-hours guard. Stocks are not traded in the first/last N minutes of the
# session; all stock positions are flattened before close. Crypto is exempt.
MARKET_BLACKOUT_MINUTES: int = 15

# Opening no-trade window (stocks). No NEW stock entries for the first N minutes
# after the open — separate from MARKET_BLACKOUT_MINUTES so the closing blackout
# / EOD flatten timing is unaffected. Set 2026-06-18 from a 2-week time-of-day
# analysis: entries in the first ~1.5h of the session (9:30-11:00 ET) net -$519
# vs +$329 for midday/afternoon — the volatile open whipsaws the mean-reversion
# entries. 90 min skips the two worst buckets. The effective opening pause is
# max(MARKET_BLACKOUT_MINUTES, MARKET_OPEN_SKIP_MINUTES). Set 0 to disable.
MARKET_OPEN_SKIP_MINUTES: int = 90

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
TUI_REFRESH_SECONDS: int = 5
BOT_NAME: str = "AI Trading Bot"


def validate() -> list[str]:
    """Return a list of human-readable problems with the current config.

    An empty list means the config is usable. Callers (e.g. ``main.py``) can
    decide whether missing keys are fatal or merely degrade functionality.
    """
    problems: list[str] = []
    if not ALPACA_API_KEY:
        problems.append("ALPACA_API_KEY is not set.")
    if not ALPACA_SECRET_KEY:
        problems.append("ALPACA_SECRET_KEY is not set.")
    if not ANTHROPIC_API_KEY:
        problems.append(
            "ANTHROPIC_API_KEY is not set (AI commentary will be disabled)."
        )
    if "paper" not in ALPACA_BASE_URL:
        problems.append(
            f"ALPACA_BASE_URL does not look like a paper endpoint: "
            f"{ALPACA_BASE_URL!r}. Refusing to risk live trading."
        )
    if TRADE_INTERVAL_SECONDS <= 0:
        problems.append("TRADE_INTERVAL_SECONDS must be positive.")
    if MAX_POSITION_SIZE_USD <= 0:
        problems.append("MAX_POSITION_SIZE_USD must be positive.")
    if not WATCHLIST:
        problems.append("WATCHLIST is empty.")
    return problems


def ensure_data_dir() -> None:
    """Create the data directory if it does not yet exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
