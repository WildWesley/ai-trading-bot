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
# Strategy constants (per the spec)
# ---------------------------------------------------------------------------
BARS_LOOKBACK: int = 50          # number of candles to fetch per analysis
BAR_TIMEFRAME_MINUTES: int = 5   # 5-minute candles
RSI_PERIOD: int = 14
EMA_FAST_PERIOD: int = 9
EMA_SLOW_PERIOD: int = 21
RSI_BUY_THRESHOLD: float = 35.0   # genuinely oversold (not just below midline)
RSI_SELL_THRESHOLD: float = 65.0

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

# Market-hours guard. Stocks are not traded in the first/last N minutes of the
# session; all stock positions are flattened before close. Crypto is exempt.
MARKET_BLACKOUT_MINUTES: int = 15

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
