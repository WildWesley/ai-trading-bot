"""Order execution and position management — runs one trading cycle per tick.

The ``Trader`` coordinates the other modules but owns none of them: the Alpaca
client, the database, and the AI advisor are injected. This keeps the loop
unit-testable with fakes and lets ``main.py`` decide how each dependency is
constructed.

One cycle (``run_cycle``) does, per the spec:
    1. For each watchlist symbol: fetch bars, run ``algorithm.analyze``.
    2. BUY signal + no position  -> size from MAX_POSITION_SIZE_USD, buy, record.
    3. SELL signal + open position -> close, compute realized P&L, record.
    4. After each trade: ask the AI advisor for commentary, store it.
    5. After the cycle: record an account snapshot.

Every external call is wrapped so a single failing symbol (or a flaky API)
degrades to a logged event rather than crashing the loop.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Protocol

from . import algorithm, config
from .alpaca_client import is_crypto_symbol


class _Advisor(Protocol):
    def get_commentary(
        self, symbol: str, analysis: dict, trade: dict
    ) -> str: ...


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Trader:
    """Runs trading cycles against an injected client/db/advisor."""

    def __init__(
        self,
        client: Any,
        db: Any,
        advisor: Any | None = None,
        *,
        watchlist: list[str] | None = None,
        max_position_usd: float | None = None,
    ) -> None:
        self.client = client
        self.db = db
        self.advisor = advisor
        self.watchlist = list(watchlist or config.WATCHLIST)
        self.max_position_usd = (
            max_position_usd
            if max_position_usd is not None
            else config.MAX_POSITION_SIZE_USD
        )

        self._lock = threading.Lock()
        # Recent human-readable events, surfaced by the TUI status panel.
        self.events: Deque[dict[str, str]] = deque(maxlen=100)
        # Optional sink called with each event as it's logged (used by headless
        # mode to stream events to the console). Set by the caller.
        self.on_event: Callable[[dict[str, str]], None] | None = None
        # Latest analysis per symbol, for display / debugging.
        self.last_analysis: dict[str, dict] = {}
        self.cycle_count = 0
        self.last_cycle_at: str | None = None
        # Daily trend cache (symbol -> "up"/"down"/"unknown"), refreshed once
        # per calendar day. Crypto entries stay "unknown" and bypass the filter.
        self._trend_cache: dict[str, str] = {}
        self._trend_cache_date: str | None = None
        # Market-state flag, set each cycle by the market guard (a later task).
        # Default True so trading works before the first market check runs.
        self._stock_trading_allowed: bool = True
        # Market-open transition tracking. _market_was_open starts None
        # (unknown) so a bot started mid-session does NOT impose an opening
        # blackout — that only fires on an observed closed->open transition.
        self._market_was_open: bool | None = None
        self._market_opened_at: datetime | None = None
        self._flattened_today: bool = False

    # -- Public API ------------------------------------------------------
    def run_cycle(self) -> None:
        """Run one full trading cycle. Never raises — errors become events."""
        self.cycle_count += 1
        self.log("info", f"Cycle {self.cycle_count} started.")
        self._warm_trend_cache_if_needed()
        self._update_market_state()
        for symbol in self.watchlist:
            try:
                self._process_symbol(symbol)
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol faults
                self.log("error", f"{symbol}: {exc}")
        try:
            self._record_snapshot()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"snapshot failed: {exc}")
        self.last_cycle_at = _utc_now_iso()
        self.log("info", f"Cycle {self.cycle_count} complete.")

    def log(self, level: str, message: str) -> None:
        """Append a timestamped event (thread-safe) and notify any sink."""
        event = {
            "timestamp": _utc_now_iso(),
            "level": level,
            "message": message,
        }
        with self._lock:
            self.events.append(event)
        # Notify outside the lock so a slow sink can't stall the trade loop.
        sink = self.on_event
        if sink is not None:
            try:
                sink(event)
            except Exception:  # noqa: BLE001 - a bad sink must not crash trading
                pass

    def recent_events(self, limit: int = 20) -> list[dict[str, str]]:
        with self._lock:
            return list(self.events)[-limit:][::-1]

    # -- Market hours ----------------------------------------------------
    def _update_market_state(self) -> None:
        """Set ``self._stock_trading_allowed`` from the market clock.

        Rules (stocks only — crypto always trades):
          * market closed                -> stock trading off
          * within MARKET_BLACKOUT_MINUTES of open  -> off (opening blackout)
          * within MARKET_BLACKOUT_MINUTES of close -> off, and flatten all
            stock positions once for the day (overnight-gap protection)
        On any clock failure, stock trading is disabled for this cycle (safe
        default); crypto is unaffected.
        """
        try:
            clock = self.client.get_clock()
        except Exception as exc:  # noqa: BLE001 - degrade safely
            self.log(
                "error",
                f"market clock unavailable; pausing stock trades: {exc}",
            )
            self._stock_trading_allowed = False
            return

        is_open = bool(clock.get("is_open"))
        now_utc = datetime.now(timezone.utc)

        # Observed closed -> open transition: record open time, reset flatten.
        if is_open and self._market_was_open is False:
            self._market_opened_at = now_utc
            self._flattened_today = False
            self.log("info", "Market opened.")
        self._market_was_open = is_open

        if not is_open:
            self._stock_trading_allowed = False
            return

        blackout = config.MARKET_BLACKOUT_MINUTES * 60

        in_opening_blackout = (
            self._market_opened_at is not None
            and (now_utc - self._market_opened_at).total_seconds() < blackout
        )

        next_close = clock.get("next_close")
        in_closing_blackout = False
        if next_close is not None:
            secs_to_close = (next_close - now_utc).total_seconds()
            in_closing_blackout = 0 <= secs_to_close < blackout

        if in_closing_blackout and not self._flattened_today:
            self._flatten_stock_positions()
            self._flattened_today = True

        self._stock_trading_allowed = not (
            in_opening_blackout or in_closing_blackout
        )

    def _flatten_stock_positions(self) -> None:
        """Close every open *stock* position (crypto is left running 24/7)."""
        self.log("info", "End-of-day: flattening all stock positions.")
        try:
            positions = self.client.get_positions()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"EOD flatten: could not list positions: {exc}")
            return
        for pos in positions:
            symbol = str(pos.get("symbol", ""))
            if not symbol or is_crypto_symbol(symbol):
                continue
            try:
                order = self.client.close_position(symbol)
                fill = (
                    order.get("filled_avg_price")
                    or pos.get("current_price")
                    or 0.0
                )
                qty = float(pos.get("qty", 0))
                pnl = float(pos.get("unrealized_pl", 0.0))
                trade = {
                    "symbol": symbol,
                    "side": "sell",
                    "qty": qty,
                    "price": fill,
                    "total_value": round(qty * fill, 2),
                    "signal_reason": "End-of-day flatten (market-close blackout).",
                    "timestamp": _utc_now_iso(),
                    "pnl_at_close": round(pnl, 2),
                }
                self.db.record_trade(trade)
                self.log(
                    "trade",
                    f"EOD SELL {qty} {symbol} @ ${fill:.2f} "
                    f"(P&L ${pnl:+.2f}).",
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol faults
                self.log("error", f"EOD flatten failed for {symbol}: {exc}")

    # -- Trend cache -----------------------------------------------------
    def _warm_trend_cache_if_needed(self) -> None:
        """Populate the daily-trend cache once per calendar day.

        Fetches ``TREND_LOOKBACK_BARS`` daily bars per stock and classifies the
        trend via ``algorithm.compute_daily_trend``. Crypto is recorded as
        "unknown" (it bypasses the filter). Per-symbol failures degrade to
        "unknown" rather than aborting the warm-up.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trend_cache_date == today and self._trend_cache:
            return
        self.log("info", "Warming daily-trend cache...")
        new_cache: dict[str, str] = {}
        total = len(self.watchlist)
        for i, symbol in enumerate(self.watchlist, start=1):
            if is_crypto_symbol(symbol):
                new_cache[symbol] = "unknown"
                continue
            try:
                daily = self.client.get_bars(
                    symbol, "1Day", config.TREND_LOOKBACK_BARS
                )
                new_cache[symbol] = algorithm.compute_daily_trend(daily)
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol faults
                new_cache[symbol] = "unknown"
                self.log("error", f"{symbol}: trend warm-up failed: {exc}")
            if i % 25 == 0:
                self.log("info", f"Trend cache: {i}/{total} symbols...")
        self._trend_cache = new_cache
        self._trend_cache_date = today
        up = sum(1 for v in new_cache.values() if v == "up")
        down = sum(1 for v in new_cache.values() if v == "down")
        self.log("info", f"Trend cache ready: {up} up, {down} down.")

    # -- Per-symbol logic ------------------------------------------------
    def _process_symbol(self, symbol: str) -> None:
        # Stocks pause when the market is closed or in a blackout window;
        # crypto trades around the clock.
        if not is_crypto_symbol(symbol) and not self._stock_trading_allowed:
            return
        bars = self.client.get_bars(
            symbol, f"{config.BAR_TIMEFRAME_MINUTES}Min", config.BARS_LOOKBACK
        )
        analysis = algorithm.analyze(symbol, bars)
        self.last_analysis[symbol] = analysis
        signal = analysis["signal"]

        # Per-symbol heartbeat so a slow/hanging symbol is visible in the UI
        # rather than looking like a frozen cycle. Concise enough not to drown
        # out trade events.
        rsi = analysis.get("rsi")
        rsi_txt = f"RSI {rsi:.0f}" if isinstance(rsi, (int, float)) else "RSI n/a"
        self.log("debug", f"{symbol}: {signal} ({rsi_txt}).")

        position = self.client.get_position(symbol)
        has_position = position is not None and float(
            position.get("qty", 0)
        ) > 0

        if signal == "BUY" and not has_position:
            if not is_crypto_symbol(symbol):
                trend = self._trend_cache.get(symbol, "unknown")
                if trend == "down":
                    self.log(
                        "debug",
                        f"{symbol}: BUY skipped — daily trend bearish "
                        f"(death cross).",
                    )
                    return
            self._open_long(symbol, analysis)
        elif signal == "SELL" and has_position:
            self._close_long(symbol, analysis, position)
        else:
            # Quietly hold; avoid flooding the event log on every HOLD.
            pass

    def _open_long(self, symbol: str, analysis: dict) -> None:
        price = analysis.get("price") or self.client.get_latest_price(symbol)
        if not price or price <= 0:
            self.log("error", f"{symbol}: no valid price; skipping BUY.")
            return

        if is_crypto_symbol(symbol):
            # Crypto trades fractionally, so a $1000 cap buys 0.0xyz BTC rather
            # than requiring a whole coin. Round to 6 dp (Alpaca's precision).
            qty = round(self.max_position_usd / price, 6)
            too_small = qty <= 0
        else:
            # Stocks: whole shares only.
            qty = int(self.max_position_usd // price)
            too_small = qty < 1
        if too_small:
            self.log(
                "info",
                f"{symbol}: price ${price:.2f} exceeds max position "
                f"${self.max_position_usd:.0f}; skipping BUY.",
            )
            return

        order = self.client.place_market_order(symbol, qty, "buy")
        fill_price = order.get("filled_avg_price") or price
        filled_qty = order.get("filled_qty") or qty

        trade = {
            "symbol": symbol,
            "side": "buy",
            "qty": filled_qty,
            "price": fill_price,
            "total_value": round(filled_qty * fill_price, 2),
            "signal_reason": analysis.get("reason", ""),
            "timestamp": _utc_now_iso(),
            "pnl_at_close": None,
        }
        trade_id = self.db.record_trade(trade)
        self.log(
            "trade",
            f"BUY {filled_qty} {symbol} @ ${fill_price:.2f} "
            f"(${trade['total_value']:.2f}).",
        )
        self._attach_commentary(trade_id, symbol, analysis, trade)

    def _close_long(
        self, symbol: str, analysis: dict, position: dict
    ) -> None:
        qty = float(position.get("qty", 0))
        # Realized P&L at close: Alpaca's unrealized P&L on the position is the
        # gain/loss we lock in by liquidating now.
        realized_pnl = float(position.get("unrealized_pl", 0.0))

        order = self.client.close_position(symbol)
        fill_price = (
            order.get("filled_avg_price")
            or analysis.get("price")
            or position.get("current_price")
            or 0.0
        )

        trade = {
            "symbol": symbol,
            "side": "sell",
            "qty": qty,
            "price": fill_price,
            "total_value": round(qty * fill_price, 2),
            "signal_reason": analysis.get("reason", ""),
            "timestamp": _utc_now_iso(),
            "pnl_at_close": round(realized_pnl, 2),
        }
        trade_id = self.db.record_trade(trade)
        self.log(
            "trade",
            f"SELL {qty} {symbol} @ ${fill_price:.2f} "
            f"(realized P&L ${realized_pnl:+.2f}).",
        )
        self._attach_commentary(trade_id, symbol, analysis, trade)

    # -- AI commentary ---------------------------------------------------
    def _attach_commentary(
        self, trade_id: int, symbol: str, analysis: dict, trade: dict
    ) -> None:
        if self.advisor is None:
            return
        try:
            commentary = self.advisor.get_commentary(symbol, analysis, trade)
        except Exception as exc:  # noqa: BLE001 - AI is best-effort
            self.log("error", f"{symbol}: AI commentary failed: {exc}")
            return
        if commentary:
            try:
                self.db.set_trade_commentary(trade_id, commentary)
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"{symbol}: storing commentary failed: {exc}")

    # -- Snapshot --------------------------------------------------------
    def _record_snapshot(self) -> None:
        account = self.client.get_account()
        positions = self.client.get_positions()
        self.db.record_snapshot(
            {
                "equity": account.get("equity", 0.0),
                "cash": account.get("cash", 0.0),
                "buying_power": account.get("buying_power", 0.0),
                "open_positions_count": len(positions),
                "timestamp": _utc_now_iso(),
            }
        )
