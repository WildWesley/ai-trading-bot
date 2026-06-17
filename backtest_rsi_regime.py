"""Replay trades.db under different RSI thresholds and SPY-regime filters.

We can't re-run the strategy (no historical bars stored), but every BUY records
its entry RSI in `signal_reason` and every SELL records realized `pnl_at_close`.
So we FIFO-match sells back to the buy lots they closed, attach each lot's
realized P&L to its entry RSI/date, and then replay the resulting round-trips
under hypothetical filters.

Filters modeled:
  - RSI threshold: keep only round-trips whose ENTRY rsi < threshold.
  - SPY regime (stocks only; crypto always exempt): drop stock round-trips whose
    entry day is flagged "SPY down". Two definitions, because the difference
    matters a lot on sharp 2-day moves:
      same-day : SPY's close that day < prior day's close  (LOOK-AHEAD: you
                 wouldn't fully know today's direction at a midday entry)
      lagged   : SPY's close yesterday < its close the day before  (REALISTIC:
                 only uses info available before the day starts)
"""
import re
import sqlite3
from collections import defaultdict, deque

DB = "data/trades.db"
con = sqlite3.connect(DB)

crypto_bases = {s.replace("/", "") for (s,) in
                con.execute("SELECT DISTINCT symbol FROM trades WHERE symbol LIKE '%/%'")}


def norm(s):          # AAVE/USD and AAVEUSD -> AAVEUSD
    return s.replace("/", "")


def is_crypto(sym):
    return "/" in sym or norm(sym) in crypto_bases


def entry_rsi(reason):
    m = re.search(r"RSI ([0-9.]+)", reason or "")
    return float(m.group(1)) if m else None


# ---- FIFO-match buys to sells, per normalized symbol -----------------------
rows = con.execute(
    "SELECT symbol, side, qty, price, signal_reason, timestamp, pnl_at_close "
    "FROM trades ORDER BY timestamp ASC"
).fetchall()

open_lots = defaultdict(deque)   # symbol -> deque of {qty, rsi, date}
roundtrips = []                  # closed lots: {symbol, kind, rsi, date, qty, pnl}

for sym, side, qty, price, reason, ts, pnl in rows:
    key = norm(sym)
    date = ts[:10]
    if side == "buy":
        open_lots[key].append({"qty": qty, "rsi": entry_rsi(reason), "date": date})
    else:  # sell closes oldest open lots; split the sell's pnl across them by qty
        remaining = qty
        sold_total = qty if qty else 1.0
        q = open_lots[key]
        while remaining > 1e-9 and q:
            lot = q[0]
            take = min(remaining, lot["qty"])
            share = (take / sold_total) * (pnl or 0.0)
            roundtrips.append({
                "symbol": key, "kind": "crypto" if is_crypto(sym) else "stock",
                "rsi": lot["rsi"], "date": lot["date"], "qty": take, "pnl": share,
            })
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-9:
                q.popleft()

# ---- SPY daily close proxy + regime down-days ------------------------------
spy = con.execute(
    "SELECT substr(timestamp,1,10) d, price, timestamp FROM trades "
    "WHERE symbol='SPY' ORDER BY timestamp ASC"
).fetchall()
spy_close = {}                       # last print of each day = that day's close proxy
for d, p, ts in spy:
    spy_close[d] = p
spy_days = sorted(spy_close)

down_sameday, down_lagged = set(), set()
for i, d in enumerate(spy_days):
    if i >= 1 and spy_close[d] < spy_close[spy_days[i - 1]]:
        down_sameday.add(d)
    if i >= 2 and spy_close[spy_days[i - 1]] < spy_close[spy_days[i - 2]]:
        down_lagged.add(d)

# ---- replay helpers --------------------------------------------------------
def summarize(trips):
    scored = [t for t in trips if t["pnl"] is not None]
    wins = sum(1 for t in scored if t["pnl"] > 0)
    losses = sum(1 for t in scored if t["pnl"] < 0)
    net = sum(t["pnl"] for t in scored)
    wr = 100 * wins / (wins + losses) if (wins + losses) else 0.0
    return len(scored), net, wr, wins, losses


def apply_filters(rsi_thr, regime):
    out = []
    for t in roundtrips:
        if t["rsi"] is None or t["rsi"] >= rsi_thr:
            continue
        if regime and t["kind"] == "stock":
            blocked = down_sameday if regime == "sameday" else down_lagged
            if t["date"] in blocked:
                continue
        out.append(t)
    return out


# ---- report ----------------------------------------------------------------
print("Round-trips reconstructed: %d  (open/unmatched lots ignored)\n"
      % len(roundtrips))

print("SPY close proxy by day (last print):")
for d in spy_days:
    tags = []
    if d in down_sameday: tags.append("DOWN-sameday")
    if d in down_lagged:  tags.append("DOWN-lagged")
    print("   %s  %.2f   %s" % (d, spy_close[d], ", ".join(tags)))
print()

print("=== Win%% and net P&L by RSI threshold (NO regime filter) ===")
print("   %-8s %-6s %-12s %-8s %s" % ("RSI<", "trips", "net P&L", "win%", "W/L"))
for thr in (45, 40, 35):
    n, net, wr, w, l = summarize(apply_filters(thr, None))
    print("   %-8s %-6d %-12.2f %-8.1f %d/%d" % (thr, n, net, wr, w, l))
print()

print("=== Grid: RSI threshold x SPY-regime (stocks only; crypto exempt) ===")
print("   %-8s %-12s %-6s %-12s %s" % ("RSI<", "regime", "trips", "net P&L", "win%"))
for thr in (45, 40, 35):
    for regime, label in ((None, "none"),
                          ("sameday", "fast-sameday"),
                          ("lagged", "fast-lagged")):
        n, net, wr, w, l = summarize(apply_filters(thr, regime))
        print("   %-8s %-12s %-6d %-12.2f %.1f" % (thr, label, n, net, wr))
    print()

# sanity: totals vs raw DB
n, net, wr, w, l = summarize(apply_filters(999, None))
print("Sanity — all round-trips: net=%.2f  win%%=%.1f  (DB sell-side net was 4069.17)"
      % (net, wr))
