"""SQLite schema and insert/query helpers for trades and account snapshots.

A single ``Database`` object owns one connection. SQLite serializes writes, and
this bot's writer (the trader loop) and reader (the TUI) run on separate
threads, so the connection is opened with ``check_same_thread=False`` and every
mutating call is guarded by a lock. WAL mode is enabled so reads never block on
the writer.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    side           TEXT    NOT NULL,
    qty            REAL    NOT NULL,
    price          REAL    NOT NULL,
    total_value    REAL    NOT NULL,
    signal_reason  TEXT,
    ai_commentary  TEXT,
    timestamp      TEXT    NOT NULL,
    pnl_at_close   REAL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    equity                REAL    NOT NULL,
    cash                  REAL    NOT NULL,
    buying_power          REAL    NOT NULL,
    open_positions_count  INTEGER NOT NULL,
    timestamp             TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts     ON snapshots(timestamp);
"""

_TRADE_COLUMNS = (
    "symbol",
    "side",
    "qty",
    "price",
    "total_value",
    "signal_reason",
    "ai_commentary",
    "timestamp",
    "pnl_at_close",
)

_SNAPSHOT_COLUMNS = (
    "equity",
    "cash",
    "buying_power",
    "open_positions_count",
    "timestamp",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Owns a single SQLite connection and exposes typed helpers."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else config.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        # WAL: concurrent reads don't block on the single writer.
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self.init_db()

    # -- Schema ----------------------------------------------------------
    def init_db(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- Writes ----------------------------------------------------------
    def record_trade(self, trade: dict[str, Any]) -> int:
        """Insert a trade row. Returns the new row id.

        Required keys: symbol, side, qty, price. ``total_value`` is computed
        from qty*price when absent. ``timestamp`` defaults to now (UTC).
        Optional: signal_reason, ai_commentary, pnl_at_close.
        """
        row = self._prepare_trade(trade)
        placeholders = ", ".join("?" for _ in _TRADE_COLUMNS)
        columns = ", ".join(_TRADE_COLUMNS)
        sql = f"INSERT INTO trades ({columns}) VALUES ({placeholders})"
        values = [row[col] for col in _TRADE_COLUMNS]
        with self._lock:
            cur = self._conn.execute(sql, values)
            self._conn.commit()
            return int(cur.lastrowid)

    def record_snapshot(self, snapshot: dict[str, Any]) -> int:
        """Insert an account snapshot row. Returns the new row id."""
        row = {
            "equity": float(snapshot.get("equity", 0.0)),
            "cash": float(snapshot.get("cash", 0.0)),
            "buying_power": float(snapshot.get("buying_power", 0.0)),
            "open_positions_count": int(
                snapshot.get("open_positions_count", 0)
            ),
            "timestamp": snapshot.get("timestamp") or _utc_now_iso(),
        }
        placeholders = ", ".join("?" for _ in _SNAPSHOT_COLUMNS)
        columns = ", ".join(_SNAPSHOT_COLUMNS)
        sql = f"INSERT INTO snapshots ({columns}) VALUES ({placeholders})"
        values = [row[col] for col in _SNAPSHOT_COLUMNS]
        with self._lock:
            cur = self._conn.execute(sql, values)
            self._conn.commit()
            return int(cur.lastrowid)

    def update_trade_pnl(self, trade_id: int, pnl: float) -> None:
        """Set ``pnl_at_close`` on an existing trade (used when closing)."""
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET pnl_at_close = ? WHERE id = ?",
                (float(pnl), int(trade_id)),
            )
            self._conn.commit()

    def set_trade_commentary(self, trade_id: int, commentary: str) -> None:
        """Attach AI commentary to an existing trade after the fact."""
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET ai_commentary = ? WHERE id = ?",
                (commentary, int(trade_id)),
            )
            self._conn.commit()

    # -- Reads -----------------------------------------------------------
    def get_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent trades (newest first) as dicts."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_snapshots(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return up to ``limit`` most-recent account snapshots, oldest first.

        Oldest-first ordering is convenient for plotting an equity curve.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM ("
                "  SELECT * FROM snapshots ORDER BY id DESC LIMIT ?"
                ") ORDER BY id ASC",
                (int(limit),),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def get_latest_snapshot(self) -> dict[str, Any] | None:
        """Return the most recent account snapshot, or None if none exist."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_latest_commentary(self) -> str | None:
        """Return AI commentary from the most recent trade that has any."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT ai_commentary FROM trades "
                "WHERE ai_commentary IS NOT NULL AND ai_commentary != '' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        return row["ai_commentary"] if row else None

    def get_net_pnl(self) -> float:
        """Sum of ``pnl_at_close`` across all closed trades."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(SUM(pnl_at_close), 0.0) AS net "
                "FROM trades WHERE pnl_at_close IS NOT NULL"
            )
            row = cur.fetchone()
        return float(row["net"]) if row else 0.0

    def get_trade_stats(self) -> dict[str, Any]:
        """Return win_rate, total_trades, and avg_pnl.

        ``total_trades`` counts every recorded trade. ``win_rate`` and
        ``avg_pnl`` are computed over *closed* trades (those with a non-null
        ``pnl_at_close``); win_rate is the fraction with pnl > 0, in [0, 1].
        """
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS c FROM trades"
            ).fetchone()["c"]
            closed = self._conn.execute(
                "SELECT COUNT(*) AS c, "
                "COALESCE(AVG(pnl_at_close), 0.0) AS avg_pnl, "
                "COALESCE(SUM(pnl_at_close), 0.0) AS net "
                "FROM trades WHERE pnl_at_close IS NOT NULL"
            ).fetchone()
            wins = self._conn.execute(
                "SELECT COUNT(*) AS c FROM trades "
                "WHERE pnl_at_close IS NOT NULL AND pnl_at_close > 0"
            ).fetchone()["c"]

        closed_count = int(closed["c"])
        win_rate = (wins / closed_count) if closed_count else 0.0
        return {
            "total_trades": int(total),
            "closed_trades": closed_count,
            "wins": int(wins),
            "win_rate": round(win_rate, 4),
            "avg_pnl": round(float(closed["avg_pnl"]), 2),
            "net_pnl": round(float(closed["net"]), 2),
        }

    # -- Lifecycle -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- Internal --------------------------------------------------------
    @staticmethod
    def _prepare_trade(trade: dict[str, Any]) -> dict[str, Any]:
        qty = float(trade.get("qty", 0.0))
        price = float(trade.get("price", 0.0))
        total_value = trade.get("total_value")
        if total_value is None:
            total_value = round(qty * price, 2)
        pnl = trade.get("pnl_at_close")
        return {
            "symbol": str(trade.get("symbol", "")).upper(),
            "side": str(trade.get("side", "")).lower(),
            "qty": qty,
            "price": price,
            "total_value": float(total_value),
            "signal_reason": trade.get("signal_reason"),
            "ai_commentary": trade.get("ai_commentary"),
            "timestamp": trade.get("timestamp") or _utc_now_iso(),
            "pnl_at_close": None if pnl is None else float(pnl),
        }
