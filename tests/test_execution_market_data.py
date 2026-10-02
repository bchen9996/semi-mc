"""``execution/market_data.py``: T5's independent arrival-price lookup, and (Phase 12b,
S4 Rollout Stage 4) ``AlpacaOptionsMarketDataClient``. Mocked ``StockHistoricalDataClient``/
``OptionHistoricalDataClient``/``TradingClient``, same boundary-mocking approach as
``tests/test_alpaca_broker.py`` -- no network, no credentials.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.data.models.bars import Bar, BarSet
from alpaca.data.models.quotes import Quote, QuoteSet
from alpaca.data.models.snapshots import OptionsSnapshot
from alpaca.trading.models import OptionContract

from semimc.execution.market_data import (
    AlpacaMarketDataClient,
    AlpacaOptionsMarketDataClient,
    OptionMarketQuote,
)

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


def test_latest_quote_returns_the_whole_nearest_quote():
    """S4's spread gate (Phase 5, `intraday/pretrade.py::check_spread_gate`) needs bid,
    ask, and the quote's own timestamp -- a plain midpoint (`arrival_price`) can't
    provide any of those."""
    quotes = [
        _quote(AT - dt.timedelta(minutes=4), 99.0, 101.0),  # farther away
        _quote(AT - dt.timedelta(seconds=5), 100.0, 100.5),  # nearest to `at`
    ]
    client = AlpacaMarketDataClient(_client_returning(quotes))
    quote = client.latest_quote("NVDA", AT)
    assert quote is not None
    assert quote.bid_price == pytest.approx(100.0)
    assert quote.ask_price == pytest.approx(100.5)
    assert quote.timestamp == AT - dt.timedelta(seconds=5)


def test_latest_quote_is_none_when_no_quotes_come_back():
    client = AlpacaMarketDataClient(_client_returning([]))
    assert client.latest_quote("NVDA", AT) is None


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


# -- AlpacaOptionsMarketDataClient (Phase 12b, S4 Rollout Stage 4) -----------------------

EXPIRY = dt.date(2026, 10, 21)
OCC_SYMBOL = "NVDA261021C00190000"


def _api_error(status_code: int) -> APIError:
    """Same construction ``tests/test_alpaca_broker.py::_api_error`` already uses --
    ``APIError.status_code`` reads ``http_error.response.status_code``."""
    response = MagicMock()
    response.status_code = status_code
    http_error = MagicMock()
    http_error.response = response
    body = json.dumps({"code": status_code, "message": "error"})
    return APIError(body, http_error)


def _snapshot(bid: float | None, ask: float | None) -> OptionsSnapshot:
    quote = None if bid is None else Quote.model_construct(bid_price=bid, ask_price=ask)
    return OptionsSnapshot.model_construct(symbol=OCC_SYMBOL, latest_quote=quote)


def _options_client(
    data_client: MagicMock | None = None, trading_client: MagicMock | None = None
) -> AlpacaOptionsMarketDataClient:
    return AlpacaOptionsMarketDataClient(data_client or MagicMock(), trading_client or MagicMock())


def test_quote_contract_returns_bid_ask_open_interest_and_volume_from_opra():
    data_client = MagicMock()
    data_client.get_option_chain.return_value = {OCC_SYMBOL: _snapshot(9.5, 10.5)}
    data_client.get_option_bars.return_value = BarSet.model_construct(
        data={OCC_SYMBOL: [Bar.model_construct(volume=42)]}
    )
    trading_client = MagicMock()
    trading_client.get_option_contract.return_value = OptionContract.model_construct(
        open_interest="150"
    )
    client = _options_client(data_client, trading_client)

    quote = client.quote_contract("NVDA", EXPIRY, 190.0, "call", AT)

    assert quote == OptionMarketQuote(
        occ_symbol=OCC_SYMBOL, bid=9.5, ask=10.5, open_interest=150, volume=42, feed="opra"
    )


def test_quote_contract_falls_back_to_indicative_on_an_entitlement_error():
    """Stage 4's own stated purpose is judging whether the feed produces sane pricing --
    which feed actually answered must be recorded accurately, not assumed."""
    data_client = MagicMock()
    data_client.get_option_chain.side_effect = [
        _api_error(403),
        {OCC_SYMBOL: _snapshot(9.5, 10.5)},
    ]
    data_client.get_option_bars.return_value = BarSet.model_construct(data={})
    trading_client = MagicMock()
    trading_client.get_option_contract.return_value = OptionContract.model_construct(
        open_interest=None
    )
    client = _options_client(data_client, trading_client)

    quote = client.quote_contract("NVDA", EXPIRY, 190.0, "call", AT)

    assert quote is not None
    assert quote.feed == "indicative"
    assert data_client.get_option_chain.call_count == 2


def test_quote_contract_reraises_a_non_entitlement_api_error():
    data_client = MagicMock()
    data_client.get_option_chain.side_effect = _api_error(500)
    client = _options_client(data_client)

    with pytest.raises(APIError):
        client.quote_contract("NVDA", EXPIRY, 190.0, "call", AT)


def test_quote_contract_is_none_when_no_contract_matches():
    data_client = MagicMock()
    data_client.get_option_chain.return_value = {}
    client = _options_client(data_client)

    assert client.quote_contract("NVDA", EXPIRY, 190.0, "call", AT) is None


def test_quote_contract_is_none_for_a_missing_quote():
    data_client = MagicMock()
    data_client.get_option_chain.return_value = {OCC_SYMBOL: _snapshot(None, None)}
    client = _options_client(data_client)

    assert client.quote_contract("NVDA", EXPIRY, 190.0, "call", AT) is None


def test_quote_contract_is_none_for_a_non_positive_quote():
    data_client = MagicMock()
    data_client.get_option_chain.return_value = {OCC_SYMBOL: _snapshot(0.0, 0.0)}
    client = _options_client(data_client)

    assert client.quote_contract("NVDA", EXPIRY, 190.0, "call", AT) is None


def test_quote_contract_defaults_open_interest_and_volume_to_zero_on_missing_data():
    """`OptionsSnapshot` carries neither field at all (confirmed directly against the
    installed alpaca-py models, see this class's own docstring) -- a contract lookup that
    fails, or a contract genuinely missing `open_interest`, must not crash a whole quote,
    the same "a single missing figure must not crash a whole cycle" tolerance every other
    method in this file already applies."""
    data_client = MagicMock()
    data_client.get_option_chain.return_value = {OCC_SYMBOL: _snapshot(9.5, 10.5)}
    data_client.get_option_bars.side_effect = _api_error(404)
    trading_client = MagicMock()
    trading_client.get_option_contract.side_effect = _api_error(404)
    client = _options_client(data_client, trading_client)

    quote = client.quote_contract("NVDA", EXPIRY, 190.0, "call", AT)

    assert quote is not None
    assert quote.open_interest == 0
    assert quote.volume == 0


def test_options_from_env_raises_a_clear_error_when_keys_are_missing(monkeypatch):
    monkeypatch.delenv("ALPACA_S4_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_S4_SECRET_KEY", raising=False)
    monkeypatch.setattr("semimc.execution.market_data.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match=".env"):
        AlpacaOptionsMarketDataClient.from_env()


def test_options_from_env_defaults_to_s4s_own_key_pair(monkeypatch):
    """Unlike `AlpacaMarketDataClient.from_env` (defaults to s0/s3's pair), this class's
    only caller is S4, which always trades its own, separate paper account."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_S4_API_KEY", "s4-key")
    monkeypatch.setenv("ALPACA_S4_SECRET_KEY", "s4-secret")
    client = AlpacaOptionsMarketDataClient.from_env()
    assert client._data is not None
    assert client._trading is not None


# -- latest trades and bars (2026-09-30: pre-market fallback, the Trader's session data) ---


def test_latest_trades_returns_price_and_timestamp_per_symbol_from_iex():
    from alpaca.data.enums import DataFeed
    from alpaca.data.models.trades import Trade

    client = MagicMock()
    client.get_stock_latest_trade.return_value = {
        "NVDA": Trade.model_construct(symbol="NVDA", timestamp=AT, price=190.5, size=10),
        "AMD": None,
    }
    trades = AlpacaMarketDataClient(client).latest_trades(["NVDA", "AMD"])

    assert trades == {"NVDA": (190.5, AT)}
    request = client.get_stock_latest_trade.call_args.args[0]
    assert request.symbol_or_symbols == ["NVDA", "AMD"]
    assert request.feed == DataFeed.IEX


def test_latest_quotes_asks_the_named_feed_for_every_symbol_in_one_request():
    """2026-10-01: the spread gate reads the free plan's 15-minute-delayed consolidated
    quote, passed as config's plain string."""
    from alpaca.data.enums import DataFeed

    client = MagicMock()
    nvda = _quote(AT, 190.0, 190.02)
    client.get_stock_latest_quote.return_value = {"NVDA": nvda, "AMD": None}
    quotes = AlpacaMarketDataClient(client).latest_quotes(["NVDA", "AMD"], feed="delayed_sip")

    assert quotes == {"NVDA": nvda}
    request = client.get_stock_latest_quote.call_args.args[0]
    assert request.symbol_or_symbols == ["NVDA", "AMD"]
    assert request.feed == DataFeed.DELAYED_SIP


def test_latest_trades_and_bars_make_no_request_for_no_symbols():
    client = MagicMock()
    data = AlpacaMarketDataClient(client)
    assert data.latest_trades([]) == {}
    assert data.latest_quotes([]) == {}
    client.get_stock_latest_quote.assert_not_called()
    assert data.bars([], AT, AT) == {}
    client.get_stock_latest_trade.assert_not_called()
    client.get_stock_bars.assert_not_called()


def test_bars_requests_split_adjusted_iex_bars_with_utc_bounds():
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.timeframe import TimeFrameUnit

    bar = Bar.model_construct(
        symbol="NVDA", timestamp=AT, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0
    )
    client = MagicMock()
    client.get_stock_bars.return_value = BarSet.model_construct(data={"NVDA": [bar]})
    naive_start = (AT - dt.timedelta(days=5)).replace(tzinfo=None)

    out = AlpacaMarketDataClient(client).bars(["NVDA"], naive_start, AT, minutes=5)

    assert out == {"NVDA": [bar]}
    request = client.get_stock_bars.call_args.args[0]
    assert request.timeframe.amount == 5
    assert request.timeframe.unit == TimeFrameUnit.Minute
    assert request.adjustment == Adjustment.SPLIT
    assert request.feed == DataFeed.IEX
    # alpaca-py stores request times as naive UTC; a naive bound must arrive unshifted.
    assert request.start.replace(tzinfo=None) == naive_start
