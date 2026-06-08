"""AlpacaClient.get_latest_prices — batched multi-symbol price fetch.

Verified with stubbed data clients (bypassing __init__) so there's no network.
This is the dashboard's fast path: one request per asset class instead of one
call per symbol.
"""

from bot.alpaca_client import AlpacaClient


class _FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class _FakeStockData:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = []

    def get_stock_latest_quote(self, req):
        syms = list(req.symbol_or_symbols)
        self.calls.append(syms)
        return {s: self.quotes[s] for s in syms if s in self.quotes}


class _FakeCryptoData:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls = []

    def get_crypto_latest_quote(self, req):
        syms = list(req.symbol_or_symbols)
        self.calls.append(syms)
        return {s: self.quotes[s] for s in syms if s in self.quotes}


def _make_client(stock_quotes, crypto_quotes) -> AlpacaClient:
    client = AlpacaClient.__new__(AlpacaClient)
    client._data = _FakeStockData(stock_quotes)
    client._crypto_data = _FakeCryptoData(crypto_quotes)
    return client


def test_returns_mid_prices_for_all_symbols():
    client = _make_client(
        {"AAPL": _FakeQuote(100.0, 102.0), "MSFT": _FakeQuote(200.0, 204.0)},
        {"BTC/USD": _FakeQuote(50000.0, 50100.0)},
    )
    prices = client.get_latest_prices(["AAPL", "MSFT", "BTC/USD"])
    assert prices["AAPL"] == 101.0
    assert prices["MSFT"] == 202.0
    assert prices["BTC/USD"] == 50050.0


def test_routes_stocks_and_crypto_to_separate_batched_calls():
    client = _make_client(
        {"AAPL": _FakeQuote(100.0, 102.0)},
        {"BTC/USD": _FakeQuote(50000.0, 50100.0)},
    )
    client.get_latest_prices(["AAPL", "BTC/USD", "MSFT"])
    # Stocks batched into ONE call (not one per symbol); crypto in its own call.
    assert client._data.calls == [["AAPL", "MSFT"]]
    assert client._crypto_data.calls == [["BTC/USD"]]


def test_missing_quote_is_zero():
    client = _make_client({"AAPL": _FakeQuote(100.0, 102.0)}, {})
    prices = client.get_latest_prices(["AAPL", "NOPE"])
    assert prices["AAPL"] == 101.0
    assert prices["NOPE"] == 0.0


def test_falls_back_to_one_side_when_other_is_missing():
    client = _make_client(
        {"ASKONLY": _FakeQuote(0.0, 50.0), "BIDONLY": _FakeQuote(40.0, 0.0)}, {}
    )
    prices = client.get_latest_prices(["ASKONLY", "BIDONLY"])
    assert prices["ASKONLY"] == 50.0
    assert prices["BIDONLY"] == 40.0


def test_only_stocks_skips_crypto_call_and_vice_versa():
    client = _make_client({"AAPL": _FakeQuote(100.0, 102.0)}, {})
    client.get_latest_prices(["AAPL"])
    assert client._data.calls == [["AAPL"]]
    assert client._crypto_data.calls == []  # no crypto -> no crypto request
