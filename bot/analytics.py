"""Pure analytics helpers over recorded trades.

No I/O or external dependencies, so this is trivially unit-testable. Used by the
dashboard to present trade history in more useful ways than the raw rows.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

_EPS = 1e-9


def pair_round_trips(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair buys with later sells (FIFO per symbol) into closed round trips.

    ``trades`` is a list of trade dicts with at least ``symbol``, ``side``
    ("buy"/"sell"), ``qty``, ``price``, and ``timestamp``. Input order doesn't
    matter — trades are sorted by timestamp here. Each closed round trip (a sell
    matched against earlier buy quantity for the same symbol) yields one dict:

        symbol, qty, entry_price, exit_price, entry_time, exit_time, pnl, pnl_pct

    Results are newest-exit-first. Still-open buys (no matching sell yet) and
    unmatched sells are omitted rather than raising.
    """
    ordered = sorted(trades, key=lambda t: t.get("timestamp") or "")
    open_buys: dict[str, deque[list[Any]]] = defaultdict(deque)
    trips: list[dict[str, Any]] = []

    for trade in ordered:
        symbol = str(trade.get("symbol", ""))
        side = str(trade.get("side", "")).lower()
        qty = float(trade.get("qty", 0) or 0)
        price = float(trade.get("price", 0) or 0)
        ts = trade.get("timestamp")
        if qty <= 0:
            continue

        if side == "buy":
            open_buys[symbol].append([qty, price, ts])
        elif side == "sell":
            remaining = qty
            lots = open_buys[symbol]
            while remaining > _EPS and lots:
                lot = lots[0]
                matched = min(remaining, lot[0])
                entry_price = lot[1]
                pnl = (price - entry_price) * matched
                pnl_pct = (price / entry_price - 1) * 100 if entry_price else 0.0
                trips.append(
                    {
                        "symbol": symbol,
                        "qty": round(matched, 6),
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(price, 2),
                        "entry_time": lot[2],
                        "exit_time": ts,
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                    }
                )
                lot[0] -= matched
                remaining -= matched
                if lot[0] <= _EPS:
                    lots.popleft()
            # A sell with no matching open buy is ignored (can't pair it).

    trips.reverse()  # newest exit first
    return trips
