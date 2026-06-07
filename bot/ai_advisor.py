"""Claude API integration — per-trade commentary and free-form advisor Q&A.

Two entry points:

    get_commentary(symbol, analysis, trade) -> str
        Short (2-3 sentence) natural-language insight about a trade we just
        placed. Non-streaming — the caller (the trader loop) just wants the
        finished text to store on the trade row.

    ask_advisor(question, recent_trades, account, on_text=None) -> str
        Free-form question from the TUI. Streams the response token-by-token:
        pass an ``on_text`` callback to receive each chunk as it arrives (the
        TUI uses this to render live), and the full text is returned at the end.

Both degrade gracefully: if no ANTHROPIC_API_KEY is configured the advisor is
"disabled" and returns a clear placeholder rather than raising, so the rest of
the bot keeps working without AI.

Prompt caching: the system prompt is marked cacheable (``cache_control``).
Note that Sonnet 4's minimum cacheable prefix is ~1024 tokens — the per-trade
commentary prompt is smaller than that, so caching realistically only engages
for repeated ``ask_advisor`` calls within the 5-minute TTL (same stable system
prefix). It's wired correctly regardless; it just won't always hit.
"""

from __future__ import annotations

import json
from typing import Any, Callable

try:
    import anthropic
except ImportError:  # pragma: no cover - anthropic is a hard runtime dependency
    anthropic = None  # type: ignore[assignment]

from . import config

# A frozen system prompt — kept byte-stable so it can be cached. No timestamps,
# no per-request interpolation (those would invalidate the cache prefix).
_SYSTEM_PROMPT = (
    "You are a concise trading assistant embedded in a paper-trading bot. "
    "The bot trades a combined RSI + EMA-crossover momentum/mean-reversion "
    "strategy on a watchlist of US equities, using Alpaca's PAPER trading API "
    "(no real money). You explain the bot's decisions and answer the operator's "
    "questions in plain, grounded language.\n\n"
    "Guidelines:\n"
    "- Be specific and reference the actual indicator values you are given.\n"
    "- Never give financial advice or price predictions; this is educational "
    "paper trading. Do not tell the user to buy or sell real securities.\n"
    "- Keep trade commentary to 2-3 sentences. Keep Q&A answers tight.\n"
    "- If the data is insufficient to say something useful, say so briefly."
)

# Sensible token ceilings: commentary is short; Q&A gets more room.
_COMMENTARY_MAX_TOKENS = 200
_ADVISOR_MAX_TOKENS = 1024


class AIAdvisor:
    """Wraps the Anthropic client for trade commentary and Q&A."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.model = model or config.CLAUDE_MODEL
        self._api_key = api_key if api_key is not None else config.ANTHROPIC_API_KEY
        self._client: Any | None = None

        if self._api_key and anthropic is not None:
            try:
                self._client = anthropic.Anthropic(api_key=self._api_key)
            except Exception:  # noqa: BLE001 - never let init crash the bot
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # -- System prompt block (cacheable) ---------------------------------
    @staticmethod
    def _system_blocks() -> list[dict[str, Any]]:
        return [
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # -- Per-trade commentary --------------------------------------------
    def get_commentary(
        self, symbol: str, analysis: dict, trade: dict
    ) -> str:
        """Return a 2-3 sentence insight about a trade. Never raises."""
        if not self.enabled:
            return "(AI commentary disabled — set ANTHROPIC_API_KEY to enable.)"

        prompt = self._build_commentary_prompt(symbol, analysis, trade)
        try:
            response = self._client.messages.create(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=_COMMENTARY_MAX_TOKENS,
                system=self._system_blocks(),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._first_text(response).strip()
        except Exception as exc:  # noqa: BLE001 - surface as text, don't crash
            return f"(AI commentary unavailable: {exc})"

    # -- Free-form Q&A (streaming) ---------------------------------------
    def ask_advisor(
        self,
        question: str,
        recent_trades: list[dict] | None = None,
        account: dict | None = None,
        on_text: Callable[[str], None] | None = None,
    ) -> str:
        """Answer a free-form question, streaming chunks to ``on_text``.

        ``on_text`` is invoked with each text delta as it arrives (the TUI uses
        this to render token-by-token). The complete answer is returned. Never
        raises — errors are returned as the answer string (and also streamed).
        """
        if not self.enabled:
            msg = "(AI advisor disabled — set ANTHROPIC_API_KEY to enable.)"
            if on_text:
                on_text(msg)
            return msg

        prompt = self._build_advisor_prompt(
            question, recent_trades or [], account or {}
        )
        try:
            chunks: list[str] = []
            with self._client.messages.stream(  # type: ignore[union-attr]
                model=self.model,
                max_tokens=_ADVISOR_MAX_TOKENS,
                system=self._system_blocks(),
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    chunks.append(text)
                    if on_text:
                        on_text(text)
            return "".join(chunks).strip()
        except Exception as exc:  # noqa: BLE001
            err = f"(AI advisor error: {exc})"
            if on_text:
                on_text(err)
            return err

    # -- Prompt construction ---------------------------------------------
    @staticmethod
    def _build_commentary_prompt(
        symbol: str, analysis: dict, trade: dict
    ) -> str:
        return (
            f"The bot just executed a trade. Give a 2-3 sentence insight.\n\n"
            f"Symbol: {symbol}\n"
            f"Order: {trade.get('side', '?').upper()} "
            f"{trade.get('qty', '?')} @ ${trade.get('price', '?')} "
            f"(total ${trade.get('total_value', '?')})\n"
            f"Signal: {analysis.get('signal', '?')}\n"
            f"RSI(14): {analysis.get('rsi', '?')}\n"
            f"EMA fast: {analysis.get('ema_fast', '?')}  "
            f"EMA slow: {analysis.get('ema_slow', '?')}\n"
            f"Reason: {analysis.get('reason', '')}\n"
            f"Realized P&L (if a close): {trade.get('pnl_at_close', 'n/a')}"
        )

    @staticmethod
    def _build_advisor_prompt(
        question: str, recent_trades: list[dict], account: dict
    ) -> str:
        # Compact, deterministic serialization of context. Only the last 10
        # trades, trimmed to the fields that matter, to keep the prompt small.
        trimmed = [
            {
                "symbol": t.get("symbol"),
                "side": t.get("side"),
                "qty": t.get("qty"),
                "price": t.get("price"),
                "pnl_at_close": t.get("pnl_at_close"),
                "timestamp": t.get("timestamp"),
            }
            for t in recent_trades[:10]
        ]
        account_summary = {
            "equity": account.get("equity"),
            "cash": account.get("cash"),
            "buying_power": account.get("buying_power"),
        }
        return (
            "Current account state:\n"
            f"{json.dumps(account_summary, default=str)}\n\n"
            "Most recent trades (newest first):\n"
            f"{json.dumps(trimmed, default=str)}\n\n"
            f"Operator's question: {question}"
        )

    # -- Response helpers ------------------------------------------------
    @staticmethod
    def _first_text(response: Any) -> str:
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""
