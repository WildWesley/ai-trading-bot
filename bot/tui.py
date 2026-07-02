"""Rich-based live terminal dashboard.

Layout (top to bottom):
    1. Header        — bot name + status + last event
    2. Two columns   — Account (equity/cash/net P&L) | Open positions
    3. Recent trades — last 10 trades
    4. AI commentary — latest trade commentary, or a live-streamed advisor answer
    5. Input bar     — prompt hint for asking Claude a question

Interaction model
-----------------
The dashboard is driven by ``rich.live.Live``. A background daemon thread
re-renders it every ``TUI_REFRESH_SECONDS`` so it tracks whatever the trading
loop writes to the database. The main thread waits at an input prompt; reading
a line requires the terminal, so we briefly ``live.stop()`` around ``input()``
and ``live.start()`` afterwards. The trader thread keeps running throughout, so
pausing the *display* loses no data.

When the operator submits a question, ``ask_advisor`` streams the answer; the
``on_text`` callback appends each chunk to a buffer and refreshes the Live, so
the answer materializes token-by-token in the AI commentary panel.

All data reads are defensive: a failing Alpaca call shows the last known values
plus an error note rather than crashing the UI.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import config


def _fmt_money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pnl_text(value: Any, prefix: str = "") -> Text:
    """Return a Text colored green for >=0, red for <0."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return Text(f"{prefix}—")
    color = "green" if v >= 0 else "red"
    sign = "+" if v >= 0 else ""
    return Text(f"{prefix}{sign}${v:,.2f}", style=color)


def _pct_text(value: Any) -> Text:
    """Colored percentage from a *fraction* (Alpaca's unrealized_plpc: 0.05 => +5.00%)."""
    try:
        v = float(value) * 100.0
    except (TypeError, ValueError):
        return Text("—")
    color = "green" if v >= 0 else "red"
    sign = "+" if v >= 0 else ""
    return Text(f"{sign}{v:.2f}%", style=color)


def _short_time(ts: Any) -> str:
    """Render an ISO timestamp as HH:MM:SS (local-ish); fall back to str."""
    if not ts:
        return "—"
    s = str(ts)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return s[-8:] if len(s) >= 8 else s


