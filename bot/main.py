"""Entry point — wires up config, DB, the trader loop, and the TUI.

Run from the ``trading-bot/`` directory with:

    python -m bot.main

Startup sequence:
    1. Load .env and validate config (config.py does the loading at import).
    2. Initialize the SQLite database (create tables if needed).
    3. Construct the Alpaca client and AI advisor (each degrades gracefully if
       its credentials are missing).
    4. Run an immediate trading cycle, then schedule one every
       TRADE_INTERVAL_SECONDS on a background daemon thread.
    5. Launch the TUI on the main thread.
    6. On KeyboardInterrupt / quit: stop the loop, cancel open orders, print a
       summary, and exit cleanly.
"""

from __future__ import annotations

import argparse
import sys
import threading

import schedule

from . import config
from .ai_advisor import AIAdvisor
from .database import Database
from .tui import TradingTUI


class BotApp:
    """Owns the long-lived objects and coordinates startup/shutdown."""

    def __init__(self, headless: bool = False, use_tui: bool = False) -> None:
        self.headless = headless  # True => don't launch the browser dashboard
        self.use_tui = use_tui  # True => interactive TUI instead of scrolling log
        self.db: Database | None = None
        self.client = None  # AlpacaClient | None
        self.advisor: AIAdvisor | None = None
        self.trader = None  # Trader | None
        self.tui: TradingTUI | None = None

        self._scheduler = schedule.Scheduler()
        self._stop_event = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._dashboard_proc = None  # subprocess.Popen | None
        self._dashboard_log = None  # open file handle for the dashboard's output

    # -- Setup -----------------------------------------------------------
    def setup(self) -> None:
        config.ensure_data_dir()
        self.db = Database(config.DB_PATH)

        # AI advisor — works (disabled) even without a key.
        self.advisor = AIAdvisor()

        # Alpaca client — requires keys. If absent, run in "display only" mode:
        # the TUI still renders DB history, but no live data or trading occurs.
        self.client = self._make_client()

        if self.client is not None:
            # Import here so the module imports cleanly even if alpaca-py has
            # an issue; and so a bad client doesn't block DB-only usage.
            from .trader import Trader

            self.trader = Trader(self.client, self.db, self.advisor)

        # Terminal output: either the interactive full-screen TUI (--tui), or a
        # plain scrolling log (default). The scrolling log streams the trader's
        # events as they happen and pairs well with the browser dashboard.
        if self.use_tui:
            self.tui = TradingTUI(
                self.db,
                client=self.client,
                trader=self.trader,
                advisor=self.advisor,
            )
        elif self.trader is not None:
            self.trader.on_event = self._print_event

    def _make_client(self):
        problems = config.validate()
        alpaca_problems = [
            p for p in problems if "ALPACA" in p
        ]
        if alpaca_problems:
            for p in alpaca_problems:
                print(f"  ! {p}", file=sys.stderr)
            print(
                "  -> Alpaca keys missing; starting in display-only mode "
                "(no live data or trading).",
                file=sys.stderr,
            )
            return None
        try:
            from .alpaca_client import AlpacaClient

            client = AlpacaClient()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Could not initialize Alpaca client: {exc}", file=sys.stderr)
            print("  -> Starting in display-only mode.", file=sys.stderr)
            return None

        # Construction doesn't talk to Alpaca — make one real call so bad or
        # non-paper keys surface here with a clear message instead of silently
        # failing inside the trading loop later.
        try:
            acct = client.verify_connection()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {exc}", file=sys.stderr)
            print("  -> Starting in display-only mode.", file=sys.stderr)
            return None

        print(
            f"  + Connected to Alpaca paper account "
            f"(status={acct['status']}, equity=${acct['equity']:,.2f})."
        )
        return client

    # -- Trading loop ----------------------------------------------------
    def _run_cycle_safe(self) -> None:
        if self.trader is None:
            return
        try:
            self.trader.run_cycle()
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            self.trader.log("error", f"cycle crashed: {exc}")

    def _loop(self) -> None:
        """Background thread: run pending scheduled jobs until stopped."""
        # Run one cycle immediately so the dashboard has data on first paint.
        self._run_cycle_safe()
        self._scheduler.every(config.TRADE_INTERVAL_SECONDS).seconds.do(
            self._run_cycle_safe
        )
        while not self._stop_event.is_set():
            self._scheduler.run_pending()
            # Short sleep so shutdown is responsive.
            self._stop_event.wait(1.0)

    def start_loop(self) -> None:
        if self.trader is None:
            return
        self._loop_thread = threading.Thread(
            target=self._loop, name="trader-loop", daemon=True
        )
        self._loop_thread.start()

    # -- Run / shutdown --------------------------------------------------
    def run(self) -> None:
        print(f"Starting {config.BOT_NAME} ...")
        self.setup()
        self.start_loop()
        if not self.headless:
            self._launch_dashboard()
        try:
            if self.use_tui:
                assert self.tui is not None
                self.tui.run()  # blocks until /quit or Ctrl-C
            else:
                self._serve_console()  # blocks, streaming the scrolling log
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def _serve_console(self) -> None:
        """Block the main thread while the trading loop streams a scrolling log.

        Events are printed by the ``on_event`` sink (set in ``setup``) as the
        trader logs them, so here we just wait until interrupted.
        """
        if self.trader is None:
            print(
                "  -> No Alpaca client; nothing to trade. Exiting.",
                file=sys.stderr,
            )
            return
        browser = (
            "Browser dashboard at http://localhost:8501. "
            if not self.headless
            else ""
        )
        print(
            f"\nRunning - a cycle every {config.TRADE_INTERVAL_SECONDS}s. "
            f"{browser}Press Ctrl-C to stop.\n"
        )
        while not self._stop_event.is_set():
            self._stop_event.wait(1.0)

    @staticmethod
    def _print_event(event: dict[str, str]) -> None:
        """Scrolling-log sink. Skips the noisy per-symbol heartbeat (debug)."""
        level = event.get("level", "")
        if level == "debug":
            return
        ts = str(event.get("timestamp", ""))[11:19] or "--:--:--"
        print(f"[{ts}] {level:>5} | {event.get('message', '')}", flush=True)

    def _launch_dashboard(self) -> None:
        """Spawn the Streamlit browser dashboard as a background subprocess.

        Notes on the flags:
          * ``--server.headless=true`` stops Streamlit's first-run email prompt
            (which would otherwise hang, waiting for input the subprocess can't
            receive). We open the browser ourselves below instead.
          * ``stdin`` is closed and output is sent to ``data/dashboard.log`` so
            failures are inspectable rather than silently swallowed, and so the
            output never corrupts the terminal dashboard.

        Any failure here is non-fatal — the terminal dashboard keeps working.
        """
        import importlib.util
        import subprocess
        import webbrowser

        if importlib.util.find_spec("streamlit") is None:
            print(
                "  ! Streamlit not installed; skipping browser dashboard "
                "(run: python -m pip install -r requirements.txt).",
                file=sys.stderr,
            )
            return
        dashboard = config.PROJECT_ROOT / "dashboard.py"
        if not dashboard.exists():
            print(
                f"  ! {dashboard} not found; skipping browser dashboard.",
                file=sys.stderr,
            )
            return

        log_path = config.DATA_DIR / "dashboard.log"
        url = "http://localhost:8501"
        try:
            self._dashboard_log = open(log_path, "w", encoding="utf-8")
            self._dashboard_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "streamlit",
                    "run",
                    str(dashboard),
                    "--server.headless=true",
                    "--server.port=8501",
                    "--browser.gatherUsageStats=false",
                ],
                stdout=self._dashboard_log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(config.PROJECT_ROOT),
            )
        except Exception as exc:  # noqa: BLE001 - dashboard is best-effort
            print(f"  ! Could not launch browser dashboard: {exc}", file=sys.stderr)
            return

        # Give the server a moment to bind the port, then open the browser.
        threading.Timer(4.0, lambda: webbrowser.open(url)).start()
        print(
            f"  -> Browser dashboard at {url} (opening shortly; "
            f"output logged to {log_path}). Use --headless to skip."
        )

    def shutdown(self) -> None:
        # Idempotent: safe to call more than once.
        self._stop_event.set()
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5.0)
        self._scheduler.clear()

        if self.tui is not None:
            self.tui.stop()

        # Tear down the browser dashboard subprocess if we launched one.
        if self._dashboard_proc is not None:
            try:
                self._dashboard_proc.terminate()
                self._dashboard_proc.wait(timeout=5.0)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            self._dashboard_proc = None
        if self._dashboard_log is not None:
            try:
                self._dashboard_log.close()
            except Exception:  # noqa: BLE001
                pass
            self._dashboard_log = None

        self._cancel_open_orders()
        self._print_summary()

        if self.db is not None:
            self.db.close()
        print("Shutdown complete. Goodbye.")

    def _cancel_open_orders(self) -> None:
        if self.client is None:
            return
        try:
            open_orders = self.client.get_orders(status="open")
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Could not fetch open orders: {exc}", file=sys.stderr)
            return
        if not open_orders:
            return
        # Best-effort cancel of any resting orders.
        cancelled = 0
        for order in open_orders:
            oid = order.get("id")
            if not oid:
                continue
            try:
                # cancel_order_by_id lives on the underlying trading client.
                self.client._trading.cancel_order_by_id(oid)  # noqa: SLF001
                cancelled += 1
            except Exception:  # noqa: BLE001
                pass
        print(f"  - Cancelled {cancelled} open order(s).")

    def _print_summary(self) -> None:
        if self.db is None:
            return
        try:
            stats = self.db.get_trade_stats()
            net = self.db.get_net_pnl()
        except Exception:  # noqa: BLE001
            return
        print("\n-------- Session summary --------")
        print(f"  Total trades : {stats.get('total_trades', 0)}")
        print(f"  Closed trades: {stats.get('closed_trades', 0)}")
        print(f"  Win rate     : {stats.get('win_rate', 0.0) * 100:.0f}%")
        print(f"  Net realized P&L: ${net:,.2f}")
        print("---------------------------------")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bot.main",
        description="AI paper trading bot (Alpaca paper trading).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Do not launch the browser (Streamlit) dashboard. The terminal "
        "still streams a scrolling log.",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Use the interactive full-screen terminal dashboard (with the "
        "'Ask Claude' prompt) instead of the default scrolling log.",
    )
    args = parser.parse_args(argv)

    # Most common setup mistake: editing .env.example without creating .env.
    if not config.ENV_PATH.exists():
        print(f"  ! No .env file found at {config.ENV_PATH}")
        if (config.PROJECT_ROOT / ".env.example").exists():
            print(
                "    An .env.example exists but the bot only reads .env. "
                "Copy it and add your keys:"
            )
            print("       Copy-Item .env.example .env")
        print()

    problems = config.validate()
    if problems:
        print("Config notes:")
        for p in problems:
            print(f"  - {p}")
    app = BotApp(headless=args.headless, use_tui=args.tui)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
