"""AlpacaClient.place_market_order: notional (fractional $) vs qty orders.

Uses a capturing fake trading client (bypassing __init__) so we can inspect the
MarketOrderRequest that would be submitted, without any network.
"""

import pytest

from bot.alpaca_client import AlpacaClient, AlpacaClientError


class _CapturingTrading:
    def __init__(self):
        self.last_request = None

    def submit_order(self, request):
        self.last_request = request
        return request  # _normalize_order reads attrs with safe getattr defaults


def _client() -> AlpacaClient:
    c = AlpacaClient.__new__(AlpacaClient)
    c._trading = _CapturingTrading()
    return c


def test_notional_order_sets_notional_not_qty():
    c = _client()
    c.place_market_order("AAPL", side="buy", notional=1000.0)
    req = c._trading.last_request
    assert float(req.notional) == 1000.0
    assert req.qty is None


def test_qty_order_sets_qty_not_notional():
    c = _client()
    c.place_market_order("AAPL", qty=5, side="buy")
    req = c._trading.last_request
    assert float(req.qty) == 5
    assert req.notional is None


def test_requires_exactly_one_of_qty_or_notional():
    c = _client()
    with pytest.raises(AlpacaClientError):
        c.place_market_order("AAPL", side="buy")  # neither
    with pytest.raises(AlpacaClientError):
        c.place_market_order("AAPL", qty=5, side="buy", notional=1000.0)  # both