class TradingTUI:
    """Renders the live dashboard and drives the advisor input loop."""

    def __init__(
        self,
        db: Any,
        client: Any | None = None,
        trader: Any | None = None,
        advisor: Any | None = None,
    ) -> None:
        self.db = db
        self.client = client
        self.trader = trader
        self.advisor = advisor

        self.status = "starting"
        self._running = False
        self._paused = threading.Event()  # set => refresher leaves screen alone
        self._refresh_lock = threading.Lock()
        self._live: Live | None = None

        # Advisor Q&A state shown in the commentary panel.
        self._advisor_question: str | None = None
        self._advisor_answer: str = ""

        # Last-known account/positions, so transient API failures don't blank
        # the panels.
        self._last_account: dict[str, Any] = {}
        self._last_positions: list[dict[str, Any]] = []
        self._data_error: str | None = None

    # -- Public hooks ----------------------------------------------------
    def set_status(self, status: str) -> None:
        self.status = status

    # -- Data acquisition (defensive) ------------------------------------
    def _account(self) -> dict[str, Any]:
        if self.client is None:
            return self._last_account
        try:
            self._last_account = self.client.get_account()
            self._data_error = None
        except Exception as exc:  # noqa: BLE001
            self._data_error = f"account fetch failed: {exc}"
        return self._last_account

    def _positions(self) -> list[dict[str, Any]]:
        if self.client is None:
            return self._last_positions
        try:
            self._last_positions = self.client.get_positions()
        except Exception as exc:  # noqa: BLE001
            self._data_error = f"positions fetch failed: {exc}"
        return self._last_positions

    # -- Renderers (pure-ish; safe to unit test) -------------------------
    def _render_header(self) -> Panel:
        last_event = ""
        if self.trader is not None:
            events = self.trader.recent_events(1)
            if events:
                e = events[0]
                last_event = f"  -  [{e['level']}] {e['message']}"
        # Single line so the header fits a size-3 panel on short terminals.
        line = Text()
        line.append(f" {config.BOT_NAME} ", style="bold white on blue")
        strat = "Momentum" if config.STRATEGY == "momentum" else "RSI"
        line.append(f"  [{strat}]", style="bold cyan")
        line.append(f"  status: {self.status}{last_event}", style="dim")
        if self._data_error:
            line.append(f"  ! {self._data_error}", style="red")
        return Panel(Align.center(line), style="blue", padding=(0, 1))

    def _render_account(self, acct: dict[str, Any]) -> Panel:
        net_pnl = 0.0
        stats: dict[str, Any] = {}
        try:
            net_pnl = self.db.get_net_pnl()
            stats = self.db.get_trade_stats()
        except Exception:  # noqa: BLE001
            pass

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="left", style="cyan")
        table.add_column(justify="right")
        table.add_row("Equity", _fmt_money(acct.get("equity")))
        table.add_row("Cash", _fmt_money(acct.get("cash")))
        table.add_row("Buying power", _fmt_money(acct.get("buying_power")))
        table.add_row("Net realized P&L", _pnl_text(net_pnl))
        if stats:
            wr = stats.get("win_rate", 0.0) * 100
            table.add_row(
                "Trades / win rate",
                f"{stats.get('total_trades', 0)} / {wr:.0f}%",
            )
        return Panel(table, title="Account", border_style="cyan")

    def _render_positions(self, positions: list[dict[str, Any]]) -> Panel:
        table = Table(expand=True, box=None)
        table.add_column("Symbol", style="bold")
        table.add_column("Qty", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Value", justify="right")
        table.add_column("P&L $", justify="right")
        table.add_column("P&L %", justify="right")
        # Momentum is a stocks-only rotation; sort by size so the biggest
        # holdings lead. (Sorting a copy — never mutate the cached list.)
        rows = sorted(
            positions,
            key=lambda p: float(p.get("market_value") or 0),
            reverse=True,
        )
        if not rows:
            table.add_row("—", "—", "—", "—", "—", "—")
        else:
            for p in rows:
                table.add_row(
                    str(p.get("symbol", "")),
                    f"{float(p.get('qty', 0)):g}",
                    _fmt_money(p.get("current_price")),
                    _fmt_money(p.get("market_value")),
                    _pnl_text(p.get("unrealized_pl")),
                    _pct_text(p.get("unrealized_plpc")),
                )
        is_momentum = config.STRATEGY == "momentum"
        title = f"Fund Holdings ({len(rows)})" if is_momentum else "Open Positions"
        return Panel(table, title=title, border_style="magenta")

    def _render_trades(self) -> Panel:
        try:
            trades = self.db.get_trades(10)
        except Exception as exc:  # noqa: BLE001
            return Panel(Text(f"trade history unavailable: {exc}"),
                         title="Recent Trades", border_style="yellow")
        table = Table(expand=True, box=None)
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("Side", no_wrap=True)
        table.add_column("Sym", style="bold", no_wrap=True)
        table.add_column("Qty", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Signal / reason", overflow="ellipsis", max_width=48)
        if not trades:
            table.add_row("—", "—", "—", "—", "—", "no trades yet")
        else:
            for t in trades:
                side = str(t.get("side", "")).upper()
                side_style = "green" if side == "BUY" else "red"
                table.add_row(
                    _short_time(t.get("timestamp")),
                    Text(side, style=side_style),
                    str(t.get("symbol", "")),
                    f"{float(t.get('qty', 0)):g}",
                    _fmt_money(t.get("price")),
                    str(t.get("signal_reason") or ""),
                )
        return Panel(table, title="Recent Trades", border_style="yellow")

    def _render_commentary(self) -> Panel:
        # If a Q&A is active/recent, show it; otherwise the latest trade
        # commentary from the DB.
        if self._advisor_question is not None:
            body = Group(
                Text(f"Q: {self._advisor_question}", style="bold cyan"),
                Text(self._advisor_answer or "…", style="white"),
            )
            return Panel(body, title="AI Advisor", border_style="green")

        commentary = None
        try:
            commentary = self.db.get_latest_commentary()
        except Exception:  # noqa: BLE001
            pass
        text = Text(commentary or "No AI commentary yet.", style="white")
        return Panel(text, title="AI Commentary", border_style="green")

    def _render_input(self) -> Panel:
        hint = Text()
        hint.append("Ask Claude: ", style="bold")
        hint.append("type a question + Enter", style="dim")
        hint.append("   |   ", style="dim")
        hint.append("/quit", style="bold red")
        hint.append(" to exit", style="dim")
        return Panel(hint, border_style="white")

    # -- Layout assembly -------------------------------------------------
    def _build_layout(self) -> Layout:
        layout = Layout(name="root")
        # Keep the fixed-size sections modest so the input bar stays visible
        # even on short (~24-line) terminals; the two-column body absorbs the
        # remaining height.
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="trades", size=10),
            Layout(name="commentary", size=6),
            Layout(name="input", size=3),
        )
        layout["body"].split_row(
            Layout(name="account"),
            Layout(name="positions"),
        )
        # Fetch account + positions once per frame (this also sets
        # ``_data_error`` before the header renders, so failures surface there).
        self._data_error = None
        acct = self._account()
        positions = self._positions()
        layout["header"].update(self._render_header())
        layout["account"].update(self._render_account(acct))
        layout["positions"].update(self._render_positions(positions))
        layout["trades"].update(self._render_trades())
        layout["commentary"].update(self._render_commentary())
        layout["input"].update(self._render_input())
        return layout

    # -- Non-interactive render (smoke test / no TTY) --------------------
    def render_once(self, console: Console | None = None) -> None:
        """Render a single frame and return. Useful for smoke tests."""
        (console or Console()).print(self._build_layout())

    # -- Live refresh ----------------------------------------------------
    def _refresh(self) -> None:
        if self._live is None or self._paused.is_set():
            return
        with self._refresh_lock:
            try:
                self._live.update(self._build_layout(), refresh=True)
            except Exception:  # noqa: BLE001 - never let a render error crash
                pass

    def _refresh_loop(self) -> None:
        interval = max(1, config.TUI_REFRESH_SECONDS)
        while self._running:
            self._refresh()
            # Sleep in small slices so shutdown is responsive.
            for _ in range(interval * 4):
                if not self._running:
                    return
                threading.Event().wait(0.25)

    # -- Advisor Q&A -----------------------------------------------------
    def _on_token(self, chunk: str) -> None:
        self._advisor_answer += chunk
        self._refresh()

    def handle_question(self, question: str) -> str:
        """Stream an advisor answer into the commentary panel. Returns it."""
        self._advisor_question = question
        self._advisor_answer = ""
        if self.advisor is None:
            self._advisor_answer = "(AI advisor not configured.)"
            self._refresh()
            return self._advisor_answer

        recent_trades = []
        account = {}
        try:
            recent_trades = self.db.get_trades(10)
        except Exception:  # noqa: BLE001
            pass
        try:
            account = self._account()
        except Exception:  # noqa: BLE001
            pass

        answer = self.advisor.ask_advisor(
            question,
            recent_trades=recent_trades,
            account=account,
            on_text=self._on_token,
        )
        # Ensure final state is consistent even if streaming was a no-op.
        self._advisor_answer = answer
        self._refresh()
        return answer

    # -- Main loop -------------------------------------------------------
    def run(self) -> None:
        """Start the live dashboard and the interactive input loop."""
        self._running = True
        self.status = "running"
        console = Console()
        self._live = Live(
            self._build_layout(),
            console=console,
            screen=False,
            auto_refresh=False,
        )
        self._live.start()
        refresher = threading.Thread(target=self._refresh_loop, daemon=True)
        refresher.start()

        try:
            while self._running:
                question = self._prompt(console)
                if question is None:  # EOF / Ctrl-D
                    break
                q = question.strip()
                if not q:
                    continue
                if q.lower() in ("/quit", "/q", "quit", "exit"):
                    break
                self.handle_question(q)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.stop()

    def _prompt(self, console: Console) -> str | None:
        """Pause the Live, read one line, resume. Returns None on EOF."""
        self._paused.set()
        if self._live is not None:
            self._live.stop()
        try:
            return input("\nAsk Claude (/quit to exit) > ")
        except (EOFError, KeyboardInterrupt):
            return None
        finally:
            if self._live is not None and self._running:
                self._live.start()
            self._paused.clear()

    def stop(self) -> None:
        self._running = False
        self.status = "stopped"
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:  # noqa: BLE001
                pass
            self._live = None
