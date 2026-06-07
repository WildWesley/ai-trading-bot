"""Thin wrapper around alpaca-py for REST market data and order placement.

Exposes a small, intention-revealing surface to the rest of the bot so the
alpaca-py types never leak past this module:

    get_account()                      -> dict
    get_positions()                    -> list[dict]
    get_bars(symbol, timeframe, limit) -> pandas.DataFrame (OHLCV)
    get_latest_price(symbol)           -> float
    place_market_order(symbol, qty, side) -> dict
    get_orders(status)                 -> list[dict]

All trading targets Alpaca's **paper** endpoint. Transient API failures
(rate limits, 5xx, connection blips) are retried with exponential backoff.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

import pandas as pd

from alpaca.common.exceptions import APIError
from alpaca.data.historical import (
    CryptoHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestBarRequest,
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from . import config


def is_crypto_symbol(symbol: str) -> bool:
    """True for Alpaca crypto pairs, which are written like ``BTC/USD``.

    Crypto uses different market-data endpoints and order rules than stocks,
    so this single check routes everything (bars, prices, order TIF, sizing).
    """
    return "/" in symbol

# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------
_MAX_RETRIES = 5
_BASE_DELAY_SECONDS = 1.0
_MAX_DELAY_SECONDS = 30.0


class AlpacaClientError(RuntimeError):
    """Raised when an Alpaca operation fails after exhausting retries."""


def _is_retryable(exc: Exception) -> bool:
    """Heuristic: retry rate limits and transient server/connection errors."""
    if isinstance(exc, APIError):
        # APIError carries the HTTP status code on .status_code in recent
        # alpaca-py; fall back to string inspection if absent.
        status = getattr(exc, "status_code", None)
        if status is not None:
            return status == 429 or 500 <= status < 600
        text = str(exc)
        return "429" in text or "rate limit" in text.lower()
    # Network-level hiccups from the underlying HTTP stack.
    return isinstance(exc, (ConnectionError, TimeoutError))


def _with_backoff(fn: Callable[[], Any], *, what: str) -> Any:
    """Call ``fn`` retrying retryable errors with exponential backoff."""
    delay = _BASE_DELAY_SECONDS
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - we re-raise below
            last_exc = exc
            if attempt == _MAX_RETRIES or not _is_retryable(exc):
                break
            time.sleep(min(delay, _MAX_DELAY_SECONDS))
            delay *= 2
    raise AlpacaClientError(
        f"Alpaca request failed ({what}) after {_MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


_TF_UNIT_MAP = {
    "min": TimeFrameUnit.Minute,
    "minute": TimeFrameUnit.Minute,
    "hour": TimeFrameUnit.Hour,
    "day": TimeFrameUnit.Day,
}


def _parse_timeframe(timeframe: str) -> TimeFrame:
    """Parse strings like '5Min', '1Min', '1Hour', '1Day' into a TimeFrame.

    Falls back to the configured default (5-minute) on anything unrecognized.
    """
    import re

    match = re.match(r"^\s*(\d+)\s*([A-Za-z]+)\s*$", timeframe)
    if not match:
        return TimeFrame(config.BAR_TIMEFRAME_MINUTES, TimeFrameUnit.Minute)
    amount = int(match.group(1))
    unit = _TF_UNIT_MAP.get(match.group(2).lower())
    if unit is None:
        return TimeFrame(config.BAR_TIMEFRAME_MINUTES, TimeFrameUnit.Minute)
    return TimeFrame(amount, unit)


def _timeframe_minutes(timeframe: str) -> int:
    """Minutes per bar for a timeframe string ('5Min'->5, '1Hour'->60, '1Day'->1440)."""
    import re

    match = re.match(r"^\s*(\d+)\s*([A-Za-z]+)\s*$", timeframe)
    if not match:
        return config.BAR_TIMEFRAME_MINUTES
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("min"):
        return amount
    if unit.startswith("hour"):
        return amount * 60
    if unit.startswith("day"):
        return amount * 1440
    return config.BAR_TIMEFRAME_MINUTES


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class AlpacaClient:
    """Wraps the alpaca-py trading + market-data clients (paper trading)."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self._api_key = api_key or config.ALPACA_API_KEY
        self._secret_key = secret_key or config.ALPACA_SECRET_KEY
        if not self._api_key or not self._secret_key:
            raise AlpacaClientError(
                "Alpaca API key/secret missing. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY in your .env."
            )

        # paper=True forces the paper endpoint regardless of url_override.
        self._trading = TradingClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
            paper=True,
        )
        self._data = StockHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
        )
        # Crypto data comes from a separate endpoint. Keys are optional for
        # crypto market data but we pass them for consistency.
        self._crypto_data = CryptoHistoricalDataClient(
            api_key=self._api_key,
            secret_key=self._secret_key,
        )

    # -- Health check ----------------------------------------------------
    def verify_connection(self) -> dict[str, Any]:
        """Make one authenticated call to confirm the keys actually work.

        Returns the account dict on success. On failure raises
        ``AlpacaClientError`` with a human-readable message; auth failures
        (HTTP 401/403) are called out specifically so the user knows the keys
        themselves were rejected rather than blaming a network blip.
        """
        try:
            return self.get_account()
        except AlpacaClientError as exc:
            text = str(exc).lower()
            if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
                raise AlpacaClientError(
                    "Alpaca rejected your API keys (HTTP 401/403). Check that "
                    "ALPACA_API_KEY and ALPACA_SECRET_KEY are correct and that "
                    "they are *paper* trading keys (generated while in Paper "
                    "Trading mode, not live)."
                ) from exc
            raise

    # -- Account ---------------------------------------------------------
    def get_account(self) -> dict[str, Any]:
        """Return account equity, buying power, and cash."""
        acct = _with_backoff(self._trading.get_account, what="get_account")
        return {
            "equity": _to_float(getattr(acct, "equity", 0)),
            "cash": _to_float(getattr(acct, "cash", 0)),
            "buying_power": _to_float(getattr(acct, "buying_power", 0)),
            "portfolio_value": _to_float(getattr(acct, "portfolio_value", 0)),
            "status": str(getattr(acct, "status", "")),
            "currency": str(getattr(acct, "currency", "USD")),
        }

    # -- Market clock ----------------------------------------------------
    def get_clock(self) -> dict[str, Any]:
        """Return the market clock: is_open plus next open/close timestamps.

        ``next_open`` / ``next_close`` are timezone-aware datetimes straight
        from alpaca-py. Authoritative for trading hours and holidays, so the
        trader uses this rather than computing Eastern time locally.
        """
        clock = _with_backoff(self._trading.get_clock, what="get_clock")
        return {
            "is_open": bool(getattr(clock, "is_open", False)),
            "next_open": getattr(clock, "next_open", None),
            "next_close": getattr(clock, "next_close", None),
            "timestamp": getattr(clock, "timestamp", None),
        }

    # -- Positions -------------------------------------------------------
    def get_positions(self) -> list[dict[str, Any]]:
        """Return open positions with unrealized P&L."""
        positions = _with_backoff(
            self._trading.get_all_positions, what="get_all_positions"
        )
        result: list[dict[str, Any]] = []
        for pos in positions:
            result.append(
                {
                    "symbol": str(getattr(pos, "symbol", "")),
                    "qty": _to_float(getattr(pos, "qty", 0)),
                    "side": str(getattr(getattr(pos, "side", ""), "value",
                                        getattr(pos, "side", ""))),
                    "avg_entry_price": _to_float(
                        getattr(pos, "avg_entry_price", 0)
                    ),
                    "current_price": _to_float(
                        getattr(pos, "current_price", 0)
                    ),
                    "market_value": _to_float(getattr(pos, "market_value", 0)),
                    "cost_basis": _to_float(getattr(pos, "cost_basis", 0)),
                    "unrealized_pl": _to_float(getattr(pos, "unrealized_pl", 0)),
                    "unrealized_plpc": _to_float(
                        getattr(pos, "unrealized_plpc", 0)
                    ),
                }
            )
        return result

    def get_position(self, symbol: str) -> dict[str, Any] | None:
        """Return a single open position for ``symbol`` or None if flat."""
        try:
            pos = _with_backoff(
                lambda: self._trading.get_open_position(symbol),
                what=f"get_open_position({symbol})",
            )
        except AlpacaClientError as exc:
            # "No position" means flat, not an error. Alpaca phrases this
            # differently across asset classes: stocks say "position does not
            # exist", crypto returns a bare "Not Found" (404).
            msg = str(exc).lower()
            if (
                "404" in msg
                or "not found" in msg
                or "position does not exist" in msg
            ):
                return None
            raise
        return {
            "symbol": str(getattr(pos, "symbol", symbol)),
            "qty": _to_float(getattr(pos, "qty", 0)),
            "avg_entry_price": _to_float(getattr(pos, "avg_entry_price", 0)),
            "current_price": _to_float(getattr(pos, "current_price", 0)),
            "market_value": _to_float(getattr(pos, "market_value", 0)),
            "unrealized_pl": _to_float(getattr(pos, "unrealized_pl", 0)),
        }

    # -- Market data -----------------------------------------------------
    def get_bars(
        self,
        symbol: str,
        timeframe: str = "5Min",
        limit: int = 50,
    ) -> pd.DataFrame:
        """Return a DataFrame of OHLCV bars indexed by timestamp.

        Columns: open, high, low, close, volume (+ trade_count, vwap when
        available). Returns an empty DataFrame if no data is returned.
        """
        tf = _parse_timeframe(timeframe)
        crypto = is_crypto_symbol(symbol)

        # Size the lookback window to the bar interval AND asset, then fetch
        # *everything* from ``start`` to now (no request limit) and keep the
        # most-recent ``limit`` bars. Alpaca returns bars ascending from
        # ``start`` and a capped request limit yields the *oldest* slice (stale
        # data), so we don't cap: overshooting the window just fetches a few
        # extra old bars that get trimmed; under-fetching is what we must avoid.
        # The estimates below deliberately err long (stocks have extended-hours
        # bars beyond a 6.5h session), which is the safe direction.
        minutes_per_bar = _timeframe_minutes(timeframe)
        if minutes_per_bar >= 1440:          # daily bars: ~1 trading bar/day
            bars_per_day, pad, floor_days = 1.0, 1.6, 3
        elif crypto:                          # crypto trades 24/7
            bars_per_day, pad, floor_days = (24 * 60) / minutes_per_bar, 1.2, 1
        else:                                 # stock intraday (~6.5h, padded up)
            bars_per_day, pad, floor_days = (6.5 * 60) / minutes_per_bar, 1.6, 3
        lookback_days = max(floor_days, int(limit / bars_per_day * pad) + floor_days)
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        if crypto:
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol, timeframe=tf, start=start
            )
            bars = _with_backoff(
                lambda: self._crypto_data.get_crypto_bars(request),
                what=f"get_crypto_bars({symbol})",
            )
        else:
            request = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=tf, start=start
            )
            bars = _with_backoff(
                lambda: self._data.get_stock_bars(request),
                what=f"get_stock_bars({symbol})",
            )

        df = bars.df
        if df is None or df.empty:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume"]
            )

        # Multi-symbol requests yield a MultiIndex (symbol, timestamp); drop
        # the symbol level so callers get a clean timestamp index.
        if isinstance(df.index, pd.MultiIndex):
            if symbol in df.index.get_level_values(0):
                df = df.xs(symbol, level=0)
            else:
                df = df.droplevel(0)

        df = df.sort_index().tail(limit)
        return df

    def get_latest_price(self, symbol: str) -> float:
        """Return a current price for ``symbol`` as a float.

        Prefers the latest **quote** mid-price, which updates continuously,
        over the latest *trade*: Alpaca's crypto trade feed can lag several
        minutes, which made watchlist prices look frozen. Falls back to the
        last trade, then the most recent 1-minute bar close, so the result is
        never blank when any data exists.
        """
        mid = self._latest_quote_mid(symbol)
        if mid > 0:
            return mid
        trade = self._latest_trade_price(symbol)
        if trade > 0:
            return trade
        return self._latest_bar_close(symbol)

    @staticmethod
    def _pick(result: Any, symbol: str) -> Any:
        return result.get(symbol) if isinstance(result, dict) else result

    def _latest_quote_mid(self, symbol: str) -> float:
        """Mid-price of the latest quote, or 0.0 if unavailable."""
        try:
            if is_crypto_symbol(symbol):
                req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
                res = _with_backoff(
                    lambda: self._crypto_data.get_crypto_latest_quote(req),
                    what=f"get_crypto_latest_quote({symbol})",
                )
            else:
                req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
                res = _with_backoff(
                    lambda: self._data.get_stock_latest_quote(req),
                    what=f"get_stock_latest_quote({symbol})",
                )
        except AlpacaClientError:
            return 0.0
        quote = self._pick(res, symbol)
        bid = _to_float(getattr(quote, "bid_price", 0))
        ask = _to_float(getattr(quote, "ask_price", 0))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return ask if ask > 0 else bid

    def _latest_trade_price(self, symbol: str) -> float:
        """Price of the latest trade, or 0.0 if unavailable."""
        try:
            if is_crypto_symbol(symbol):
                req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
                res = _with_backoff(
                    lambda: self._crypto_data.get_crypto_latest_trade(req),
                    what=f"get_crypto_latest_trade({symbol})",
                )
            else:
                req = StockLatestTradeRequest(symbol_or_symbols=symbol)
                res = _with_backoff(
                    lambda: self._data.get_stock_latest_trade(req),
                    what=f"get_stock_latest_trade({symbol})",
                )
        except AlpacaClientError:
            return 0.0
        return _to_float(getattr(self._pick(res, symbol), "price", 0))

    def _latest_bar_close(self, symbol: str) -> float:
        """Close of the most recent 1-minute bar, or 0.0 if unavailable."""
        try:
            if is_crypto_symbol(symbol):
                req = CryptoLatestBarRequest(symbol_or_symbols=symbol)
                res = _with_backoff(
                    lambda: self._crypto_data.get_crypto_latest_bar(req),
                    what=f"get_crypto_latest_bar({symbol})",
                )
            else:
                req = StockLatestBarRequest(symbol_or_symbols=symbol)
                res = _with_backoff(
                    lambda: self._data.get_stock_latest_bar(req),
                    what=f"get_stock_latest_bar({symbol})",
                )
        except AlpacaClientError:
            return 0.0
        return _to_float(getattr(self._pick(res, symbol), "close", 0))

    # -- Orders ----------------------------------------------------------
    def place_market_order(
        self,
        symbol: str,
        qty: float | None = None,
        side: Literal["buy", "sell"] = "buy",
        *,
        notional: float | None = None,
    ) -> dict[str, Any]:
        """Submit a market order and return a normalized order dict.

        Provide exactly one of ``qty`` (share/coin quantity) or ``notional``
        (a dollar amount — fractional). Notional orders let stocks be sized to a
        dollar budget regardless of share price; Alpaca requires DAY
        time-in-force for them, which stocks already use.
        """
        if (qty is None) == (notional is None):
            raise AlpacaClientError(
                "place_market_order requires exactly one of qty or notional."
            )
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        # Crypto only accepts GTC/IOC; stocks use DAY (also required for
        # fractional/notional orders).
        tif = TimeInForce.GTC if is_crypto_symbol(symbol) else TimeInForce.DAY
        if notional is not None:
            request = MarketOrderRequest(
                symbol=symbol,
                notional=notional,
                side=order_side,
                time_in_force=tif,
            )
            what = f"submit_order({symbol},{side},${notional})"
        else:
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=tif,
            )
            what = f"submit_order({symbol},{side},{qty})"
        order = _with_backoff(
            lambda: self._trading.submit_order(request),
            what=what,
        )
        return self._normalize_order(order)

    def get_asset(self, symbol: str) -> dict[str, Any]:
        """Return asset metadata: ``fractionable`` and ``tradable`` flags.

        Used to decide whether a stock can be bought by a notional (fractional)
        dollar amount or only in whole shares. ``fractionable`` defaults to
        False when the attribute is absent, so callers fall back to whole shares.
        """
        asset = _with_backoff(
            lambda: self._trading.get_asset(symbol),
            what=f"get_asset({symbol})",
        )
        return {
            "symbol": str(getattr(asset, "symbol", symbol)),
            "fractionable": bool(getattr(asset, "fractionable", False)),
            "tradable": bool(getattr(asset, "tradable", False)),
        }

    def close_position(self, symbol: str) -> dict[str, Any]:
        """Liquidate the entire position in ``symbol``; return the order dict."""
        order = _with_backoff(
            lambda: self._trading.close_position(symbol),
            what=f"close_position({symbol})",
        )
        return self._normalize_order(order)

    def get_orders(self, status: str = "all", limit: int = 50) -> list[dict[str, Any]]:
        """Return recent orders. ``status`` is one of open/closed/all."""
        status_map = {
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all": QueryOrderStatus.ALL,
        }
        request = GetOrdersRequest(
            status=status_map.get(status.lower(), QueryOrderStatus.ALL),
            limit=limit,
        )
        orders = _with_backoff(
            lambda: self._trading.get_orders(filter=request),
            what="get_orders",
        )
        return [self._normalize_order(o) for o in orders]

    # -- Internal --------------------------------------------------------
    @staticmethod
    def _normalize_order(order: Any) -> dict[str, Any]:
        def _enum_val(attr: str) -> str:
            raw = getattr(order, attr, "")
            return str(getattr(raw, "value", raw))

        return {
            "id": str(getattr(order, "id", "")),
            "symbol": str(getattr(order, "symbol", "")),
            "side": _enum_val("side"),
            "qty": _to_float(getattr(order, "qty", 0)),
            "filled_qty": _to_float(getattr(order, "filled_qty", 0)),
            "filled_avg_price": _to_float(
                getattr(order, "filled_avg_price", 0)
            ),
            "status": _enum_val("status"),
            "order_type": _enum_val("order_type"),
            "submitted_at": str(getattr(order, "submitted_at", "")),
        }
