"""Streamlit browser dashboard for the AI trading bot.

Run from the ``trading-bot/`` directory with:

    streamlit run dashboard.py

It reads the same SQLite database the bot writes to (``data/trades.db``), so it
works whether the bot is running live (``python -m bot.main``) or is stopped —
in which case it shows the last persisted history.

Open positions, the live account line, and the watchlist prices are fetched
from Alpaca when keys are configured; everything else (equity curve, trades,
P&L, stats) comes from the database and works with no keys at all.

Layout: two tabs — **Overview** (account, equity curve, positions, trades,
commentary) and **Watchlist** (latest polled price per symbol). Each tab's data
auto-refreshes in place via fragments, so switching tabs is sticky.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from bot import algorithm, config, momentum
from bot.alpaca_client import is_crypto_symbol
from bot.analytics import pair_round_trips
from bot.database import Database

st.set_page_config(page_title="AI Trading Bot", page_icon="📈", layout="wide")


# ---------------------------------------------------------------------------
# Shared resources (built once per session, not per rerun)
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db() -> Database:
    return Database(config.DB_PATH)


@st.cache_data(ttl=60, show_spinner=False)
def _cached_bars(_client: Any, symbol: str, timeframe: str, limit: int):
    """Cache chart bars briefly so repeat views / tab switches don't refetch.

    ``_client`` is underscore-prefixed so Streamlit doesn't try to hash it; the
    cache key is (symbol, timeframe, limit). The cache lives in the long-running
    dashboard server, so once any visit warms it, later visits load instantly.
    The "Refresh chart" button clears it on demand.
    """
    return _client.get_bars(symbol, timeframe, limit)


@st.cache_resource
def get_client() -> Any | None:
    """Return a verified AlpacaClient, or None if keys are absent/invalid."""
    if not (config.ALPACA_API_KEY and config.ALPACA_SECRET_KEY):
        return None
    try:
        from bot.alpaca_client import AlpacaClient

        client = AlpacaClient()
        client.verify_connection()
        return client
    except Exception:  # noqa: BLE001 - dashboard must render without live data
        return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def fmt_pnl(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if v >= 0 else ''}${v:,.2f}"


def human_span(minutes: float) -> str:
    """Human-readable duration: 105 -> '~2 h', 12960 -> '~9 days'."""
    if minutes >= 1440:
        return f"~{minutes / 1440:.0f} days"
    if minutes >= 60:
        return f"~{minutes / 60:.0f} h"
    return f"~{minutes:.0f} min"


db = get_db()
client = get_client()


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------
st.sidebar.title("📈 AI Trading Bot")
auto = st.sidebar.checkbox("Auto-refresh", value=True)
interval = st.sidebar.slider("Refresh interval (seconds)", 5, 60, 10)
refresh_every = f"{interval}s" if auto else None

if client is not None:
    st.sidebar.success("Connected to Alpaca (live data).")
else:
    st.sidebar.info("No Alpaca keys — showing saved history only.")

# Which strategy is live drives the whole UI framing (fund vs. intraday).
if config.STRATEGY == "momentum":
    st.sidebar.success(
        f"Strategy: **Momentum rotation** — top {config.MOMENTUM_TOP_N}, "
        f"rebalanced weekly."
    )
else:
    st.sidebar.info("Strategy: **RSI intraday**.")

st.sidebar.caption(f"Watchlist: {len(config.WATCHLIST)} symbols.")


# ---------------------------------------------------------------------------
# Momentum target (expensive: scans the whole universe). Cached 30 min and only
# computed on demand, so it never runs on an ordinary auto-refresh.
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner="Scoring the universe…")
def compute_momentum_target(_client: Any) -> list[str]:
    universe = [
        s for s in config.WATCHLIST
        if not is_crypto_symbol(s) and s not in ("SPY", "QQQ")
    ]
    bars: dict[str, Any] = {}
    for symbol in universe:
        try:
            df = _client.get_bars(symbol, "1Day", config.MOMENTUM_BARS_LOOKBACK)
            if df is not None and not df.empty:
                bars[symbol] = df
        except Exception:  # noqa: BLE001 - skip a symbol that won't fetch
            pass
    return momentum.select_top(
        bars,
        lookback=config.MOMENTUM_LOOKBACK_DAYS,
        skip=config.MOMENTUM_SKIP_DAYS,
        sma_window=config.MOMENTUM_SMA_WINDOW,
        top_n=config.MOMENTUM_TOP_N,
    )


# ---------------------------------------------------------------------------
# Fund tab: the momentum book — current holdings with live per-stock gain/loss.
# ---------------------------------------------------------------------------
@st.fragment(run_every=refresh_every)
def render_fund() -> None:
    if client is None:
        st.info("Connect Alpaca keys to see the live fund holdings.")
        return
    try:
        account = client.get_account()
        positions = client.get_positions()
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Live fetch failed: {exc}")
        return

    # The momentum fund is stocks only; exclude any residual crypto.
    holdings = [
        p for p in positions if not is_crypto_symbol(str(p.get("symbol", "")))
    ]
    equity = float(account.get("equity") or 0.0)
    cash = float(account.get("cash") or 0.0)
    total_mv = sum(float(p.get("market_value") or 0.0) for p in holdings)
    total_pnl = sum(float(p.get("unrealized_pl") or 0.0) for p in holdings)
    total_cost = sum(float(p.get("cost_basis") or 0.0) for p in holdings)
    total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost else 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Holdings", f"{len(holdings)}")
    m2.metric(
        "Invested",
        fmt_money(total_mv),
        f"{(total_mv / equity * 100):.0f}% of equity" if equity else None,
    )
    m3.metric("Unrealized P&L", fmt_pnl(total_pnl), f"{total_pnl_pct:+.2f}%")
    m4.metric("Cash", fmt_money(cash))

    if not holdings:
        st.caption(
            "No stock holdings yet — the momentum book fills at the **next "
            "market open** (the first rebalance after a restart is immediate), "
            "then rebalances weekly on Tuesdays around noon ET."
        )
        return

    rows = []
    for p in holdings:
        mv = float(p.get("market_value") or 0.0)
        rows.append(
            {
                "Symbol": str(p.get("symbol", "")),
                "Weight": (mv / total_mv * 100.0) if total_mv else 0.0,
                "Qty": float(p.get("qty", 0.0)),
                "Avg entry": float(p.get("avg_entry_price") or 0.0),
                "Price": float(p.get("current_price") or 0.0),
                "Value": mv,
                "P&L $": float(p.get("unrealized_pl") or 0.0),
                "P&L %": float(p.get("unrealized_plpc") or 0.0) * 100.0,
            }
        )
    df = pd.DataFrame(rows).sort_values("Weight", ascending=False)
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={
            "Weight": st.column_config.NumberColumn("Weight", format="%.1f%%"),
            "Qty": st.column_config.NumberColumn(format="%.4f"),
            "Avg entry": st.column_config.NumberColumn(format="$%.2f"),
            "Price": st.column_config.NumberColumn(format="$%.2f"),
            "Value": st.column_config.NumberColumn(format="$%.2f"),
            "P&L $": st.column_config.NumberColumn(format="$%.2f"),
            "P&L %": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    best = df.loc[df["P&L %"].idxmax()]
    worst = df.loc[df["P&L %"].idxmin()]
    st.caption(
        f"🟢 Best: {best['Symbol']} {best['P&L %']:+.1f}%  ·  "
        f"🔴 Worst: {worst['Symbol']} {worst['P&L %']:+.1f}%  ·  "
        f"updated {datetime.now():%H:%M:%S}"
    )

    # Drift: what the strategy would pick right now vs. what we actually hold.
    if config.STRATEGY == "momentum":
        with st.expander("🎯 Momentum target vs. current book (drift)"):
            st.caption(
                f"The top {config.MOMENTUM_TOP_N} the strategy would pick now. "
                "Scanning the whole universe (~325 names) takes a few seconds."
            )
            if st.button("Compute current target"):
                target = compute_momentum_target(client)
                if not target:
                    st.caption("Could not compute a target (insufficient history).")
                else:
                    held = {r["Symbol"] for r in rows}
                    tset = set(target)
                    st.write(
                        f"**Target ({len(target)}):** {', '.join(target)}"
                    )
                    to_sell = sorted(held - tset)
                    to_buy = [s for s in target if s not in held]
                    if to_sell:
                        st.markdown(
                            ":red[Would **sell** next rebalance:] " + ", ".join(to_sell)
                        )
                    if to_buy:
                        st.markdown(
                            ":green[Would **buy** next rebalance:] " + ", ".join(to_buy)
                        )
                    if not to_sell and not to_buy:
                        st.success("The book already matches the target. ✅")


# ---------------------------------------------------------------------------
# Overview tab content (auto-refreshing fragment)
# ---------------------------------------------------------------------------
@st.fragment(run_every=refresh_every)
def render_overview() -> None:
    # Prefer the live account; fall back to the latest persisted snapshot.
    account: dict[str, Any] = {}
    positions: list[dict[str, Any]] = []
    if client is not None:
        try:
            account = client.get_account()
            positions = client.get_positions()
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Live fetch failed, showing saved snapshot: {exc}")
    if not account:
        snap = db.get_latest_snapshot()
        if snap:
            account = {
                "equity": snap.get("equity"),
                "cash": snap.get("cash"),
                "buying_power": snap.get("buying_power"),
            }

    stats = db.get_trade_stats()
    net_pnl = db.get_net_pnl()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Equity", fmt_money(account.get("equity")))
    c2.metric("Cash", fmt_money(account.get("cash")))
    c3.metric("Buying power", fmt_money(account.get("buying_power")))
    c4.metric("Net realized P&L", fmt_pnl(net_pnl))
    c5.metric(
        "Trades / win rate",
        f"{stats.get('total_trades', 0)} / {stats.get('win_rate', 0.0) * 100:.0f}%",
    )

    st.subheader("Equity curve")
    snapshots = db.get_snapshots(limit=1000)
    if snapshots:
        eq = pd.DataFrame(snapshots)
        eq["timestamp"] = pd.to_datetime(eq["timestamp"], errors="coerce")
        eq = eq.dropna(subset=["timestamp"]).set_index("timestamp")
        st.line_chart(eq[["equity", "cash"]])
    else:
        st.caption("No snapshots yet — the bot records one after each cycle.")

    left, right = st.columns(2)
    with left:
        st.subheader("Open positions")
        if positions:
            pos_df = pd.DataFrame(positions)
            pos_df["unrealized_plpc"] = (
                pd.to_numeric(pos_df.get("unrealized_plpc"), errors="coerce") * 100.0
            )
            pos_df = pos_df[
                ["symbol", "qty", "current_price", "market_value",
                 "unrealized_pl", "unrealized_plpc"]
            ]
            st.dataframe(
                pos_df, hide_index=True, width="stretch",
                column_config={
                    "current_price": st.column_config.NumberColumn("Price", format="$%.2f"),
                    "market_value": st.column_config.NumberColumn("Value", format="$%.2f"),
                    "unrealized_pl": st.column_config.NumberColumn("P&L $", format="$%.2f"),
                    "unrealized_plpc": st.column_config.NumberColumn("P&L %", format="%.2f%%"),
                },
            )
        elif client is None:
            st.caption("Connect Alpaca keys to see live positions.")
        else:
            st.caption("No open positions.")
    with right:
        st.subheader("Recent trades")
        trades = db.get_trades(limit=50)
        if trades:
            tr_df = pd.DataFrame(trades)[
                ["timestamp", "side", "symbol", "qty", "price", "pnl_at_close"]
            ]
            st.dataframe(tr_df, hide_index=True, width="stretch")
        else:
            st.caption("No trades yet — signals are rare by design.")

    st.subheader("Closed round trips")
    round_trips = pair_round_trips(db.get_trades(limit=500))
    if round_trips:
        rt_df = pd.DataFrame(round_trips)[
            ["symbol", "qty", "entry_price", "exit_price", "pnl", "pnl_pct",
             "entry_time", "exit_time"]
        ]
        st.dataframe(
            rt_df,
            hide_index=True,
            width="stretch",
            column_config={
                "entry_price": st.column_config.NumberColumn(
                    "Bought @", format="$%.2f"
                ),
                "exit_price": st.column_config.NumberColumn(
                    "Sold @", format="$%.2f"
                ),
                "pnl": st.column_config.NumberColumn("P&L $", format="$%.2f"),
                "pnl_pct": st.column_config.NumberColumn("P&L %", format="%.2f%%"),
            },
        )
    else:
        st.caption(
            "No closed round trips yet — each appears here once a buy is later "
            "sold, pairing the entry and exit price with the P&L."
        )

    st.subheader("Latest AI commentary")
    commentary = db.get_latest_commentary()
    st.write(commentary or "_No AI commentary yet (needs an Anthropic API key)._")

    st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')}")


# ---------------------------------------------------------------------------
# Watchlist tab content (auto-refreshing fragment): latest polled price/symbol
# ---------------------------------------------------------------------------
@st.fragment(run_every=refresh_every)
def render_watchlist() -> None:
    if client is None:
        st.info("Connect Alpaca keys to see live prices.")
        st.dataframe(
            pd.DataFrame(
                {
                    "Symbol": config.WATCHLIST,
                    "Type": [
                        "crypto" if is_crypto_symbol(s) else "stock"
                        for s in config.WATCHLIST
                    ],
                }
            ),
            hide_index=True,
            width="stretch",
        )
        return

    now = datetime.now().strftime("%H:%M:%S")
    # One batched request per asset class for the whole watchlist, instead of a
    # per-symbol call — turns ~100 sequential round-trips into ~2 (fast on a Pi).
    try:
        prices = client.get_latest_prices(config.WATCHLIST)
    except Exception:  # noqa: BLE001 - a failed fetch shouldn't blank the table
        prices = {}
    rows: list[dict[str, Any]] = []
    for symbol in config.WATCHLIST:
        price = prices.get(symbol) or None  # 0.0 / missing => unavailable
        rows.append(
            {
                "Symbol": symbol,
                "Type": "crypto" if is_crypto_symbol(symbol) else "stock",
                "Latest price": price,
                "As of": now,
            }
        )

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={
            "Latest price": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    st.caption(
        "Prices are polled live from Alpaca on each refresh "
        "(latest quote mid-price)."
    )


# ---------------------------------------------------------------------------
# Chart tab content: one symbol's strategy view.
#
# This fragment is NOT on a timer (no ``run_every``): the longer ranges pull
# thousands of bars, so it would be wasteful to refetch every few seconds. It
# reloads only when you change the symbol/range or press "Refresh chart".
# ---------------------------------------------------------------------------
@st.fragment
def render_chart() -> None:
    if client is None:
        st.info("Connect Alpaca keys to chart live data.")
        return

    # Bar interval drives the EMA horizon: the bot's EMA(9)/EMA(21) on daily
    # bars are 9-/21-DAY EMAs, on hourly bars 9-/21-HOUR, etc. (tf string,
    # minutes/bar, crypto bars/day, stock bars/day).
    intervals = {
        "5 min": ("5Min", 5, 288, 78),
        "Hourly": ("1Hour", 60, 24, 7),
        "Daily": ("1Day", 1440, 1, 0.72),
    }
    # History options per interval, as (label, calendar days).
    ranges = {
        "5 min": [("1 day", 1), ("3 days", 3), ("1 week", 7)],
        "Hourly": [("3 days", 3), ("2 weeks", 14), ("1 month", 30)],
        "Daily": [("3 months", 90), ("6 months", 180), ("1 year", 365)],
    }

    top = st.columns([2, 2, 3])
    with top[0]:
        symbol = st.selectbox("Symbol", config.WATCHLIST, key="chart_symbol")
    with top[1]:
        interval_label = st.radio(
            "Bar interval", list(intervals), index=2, key="chart_interval"
        )
    tf_str, minutes_per_bar, cpd, spd = intervals[interval_label]
    range_opts = ranges[interval_label]
    with top[2]:
        # Key includes the interval so the options reset cleanly when it changes.
        range_label = st.radio(
            "History",
            [r[0] for r in range_opts],
            index=0,
            horizontal=True,
            key=f"chart_range_{interval_label}",
        )
    range_days = dict(range_opts)[range_label]
    bars_per_day = cpd if is_crypto_symbol(symbol) else spd
    display_bars = max(40, int(round(range_days * bars_per_day)))

    btn_col, info_col = st.columns([1, 4])
    with btn_col:
        if st.button("🔄 Refresh chart", key="chart_refresh", width="stretch"):
            _cached_bars.clear()  # force a fresh fetch past the cache TTL
    with info_col:
        st.caption(
            f"EMA{config.EMA_FAST_PERIOD} ≈ "
            f"{human_span(config.EMA_FAST_PERIOD * minutes_per_bar)}, "
            f"EMA{config.EMA_SLOW_PERIOD} ≈ "
            f"{human_span(config.EMA_SLOW_PERIOD * minutes_per_bar)} at this "
            f"interval · loaded {datetime.now():%H:%M:%S} (manual refresh)."
        )

    # Fetch extra "warm-up" bars *before* the visible window so the EMAs/RSI
    # enter the chart already converged, instead of starting cold at the left
    # edge. Compute indicators over the full series, then show only the most
    # recent ``display_bars``.
    warmup_bars = 3 * max(config.EMA_SLOW_PERIOD, config.RSI_PERIOD)
    try:
        bars = _cached_bars(client, symbol, tf_str, display_bars + warmup_bars)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not fetch bars for {symbol}: {exc}")
        return

    ind = algorithm.indicator_frame(bars).tail(display_bars)
    if ind.empty:
        st.caption("No data for this symbol yet.")
        return
    # Show timestamps as UTC wall-clock (drop tz) so the axis renders stably
    # rather than shifting into the browser's local zone.
    if getattr(ind.index, "tz", None) is not None:
        ind = ind.copy()
        ind.index = ind.index.tz_convert("UTC").tz_localize(None)

    fast, slow = config.EMA_FAST_PERIOD, config.EMA_SLOW_PERIOD
    buy, sell = config.RSI_BUY_THRESHOLD, config.RSI_SELL_THRESHOLD

    # Current signal summary (same logic the bot trades on).
    analysis = algorithm.analyze(symbol, bars)
    sig = analysis["signal"]
    color = {"BUY": "green", "SELL": "red"}.get(sig, "gray")
    st.markdown(f"**Signal:** :{color}[{sig}]")
    st.caption(analysis.get("reason", ""))

    # Price with both EMAs overlaid, plus BUY/SELL markers where the strategy
    # fired on a bar. Built with Altair so we can layer points on the lines.
    st.markdown(
        f"**Price with EMA{fast} (fast) / EMA{slow} (slow)** "
        "— :green[▲ BUY]  :red[▼ SELL]"
    )
    plot = ind.rename_axis("timestamp").reset_index()
    line_long = plot.melt(
        id_vars="timestamp",
        value_vars=["close", "ema_fast", "ema_slow"],
        var_name="series",
        value_name="value",
    )
    label_map = {
        "close": "Price",
        "ema_fast": f"EMA{fast}",
        "ema_slow": f"EMA{slow}",
    }
    line_long["series"] = line_long["series"].map(label_map)
    axis_fmt = "%b %d" if minutes_per_bar >= 1440 else "%b %d %H:%M"
    lines = (
        alt.Chart(line_long)
        .mark_line()
        .encode(
            x=alt.X(
                "timestamp:T",
                title=None,
                axis=alt.Axis(format=axis_fmt, labelAngle=0),
            ),
            y=alt.Y("value:Q", title="Price", scale=alt.Scale(zero=False)),
            color=alt.Color(
                "series:N",
                title=None,
                sort=["Price", f"EMA{fast}", f"EMA{slow}"],
            ),
        )
    )

    # Signal at each bar, then isolate the BUY/SELL bars for markers.
    plot["signal"] = algorithm.signal_series(ind, symbol).to_numpy()
    buys = plot[plot["signal"] == "BUY"]
    sells = plot[plot["signal"] == "SELL"]
    layers = [lines]
    if not buys.empty:
        layers.append(
            alt.Chart(buys)
            .mark_point(shape="triangle-up", size=140, filled=True, color="#2ecc71")
            .encode(x="timestamp:T", y="close:Q")
        )
    if not sells.empty:
        layers.append(
            alt.Chart(sells)
            .mark_point(shape="triangle-down", size=140, filled=True, color="#e74c3c")
            .encode(x="timestamp:T", y="close:Q")
        )
    # Horizontal date labels (labelAngle=0 above) — the working RSI chart proves
    # horizontal time labels render cleanly in this Streamlit/Vega version, while
    # the old -40° rotation pushed the month below the box and clipped it.
    chart = alt.layer(*layers).properties(width="container", height=420).interactive()
    # theme=None: render the Altair spec as-is instead of letting Streamlit
    # reskin it. Streamlit's default theme on a custom (layered, temporal-axis)
    # spec broke the x-axis labels after the Streamlit 1.58 / Altair 6 upgrade.
    st.altair_chart(chart, theme=None)

    # What's actually loaded: bar count and the span the bars cover.
    span_start = pd.to_datetime(ind.index[0])
    span_end = pd.to_datetime(ind.index[-1])
    st.caption(
        f"📊 {len(ind):,} {interval_label} bars · "
        f"{span_start:{axis_fmt}} – {span_end:{axis_fmt}} UTC · "
        f"markers reflect the strategy on these {interval_label} bars "
        f"(the live bot trades on {config.BAR_TIMEFRAME_MINUTES}-min bars)."
    )

    n_sig = int((plot["signal"] == "BUY").sum() + (plot["signal"] == "SELL").sum())
    if n_sig == 0:
        st.caption(
            "No BUY/SELL signals fired in this window — mostly HOLD is normal."
        )
    else:
        st.caption(
            f"{n_sig} signal bar(s) marked: "
            f"{int((plot['signal'] == 'BUY').sum())} BUY, "
            f"{int((plot['signal'] == 'SELL').sum())} SELL."
        )

    # RSI with oversold/overbought bands drawn as flat reference lines.
    st.markdown(
        f"**RSI({config.RSI_PERIOD})** — below {int(buy)} = oversold, "
        f"above {int(sell)} = overbought"
    )
    rsi_df = pd.DataFrame(
        {
            "RSI": ind["rsi"],
            f"Oversold ({int(buy)})": buy,
            f"Overbought ({int(sell)})": sell,
        },
        index=ind.index,
    )
    st.line_chart(rsi_df)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
fund_tab, overview_tab, watchlist_tab, chart_tab = st.tabs(
    ["Fund", "Overview", "Watchlist", "Chart"]
)
with fund_tab:
    st.title("Fund holdings")
    render_fund()
with overview_tab:
    st.title("Account")
    render_overview()
with watchlist_tab:
    st.title("Watchlist prices")
    render_watchlist()
with chart_tab:
    st.title("Strategy chart")
    render_chart()
