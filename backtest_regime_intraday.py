"""Backtest SPY-regime filters against trades.db using REAL SPY bars.

Round-trips are reconstructed by FIFO-matching sells to their entry buys (so each
closed lot carries its entry timestamp, entry RSI, and realized P&L). For each
STOCK round-trip we evaluate a regime gate AT THE BUY'S TIMESTAMP and drop the
trip if the gate says "down". Crypto is always exempt. RSI filter fixed at <45
(established as the best threshold). Filters compared:

  none            baseline, no regime gate
  dev-EMA 3/6     developing-candle: daily SPY EMA(3) vs EMA(6), where TODAY's
                  bar is the live 5-min price at buy time  (the user's idea)
  dev-EMA 5/10    same, slightly slower (less whipsaw)
  red-vs-close    SPY's live price < prior daily close      (simple, robust)
  price<SMA20     SPY live price < its 20-day SMA           (classic trend filter)

Data: spy_daily.csv, spy_5min.csv (fetched from Alpaca), data/trades.db.
"""
import re
import sqlite3
from collections import defaultdict, deque

import pandas as pd

# ---- load SPY reference data ----------------------------------------------
daily = pd.read_csv("spy_daily.csv", parse_dates=["timestamp"]).sort_values("timestamp")
daily["date"] = daily["timestamp"].dt.date
daily_close = list(zip(daily["date"], daily["close"]))           # [(date, close)]

mins = pd.read_csv("spy_5min.csv", parse_dates=["timestamp"]).sort_values("timestamp")
mins_ts = pd.DatetimeIndex(mins["timestamp"])                    # tz-aware UTC
mins_close = mins["close"].to_numpy()


def spy_live_at(ts):
    """SPY 5-min close at or before ts (the 'developing' price)."""
    pos = mins_ts.searchsorted(ts, side="right") - 1
    return float(mins_close[pos]) if pos >= 0 else None


def daily_body(before_date):
    """SPY daily closes strictly before `before_date` (the settled bars)."""
    return [c for d, c in daily_close if d < before_date]


def ema_last(series, span):
    return pd.Series(series).ewm(span=span, adjust=False).mean().iloc[-1]


# ---- regime gates: return True if "down" (block stock buys) ----------------
def gate_dev_ema(ts, date, fast, slow):
    body = daily_body(date)
    tip = spy_live_at(ts)
    if tip is None or len(body) < slow:
        return False                       # fail open
    series = body + [tip]
    return ema_last(series, fast) < ema_last(series, slow)


def gate_red_vs_close(ts, date, *_):
    body = daily_body(date)
    tip = spy_live_at(ts)
    if tip is None or not body:
        return False
    return tip < body[-1]


def gate_price_below_sma20(ts, date, *_):
    body = daily_body(date)
    tip = spy_live_at(ts)
    if tip is None or len(body) < 20:
        return False
    return tip < sum(body[-20:]) / 20.0


GATES = {
    "none":          None,
    "dev-EMA 3/6":   lambda ts, d: gate_dev_ema(ts, d, 3, 6),
    "dev-EMA 5/10":  lambda ts, d: gate_dev_ema(ts, d, 5, 10),
    "red-vs-close":  lambda ts, d: gate_red_vs_close(ts, d),
    "price<SMA20":   lambda ts, d: gate_price_below_sma20(ts, d),
}

# ---- reconstruct round-trips (FIFO), carrying buy timestamp ----------------
con = sqlite3.connect("data/trades.db")
crypto_bases = {s.replace("/", "") for (s,) in
                con.execute("SELECT DISTINCT symbol FROM trades WHERE symbol LIKE '%/%'")}


def norm(s):
    return s.replace("/", "")


def is_crypto(s):
    return "/" in s or norm(s) in crypto_bases


def entry_rsi(reason):
    m = re.search(r"RSI ([0-9.]+)", reason or "")
    return float(m.group(1)) if m else None


rows = con.execute(
    "SELECT symbol, side, qty, signal_reason, timestamp, pnl_at_close "
    "FROM trades ORDER BY timestamp ASC"
).fetchall()

open_lots = defaultdict(deque)
trips = []
for sym, side, qty, reason, ts, pnl in rows:
    key = norm(sym)
    if side == "buy":
        open_lots[key].append({"qty": qty, "rsi": entry_rsi(reason), "ts": ts})
    else:
        remaining, sold = qty, (qty or 1.0)
        q = open_lots[key]
        while remaining > 1e-9 and q:
            lot = q[0]
            take = min(remaining, lot["qty"])
            tsp = pd.Timestamp(lot["ts"])
            trips.append({
                "kind": "crypto" if is_crypto(sym) else "stock",
                "rsi": lot["rsi"], "ts": tsp, "date": tsp.date(),
                "pnl": (take / sold) * (pnl or 0.0),
            })
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-9:
                q.popleft()

# keep tradeable population: RSI < 45, scored (has pnl)
pop = [t for t in trips if t["rsi"] is not None and t["rsi"] < 45]


def wl(ts):
    w = sum(1 for t in ts if t["pnl"] > 0)
    l = sum(1 for t in ts if t["pnl"] < 0)
    return w, l, 100 * w / (w + l) if (w + l) else 0.0


# ---- run each gate ---------------------------------------------------------
print("Tradeable round-trips (RSI<45): %d  |  stock=%d  crypto=%d\n" % (
    len(pop),
    sum(1 for t in pop if t["kind"] == "stock"),
    sum(1 for t in pop if t["kind"] == "crypto"),
))
print("%-14s %-6s %-11s %-7s %-7s %s" %
      ("regime", "trips", "net P&L", "win%", "blockd", "P&L of blocked stock buys"))
for name, gate in GATES.items():
    kept, blocked = [], []
    for t in pop:
        if gate and t["kind"] == "stock" and gate(t["ts"], t["date"]):
            blocked.append(t)
        else:
            kept.append(t)
    net = sum(t["pnl"] for t in kept)
    w, l, wr = wl(kept)
    bnet = sum(t["pnl"] for t in blocked)
    print("%-14s %-6d %-11.2f %-7.1f %-7d %+.2f (n=%d)" %
          (name, len(kept), net, wr, len(blocked), bnet, len(blocked)))
