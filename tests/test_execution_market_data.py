"""``execution/market_data.py``: T5's independent arrival-price lookup. Mocked
``StockHistoricalDataClient``, same boundary-mocking approach as
``tests/test_alpaca_broker.py`` -- no network, no credentials.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from alpaca.data.models.quotes import Quote, QuoteSet

from semimc.execution.market_data import AlpacaMarketDataClient

AT = dt.datetime(2026, 1, 5, 14, 30, 0, tzinfo=dt.UTC)  # Quote.timestamp is always aware UTC


def _quote(timestamp: dt.datetime, bid: float, ask: float) -> Quote:
    return Quote.model_construct(
        symbol="NVDA", timestamp=timestamp, bid_price=bid, bid_size=1.0, ask_price=ask, ask_size=1.0
    )


def _client_returning(quotes: list[Quote]) -> MagicMock:
    client = MagicMock()
    client.get_stock_quotes.return_value = QuoteSet.model_construct(data={"NVDA": quotes})
    return client


def test_arrival_price_is_the_midpoint_of_the_nearest_quote():
    quotes = [
        _quote(AT - dt.timedelta(minutes=4), 99.0, 101.0),  # farther away
        _quote(AT - dt.timedelta(seconds=5), 100.0, 100.5),  # nearest to `at`
    ]
    client = AlpacaMarketDataClient(_client_returning(quotes))
    price = client.arrival_price("NVDA", AT)
    assert price == pytest.approx(100.25)


def test_arrival_price_normalizes_a_naive_at_to_utc_rather_than_raising():
    """Quote.timestamp is always aware UTC (confirmed against a real account) -- a caller
    passing a naive `at` (e.g. a ledger timestamp round-tripped without care) must not hit
    "can't subtract offset-naive and offset-aware datetimes"."""
    naive_at = AT.replace(tzinfo=None)
    client = AlpacaMarketDataClient(_client_returning([_quote(AT, 100.0, 100.5)]))
    assert client.arrival_price("NVDA", naive_at) == pytest.approx(100.25)


def test_arrival_price_is_none_when_no_quotes_come_back():
    client = AlpacaMarketDataClient(_client_returning([]))
    assert client.arrival_price("NVDA", AT) is None


def test_arrival_price_is_none_for_a_non_positive_quote():
    client = AlpacaMarketDataClient(_client_returning([_quote(AT, 0.0, 0.0)]))
    assert client.arrival_price("NVDA", AT) is None


def test_from_env_raises_a_clear_error_when_keys_are_missing(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setattr("semimc.execution.market_data.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match=".env"):
        AlpacaMarketDataClient.from_env()


def test_from_env_reuses_the_same_env_vars_alpaca_paper_broker_uses(monkeypatch):
    from alpaca.data.historical import StockHistoricalDataClient

    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    client = AlpacaMarketDataClient.from_env()
    assert isinstance(client._client, StockHistoricalDataClient)


def test_from_env_reads_a_named_key_pair_for_a_second_account(monkeypatch):
    """S4 (BRIEF_INTRADAY.md) reads quotes on behalf of its own, separate paper account --
    same account-separation reasoning as AlpacaPaperBroker.from_env."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_S4_API_KEY", "s4-key")
    monkeypatch.setenv("ALPACA_S4_SECRET_KEY", "s4-secret")
    client = AlpacaMarketDataClient.from_env("ALPACA_S4_API_KEY", "ALPACA_S4_SECRET_KEY")
    assert client._client is not None
