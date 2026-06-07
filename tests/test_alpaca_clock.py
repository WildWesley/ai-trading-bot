"""Tests for AlpacaClient.get_clock — verified with a stubbed trading client."""

from datetime import datetime, timezone

from bot.alpaca_client import AlpacaClient


class _FakeClock:
    def __init__(self):
        self.is_open = True
        self.next_open = datetime(2026, 6, 8, 13, 30, tzinfo=timezone.utc)
        self.next_close = datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc)
        self.timestamp = datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)


class _FakeTrading:
    def get_clock(self):
        return _FakeClock()


def _make_client_without_network() -> AlpacaClient:
    # Bypass __init__ (which builds real alpaca clients) and inject a fake.
    client = AlpacaClient.__new__(AlpacaClient)
    client._trading = _FakeTrading()
    return client


def test_get_clock_normalizes_fields():
    client = _make_client_without_network()
    clock = client.get_clock()
    assert clock["is_open"] is True
    assert clock["next_open"] == datetime(2026, 6, 8, 13, 30, tzinfo=timezone.utc)
    assert clock["next_close"] == datetime(2026, 6, 8, 20, 0, tzinfo=timezone.utc)
    assert clock["timestamp"] == datetime(2026, 6, 8, 15, 0, tzinfo=timezone.utc)
