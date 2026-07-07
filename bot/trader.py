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
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")  # rebalance timing is in US market time

from . import algorithm, config, momentum
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
        # Market-regime gate. Recomputed each cycle: True == the market proxy
        # (MARKET_REGIME_SYMBOL) is trading below its prior daily close ("red"
        # intraday), so new long stock entries pause. Defaults False (allow).
        # The proxy's prior close is cached per calendar day.
        self._market_regime_down: bool = False
        self._regime_prior_close: float | None = None
        self._regime_prior_close_date: str | None = None
        # Crypto-regime gate (the SPY analog for the crypto book). Recomputed
        # each cycle: True == the crypto proxy (CRYPTO_REGIME_SYMBOL) is trading
        # below its prior daily (UTC) close, so new long crypto entries pause.
        # Defaults False (allow). Prior close cached per calendar day. The
        # daily-EMA20/50 trend gate reuses self._trend_cache (see warm-up).
        self._crypto_regime_down: bool = False
        self._crypto_regime_prior_close: float | None = None
        self._crypto_regime_prior_close_date: str | None = None
        # Market-state flag, set each cycle by the market guard (a later task).
        # Default True so trading works before the first market check runs.
        self._stock_trading_allowed: bool = True
        # Market-open transition tracking. _market_was_open starts None
        # (unknown) so a bot started mid-session does NOT impose an opening
        # blackout — that only fires on an observed closed->open transition.
        self._market_was_open: bool | None = None
        self._market_opened_at: datetime | None = None
        self._flattened_today: bool = False
        # Per-symbol "can this stock be bought by a fractional/notional dollar
        # amount?" cache, so a BUY doesn't re-query the asset every cycle.
        self._fractionable_cache: dict[str, bool] = {}
        # Open positions snapshotted once per cycle (symbol -> position dict),
        # so we don't make a per-symbol position lookup across the watchlist.
        self._positions: dict[str, dict] = {}
        # Alpaca reports crypto positions slashless ("BTCUSD"), but the
        # watchlist and order path use the canonical "BTC/USD". Without a
        # mapping the per-symbol position lookup in _process_symbol never
        # matches, so the bot is blind to crypto it already holds: it re-buys
        # held coins (runaway accumulation) and can never match a sell. Map
        # slashless -> canonical so lookups resolve. Built once; the watchlist
        # is fixed for the trader's lifetime.
        self._crypto_position_aliases: dict[str, str] = {
            s.replace("/", ""): s for s in self.watchlist if "/" in s
        }
        # Entry time per held crypto symbol, for the time-cap exit. Set when we
        # open a crypto long; lazily initialized to "now" the first time we see
        # a position we have no record for (e.g. positions that predate a
        # restart) — those get a fresh time-cap clock, while the price-based
        # take-profit / stop-loss legs apply to them immediately regardless.
        self._crypto_entry_times: dict[str, datetime] = {}
        # Momentum rotation: the ISO week ("2026-W27") of the last rebalance, so
        # the weekly rotation fires at most once per week. None until the first.
        self._last_rebalance_week: str | None = None
        # Whether we've done the first rebalance since (re)start. The first one
        # fires as soon as the market is open — regardless of the configured
        # rebalance weekday — so a fresh deploy doesn't sit in cash waiting.
        self._did_initial_rebalance: bool = False

    # -- Public API ------------------------------------------------------
    def run_cycle(self) -> None:
        """Run one full trading cycle. Never raises — errors become events.

        Dispatches on ``config.STRATEGY``: "momentum" runs the weekly rotation
        (cheap except on the weekly rebalance); "rsi" runs the legacy 5-minute
        intraday cycle below.
        """
        self.cycle_count += 1
        if config.STRATEGY == "momentum":
            self._run_momentum_cycle()
            return
        self.log("info", f"Cycle {self.cycle_count} started.")
        self._warm_trend_cache_if_needed()
        self._update_market_state()
        self._update_market_regime()
        self._update_crypto_regime()
        # Snapshot all open positions in ONE call, then look each symbol up
        # locally — far fewer API calls than a per-symbol position lookup. If
        # this fails we skip trading this cycle rather than risk treating held
        # positions as flat and double-buying them.
        try:
            self._positions = {
                self._crypto_position_aliases.get(
                    str(p.get("symbol")), str(p.get("symbol"))
                ): p
                for p in self.client.get_positions()
            }
            positions_ok = True
        except Exception as exc:  # noqa: BLE001 - degrade safely
            self.log(
                "error",
                f"could not fetch positions; skipping trades this cycle: {exc}",
            )
            positions_ok = False

        if positions_ok:
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

    # -- Market regime ---------------------------------------------------
    def _prior_daily_close(self, symbol: str) -> float | None:
        """Most recent *settled* daily close for ``symbol`` — i.e. excluding
        today's still-forming daily bar. None if no usable data.
        """
        bars = self.client.get_bars(symbol, "1Day", limit=5)
        if bars is None or bars.empty or "close" not in bars.columns:
            return None
        today = datetime.now(timezone.utc).date()
        closes = [
            float(c)
            for ts, c in zip(bars.index, bars["close"])
            if ts.date() < today
        ]
        return closes[-1] if closes else None

    def _update_market_regime(self) -> None:
        """Set ``self._market_regime_down`` from an intraday "red day" check on
        the market proxy (``MARKET_REGIME_SYMBOL``, default SPY): pause new long
        stock entries while the proxy trades *below its prior daily close*.

        Unlike the slow daily-EMA trend, this reacts within the session, which
        is what catches sharp broad-market drops. Crypto is unaffected (the gate
        is only consulted for stocks). Fails OPEN — any missing data or error
        leaves trading allowed. The proxy's prior close is cached per day.
        """
        self._market_regime_down = False  # default: allow (fail open)
        regime = config.MARKET_REGIME_SYMBOL
        if not regime or not self._stock_trading_allowed:
            return
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            if self._regime_prior_close_date != today:
                self._regime_prior_close = self._prior_daily_close(regime)
                self._regime_prior_close_date = today
            prior_close = self._regime_prior_close
            if prior_close is None:
                return
            live = self.client.get_latest_price(regime)
            if not live or live <= 0:
                return
            if live < prior_close:
                self._market_regime_down = True
                pct = (live / prior_close - 1) * 100
                self.log(
                    "info",
                    f"Market regime risk-off: {regime} {live:.2f} below prior "
                    f"close {prior_close:.2f} ({pct:+.2f}%); pausing stock buys.",
                )
        except Exception as exc:  # noqa: BLE001 - never crash the cycle
            self.log(
                "error", f"market-regime check failed (allowing trades): {exc}"
            )

    def _update_crypto_regime(self) -> None:
        """Set ``self._crypto_regime_down`` from an intraday "red day" check on
        the crypto proxy (``CRYPTO_REGIME_SYMBOL``, default BTC/USD): pause new
        long *crypto* entries while the proxy trades *below its prior daily
        close*. The crypto analog of ``_update_market_regime``.

        Unlike the stock check this is NOT gated on market hours — crypto trades
        24/7. "Prior close" is the proxy's last settled daily (UTC) candle.
        Fails OPEN — any missing data or error leaves trading allowed. The
        proxy's prior close is cached per calendar day.
        """
        self._crypto_regime_down = False  # default: allow (fail open)
        sym = config.CRYPTO_REGIME_SYMBOL
        if not sym or not config.CRYPTO_REGIME_USE_INTRADAY:
            return
        try:
            today = datetime.now(timezone.utc).date().isoformat()
            if self._crypto_regime_prior_close_date != today:
                self._crypto_regime_prior_close = self._prior_daily_close(sym)
                self._crypto_regime_prior_close_date = today
            prior_close = self._crypto_regime_prior_close
            if prior_close is None:
                return
            live = self.client.get_latest_price(sym)
            if not live or live <= 0:
                return
            if live < prior_close:
                self._crypto_regime_down = True
                pct = (live / prior_close - 1) * 100
                self.log(
                    "info",
                    f"Crypto regime risk-off: {sym} {live:.2f} below prior "
                    f"close {prior_close:.2f} ({pct:+.2f}%); pausing crypto buys.",
                )
        except Exception as exc:  # noqa: BLE001 - never crash the cycle
            self.log(
                "error",
                f"crypto-regime check failed (allowing trades): {exc}",
            )

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
        # Opening pause: the wider of the safety blackout and the (data-driven)
        # open-skip window, so we sit out the volatile first ~90 min. Closing
        # blackout / EOD flatten below stay on MARKET_BLACKOUT_MINUTES.
        opening_pause = max(blackout, config.MARKET_OPEN_SKIP_MINUTES * 60)

        in_opening_blackout = (
            self._market_opened_at is not None
            and (now_utc - self._market_opened_at).total_seconds() < opening_pause
        )

        # NOTE: the EOD flatten relies on at least one cycle landing inside the
        # closing window, i.e. TRADE_INTERVAL_SECONDS < MARKET_BLACKOUT_MINUTES*60
        # (default 300s < 900s). If the interval is raised above the blackout
        # width, a cycle could skip the window and positions go unflattened.
        next_close = clock.get("next_close")
        in_closing_blackout = False
        if next_close is not None:
            secs_to_close = (next_close - now_utc).total_seconds()
            in_closing_blackout = 0 <= secs_to_close < blackout
        else:
            self.log(
                "warning",
                "market clock missing next_close; skipping closing-blackout "
                "/ end-of-day flatten this cycle.",
            )

        if in_closing_blackout and not self._flattened_today:
            self._flatten_stock_positions()
            # Set even if the flatten logged per-symbol errors: only attempt the
            # EOD flatten once per day rather than retrying every cycle.
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
            # Skip crypto: it trades 24/7 and is governed by its own exit policy
            # (_maybe_close_crypto), not the stock market-close flatten. Alpaca
            # reports crypto slashless ("BTCUSD"), so is_crypto_symbol() (which
            # keys on "/") misses it — also check the slashless alias map, or the
            # EOD flatten would wrongly liquidate the whole crypto book daily.
            if (
                not symbol
                or is_crypto_symbol(symbol)
                or symbol in self._crypto_position_aliases
            ):
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
            # Crypto bypasses the per-symbol trend filter and stays "unknown" —
            # except the crypto regime proxy, whose trend we DO compute (when the
            # trend gate is enabled) so it can gate the whole crypto book.
            is_crypto_proxy = (
                symbol == config.CRYPTO_REGIME_SYMBOL
                and config.CRYPTO_REGIME_USE_TREND
            )
            if is_crypto_symbol(symbol) and not is_crypto_proxy:
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

        position = self._positions.get(symbol)
        has_position = position is not None and float(
            position.get("qty", 0)
        ) > 0

        # Crypto exits are price/time-driven (take-profit / stop-loss / time
        # cap), checked every cycle regardless of the BUY/SELL signal. This
        # replaces the (dead) RSI signal exit for crypto; stocks keep theirs.
        if is_crypto_symbol(symbol) and has_position:
            if self._maybe_close_crypto(symbol, position, analysis):
                return

        if signal == "BUY" and not has_position:
            if not is_crypto_symbol(symbol):
                # Market-regime filter: don't go long ANY stock while the broad
                # market proxy (SPY) is "red" intraday — trading below its prior
                # daily close. Recomputed each cycle in _update_market_regime;
                # missing data / errors fail open (allow). See that method.
                if self._market_regime_down:
                    self.log(
                        "debug",
                        f"{symbol}: BUY skipped — market "
                        f"({config.MARKET_REGIME_SYMBOL}) red intraday.",
                    )
                    return
                trend = self._trend_cache.get(symbol, "unknown")
                if trend == "down":
                    self.log(
                        "debug",
                        f"{symbol}: BUY skipped — daily trend bearish "
                        f"(death cross).",
                    )
                    return
            else:
                # Master switch: crypto entries are disabled (short-timeframe
                # crypto loses to buy-and-hold after the taker fee — see config).
                # Existing crypto positions still exit via _maybe_close_crypto
                # above, so the book winds down to cash rather than re-buying.
                if not config.CRYPTO_TRADING_ENABLED:
                    self.log(
                        "debug",
                        f"{symbol}: BUY skipped — crypto trading disabled.",
                    )
                    return
                # Crypto regime gates (the SPY analog for the crypto book): a
                # crypto BUY needs the proxy (BTC) both in a daily uptrend AND
                # not red intraday. Each gate is independently toggleable; both
                # fail open. See _update_crypto_regime / _warm_trend_cache.
                sym = config.CRYPTO_REGIME_SYMBOL
                if (
                    sym
                    and config.CRYPTO_REGIME_USE_TREND
                    and self._trend_cache.get(sym) == "down"
                ):
                    self.log(
                        "debug",
                        f"{symbol}: BUY skipped — crypto regime ({sym}) "
                        f"daily trend bearish.",
                    )
                    return
                if (
                    sym
                    and config.CRYPTO_REGIME_USE_INTRADAY
                    and self._crypto_regime_down
                ):
                    self.log(
                        "debug",
                        f"{symbol}: BUY skipped — crypto regime ({sym}) "
                        f"red intraday.",
                    )
                    return
            self._open_long(symbol, analysis)
        elif signal == "SELL" and has_position and not is_crypto_symbol(symbol):
            # Stocks exit on the RSI signal. Crypto does NOT use the signal exit
            # (see _maybe_close_crypto above): the RSI>65 sell rarely fires in a
            # downtrend, so crypto exits on take-profit / stop-loss / time cap.
            self._close_long(symbol, analysis, position)
        else:
            # Quietly hold; avoid flooding the event log on every HOLD.
            pass

    def _is_fractionable(self, symbol: str) -> bool:
        """Whether ``symbol`` can be bought by a notional (fractional) amount.

        Cached per symbol. On any lookup failure, returns False so the BUY
        falls back to safe whole-share sizing.
        """
        if symbol in self._fractionable_cache:
            return self._fractionable_cache[symbol]
        try:
            asset = self.client.get_asset(symbol)
            frac = bool(asset.get("fractionable", False))
        except Exception as exc:  # noqa: BLE001 - degrade to whole shares
            self.log("error", f"{symbol}: fractionable check failed: {exc}")
            frac = False
        self._fractionable_cache[symbol] = frac
        return frac

    def _open_long(self, symbol: str, analysis: dict) -> None:
        price = analysis.get("price") or self.client.get_latest_price(symbol)
        if not price or price <= 0:
            self.log("error", f"{symbol}: no valid price; skipping BUY.")
            return

        if is_crypto_symbol(symbol):
            # Crypto trades fractionally, so a $1000 cap buys 0.0xyz BTC rather
            # than requiring a whole coin. Round to 6 dp (Alpaca's precision).
            qty = round(self.max_position_usd / price, 6)
            if qty <= 0:
                self.log("info", f"{symbol}: budget too small; skipping BUY.")
                return
            order = self.client.place_market_order(symbol, qty, "buy")
            est_qty = qty
            # Start the time-cap clock for this crypto position (see exit policy).
            self._crypto_entry_times[symbol] = datetime.now(timezone.utc)
        elif self._is_fractionable(symbol):
            # Fractionable stock: spend the whole dollar budget via a notional
            # order, so the share price never wastes budget or blocks the buy.
            order = self.client.place_market_order(
                symbol, side="buy", notional=self.max_position_usd
            )
            est_qty = round(self.max_position_usd / price, 6)
        else:
            # Non-fractionable stock: whole shares only.
            qty = int(self.max_position_usd // price)
            if qty < 1:
                self.log(
                    "info",
                    f"{symbol}: price ${price:.2f} exceeds max position "
                    f"${self.max_position_usd:.0f} and not fractionable; "
                    f"skipping BUY.",
                )
                return
            order = self.client.place_market_order(symbol, qty, "buy")
            est_qty = qty

        fill_price = order.get("filled_avg_price") or price
        filled_qty = order.get("filled_qty") or est_qty

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
        self,
        symbol: str,
        analysis: dict,
        position: dict,
        reason: str | None = None,
    ) -> None:
        qty = float(position.get("qty", 0))
        # Realized P&L at close: Alpaca's unrealized P&L on the position is the
        # gain/loss we lock in by liquidating now.
        realized_pnl = float(position.get("unrealized_pl", 0.0))

        # Close using the symbol Alpaca itself reported for the position (crypto
        # comes back slashless, e.g. "BTCUSD"), since that's the identifier its
        # close endpoint expects; "BTC/USD" with a slash would not match. The
        # trade is still recorded under the canonical watchlist `symbol` so a
        # crypto buy and its sell share one symbol.
        broker_symbol = str(position.get("symbol") or symbol)
        order = self.client.close_position(broker_symbol)
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
            "signal_reason": reason or analysis.get("reason", ""),
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

    def _maybe_close_crypto(
        self, symbol: str, position: dict, analysis: dict
    ) -> bool:
        """Apply the crypto exit policy to a held position and return True if it
        was closed. Exits on whichever fires first, measured against the
        position's average entry price (so accumulated bags use their real cost
        basis): take-profit (CRYPTO_TAKE_PROFIT_PCT), stop-loss
        (CRYPTO_STOP_LOSS_PCT), or time cap (CRYPTO_MAX_HOLD_HOURS). Each leg is
        disabled when its config value is 0. Price/time-driven — runs every
        cycle, independent of the BUY/SELL signal.
        """
        avg_entry = float(position.get("avg_entry_price", 0) or 0)
        price = float(
            position.get("current_price")
            or analysis.get("price")
            or self.client.get_latest_price(symbol)
            or 0.0
        )
        if avg_entry <= 0 or price <= 0:
            return False  # can't evaluate the policy; hold

        tp = config.CRYPTO_TAKE_PROFIT_PCT
        sl = config.CRYPTO_STOP_LOSS_PCT
        cap_h = config.CRYPTO_MAX_HOLD_HOURS
        gain = price / avg_entry - 1

        reason: str | None = None
        if tp and gain >= tp:
            reason = f"Crypto take-profit (+{gain * 100:.1f}% ≥ {tp * 100:.0f}%)."
        elif sl and gain <= -sl:
            reason = f"Crypto stop-loss ({gain * 100:.1f}% ≤ -{sl * 100:.0f}%)."
        elif cap_h:
            entry_ts = self._crypto_entry_times.get(symbol)
            if entry_ts is None:
                # First sighting (e.g. position predates a restart): start the
                # time-cap clock now. TP/SL above already applied this cycle.
                self._crypto_entry_times[symbol] = datetime.now(timezone.utc)
            else:
                held_h = (
                    datetime.now(timezone.utc) - entry_ts
                ).total_seconds() / 3600.0
                if held_h >= cap_h:
                    reason = f"Crypto time cap ({held_h:.0f}h ≥ {cap_h:.0f}h)."

        if reason is None:
            return False

        self.log("info", f"{symbol}: {reason}")
        self._close_long(symbol, analysis, position, reason=reason)
        self._crypto_entry_times.pop(symbol, None)
        return True

    # -- Momentum rotation -----------------------------------------------
    def _run_momentum_cycle(self) -> None:
        """One cycle of the weekly momentum rotation. Cheap on ordinary days —
        it only records a snapshot; the heavy rebalance runs at most once per
        ISO week (see ``_maybe_rebalance_momentum``). Unlike the RSI cycle it
        never flattens positions: momentum holds names for weeks."""
        self.log("info", f"Cycle {self.cycle_count} (momentum) started.")
        try:
            self._maybe_rebalance_momentum()
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            self.log("error", f"momentum rebalance failed: {exc}")
        try:
            self._record_snapshot_and_log_fund()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"snapshot failed: {exc}")
        self.last_cycle_at = _utc_now_iso()
        self.log("info", f"Cycle {self.cycle_count} complete.")

    def _record_snapshot_and_log_fund(self) -> None:
        """Record the account snapshot AND log a one-line fund summary, reusing a
        single account+positions fetch. The summary line surfaces the current
        holdings + live P&L in the headless journal (what the systemd service
        shows), so the fund is visible without opening the dashboard."""
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
        holdings = [
            p for p in positions
            if not is_crypto_symbol(str(p.get("symbol")))
            and str(p.get("symbol")) not in self._crypto_position_aliases
        ]
        if not holdings:
            return
        market_value = sum(float(p.get("market_value") or 0) for p in holdings)
        pnl = sum(float(p.get("unrealized_pl") or 0) for p in holdings)
        cost = sum(float(p.get("cost_basis") or 0) for p in holdings)
        pct = (pnl / cost * 100.0) if cost else 0.0
        self.log(
            "info",
            f"Fund: {len(holdings)} holdings, value ${market_value:,.0f}, "
            f"unrealized P&L {'+' if pnl >= 0 else ''}${pnl:,.0f} ({pct:+.1f}%).",
        )

    def _maybe_rebalance_momentum(self) -> None:
        """Rebalance once per ISO week, on the first market-open cycle on/after
        the configured weekday. Idempotent within a week via
        ``_last_rebalance_week``; robust to downtime (catches up later in the
        week)."""
        # Work in US/Eastern so the weekday + midday-hour cadence line up with the
        # actual trading session (the ISO week is stable within a session).
        now_et = datetime.now(_ET)
        iso_year, iso_week, iso_weekday = now_et.isocalendar()  # weekday 1=Mon..7=Sun
        week_key = f"{iso_year}-W{iso_week:02d}"
        if self._last_rebalance_week == week_key:
            return  # already rebalanced this week
        # The first rebalance after a (re)start fires as soon as the market is
        # open — regardless of the configured weekday/hour — so a fresh deploy
        # invests right away. Recurring rebalances hold to the weekly cadence:
        # on/after the configured weekday AND past the midday hour (calmer fills).
        if self._did_initial_rebalance:
            if (iso_weekday - 1) < config.MOMENTUM_REBALANCE_WEEKDAY:
                return  # not yet the rebalance weekday this week
            if now_et.hour < config.MOMENTUM_REBALANCE_HOUR_ET:
                return  # wait for the calmer midday session
        if not self._market_open_for_rebalance():
            return
        self._rebalance_momentum()
        self._last_rebalance_week = week_key
        self._did_initial_rebalance = True

    def _market_open_for_rebalance(self) -> bool:
        """True when the stock market is open and outside the closing blackout —
        a clean window for rebalance fills. Never flattens (momentum holds for
        weeks). On any clock failure, defer the rebalance."""
        try:
            clock = self.client.get_clock()
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"clock unavailable; deferring rebalance: {exc}")
            return False
        if not clock.get("is_open"):
            return False
        next_close = clock.get("next_close")
        if next_close is not None:
            secs_to_close = (next_close - datetime.now(timezone.utc)).total_seconds()
            if 0 <= secs_to_close < config.MARKET_BLACKOUT_MINUTES * 60:
                return False
        return True

    def _rebalance_momentum(self) -> None:
        """The weekly rotation: score the universe, sell names that fell out of
        the target, and buy new entrants at equal weight. Existing holders that
        remain in the target are left to run (keeps turnover low)."""
        universe = [
            s for s in self.watchlist
            if not is_crypto_symbol(s) and s not in ("SPY", "QQQ")
        ]
        self.log("info", f"Momentum rebalance: scoring {len(universe)} names...")
        bars_by: dict[str, Any] = {}
        for i, symbol in enumerate(universe, start=1):
            try:
                df = self.client.get_bars(
                    symbol, "1Day", config.MOMENTUM_BARS_LOOKBACK
                )
                if df is not None and not df.empty:
                    bars_by[symbol] = df
            except Exception as exc:  # noqa: BLE001 - isolate per-symbol faults
                self.log("error", f"{symbol}: momentum bars failed: {exc}")
            if i % 50 == 0:
                self.log("info", f"Momentum data: {i}/{len(universe)}...")

        target = momentum.select_top(
            bars_by,
            lookback=config.MOMENTUM_LOOKBACK_DAYS,
            skip=config.MOMENTUM_SKIP_DAYS,
            sma_window=config.MOMENTUM_SMA_WINDOW,
            top_n=config.MOMENTUM_TOP_N,
        )
        if not target:
            self.log(
                "warning",
                "Momentum: no eligible names (all below their 200d SMA?); "
                "holding current book.",
            )
            return
        self.log("info", f"Momentum target ({len(target)}): {', '.join(target)}")

        # Current *stock* positions only — the momentum book is stocks; any
        # residual crypto is left alone (it winds down via its own policy under
        # the RSI strategy). Alpaca reports crypto slashless, so exclude both.
        try:
            current = {
                str(p.get("symbol")): p
                for p in self.client.get_positions()
                if not is_crypto_symbol(str(p.get("symbol")))
                and str(p.get("symbol")) not in self._crypto_position_aliases
            }
        except Exception as exc:  # noqa: BLE001
            self.log(
                "error",
                f"Momentum: could not fetch positions; aborting rebalance: {exc}",
            )
            return
        target_set = set(target)

        # 1) SELL names that dropped out of the target (frees cash for entrants).
        for symbol, position in current.items():
            if symbol in target_set:
                continue
            try:
                self._close_long(
                    symbol,
                    {"price": position.get("current_price")},
                    position,
                    reason="Momentum exit: out of top-N / below 200d SMA.",
                )
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"Momentum sell failed for {symbol}: {exc}")

        # 2) BUY new entrants at equal weight (equity / N). Held targets ride.
        try:
            equity = float(self.client.get_account().get("equity", 0.0))
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"Momentum: no account equity; skipping buys: {exc}")
            return
        budget = equity / config.MOMENTUM_TOP_N if config.MOMENTUM_TOP_N else 0.0
        if budget <= 0:
            self.log("warning", "Momentum: non-positive per-name budget; skipping buys.")
            return
        for symbol in target:
            if symbol in current:
                continue  # already held; let the winner run
            try:
                self._place_buy(
                    symbol,
                    budget,
                    "Momentum entry: top-N by 12-1 momentum, above 200d SMA.",
                )
            except Exception as exc:  # noqa: BLE001
                self.log("error", f"Momentum buy failed for {symbol}: {exc}")
        self.log("info", "Momentum rebalance complete.")

    def _place_buy(self, symbol: str, budget: float, reason: str) -> None:
        """Buy ~``budget`` dollars of a stock (notional if fractionable, else
        whole shares) and record the trade. Shared by the momentum rebalancer."""
        # Alpaca rejects notional (dollar) orders with more than 2 decimal
        # places, and equity/N is usually a repeating decimal — round to cents.
        budget = round(budget, 2)
        price = self.client.get_latest_price(symbol)
        if not price or price <= 0:
            self.log("error", f"{symbol}: no valid price; skipping BUY.")
            return
        if self._is_fractionable(symbol):
            order = self.client.place_market_order(
                symbol, side="buy", notional=budget
            )
            est_qty = round(budget / price, 6)
        else:
            qty = int(budget // price)
            if qty < 1:
                self.log(
                    "info",
                    f"{symbol}: ${price:.2f} exceeds per-name budget "
                    f"${budget:.0f} and not fractionable; skipping BUY.",
                )
                return
            order = self.client.place_market_order(symbol, qty, "buy")
            est_qty = qty

        fill_price = order.get("filled_avg_price") or price
        filled_qty = order.get("filled_qty") or est_qty
        trade = {
            "symbol": symbol,
            "side": "buy",
            "qty": filled_qty,
            "price": fill_price,
            "total_value": round(filled_qty * fill_price, 2),
            "signal_reason": reason,
            "timestamp": _utc_now_iso(),
            "pnl_at_close": None,
        }
        trade_id = self.db.record_trade(trade)
        self.log(
            "trade",
            f"BUY {filled_qty} {symbol} @ ${fill_price:.2f} "
            f"(${trade['total_value']:.2f}).",
        )
        self._attach_commentary(
            trade_id, symbol, {"reason": reason, "price": fill_price}, trade
        )

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
