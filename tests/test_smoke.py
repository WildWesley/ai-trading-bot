"""Smoke test: the package imports and fakes wire together."""

from bot import algorithm, config
from tests.conftest import FakeClient, FakeDB


def test_imports_and_fakes():
    assert hasattr(algorithm, "analyze")
    assert config.EMA_FAST_PERIOD == 9
    client = FakeClient()
    db = FakeDB()
    assert client.get_clock()["is_open"] is True
    assert db.record_trade({"symbol": "AAPL"}) == 1
