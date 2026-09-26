"""T5's independent price source (`THESES.md`): a real quote, pulled after the fact, that
``execution/orders.py`` never consulted when it priced an order. Mirrors
``alpaca_broker.py``'s shape exactly, for the same reason -- a plain constructor taking an
already-built SDK client for unit tests, a ``from_env()`` for real use.

New capability, not reuse of something already built: this project has never called
``alpaca.data`` anywhere before this. It needs no new credentials -- ``StockHistoricalData
Client`` takes the same ``ALPACA_API_KEY``/``ALPACA_SECRET_KEY`` `.env` values
``AlpacaPaperBroker.from_env`` already reads.

Verified directly against the installed ``alpaca-py`` 0.44.0 models (not assumed):
``Quote.bid_price``/``.ask_price`` are plain ``float``, unlike the *trading* API's
numeric-string fields (`alpaca_broker.py`'s docstring) -- these are a different
subpackage (`alpaca.data`, not `alpaca.trading`) with its own, differently-typed models.
``StockHistoricalDataClient.get_stock_quotes`` returns a ``QuoteSet`` whose ``.data`` is a
``dict[str, list[Quote]]`` keyed by symbol.

**Confirmed against a real account (2026-09-19)**: ``Quote.timestamp`` comes back aware
UTC, not naive -- the opposite convention from ``alpaca_broker.py``'s ``Calendar``/
``Clock`` handling, which strips or never attaches tzinfo. First attempt at this
integration test failed on exactly that mismatch (``can't subtract offset-naive and
offset-aware datetimes``); :meth:`AlpacaMarketDataClient.arrival_price` now normalizes a
naive ``at`` to UTC rather than assuming either convention. A historical quote pull with
``feed=DataFeed.IEX`` (the unambiguously-free option) successfully returned real quotes
for a 3-day-old timestamp -- confirms IEX has usable historical coverage; whether
``SIP``/``DELAYED_SIP`` would also work (and return a more complete consolidated tape) is
still unconfirmed, not yet worth spending a test on until IEX's coverage proves
insufficient for a real cost report.

**Phase 12b (S4 Rollout Stage 4, options shadow pricing)** adds
:class:`AlpacaOptionsMarketDataClient`, a second, unrelated client in this same file --
options quotes are a different alpaca-py subpackage entirely (``OptionHistoricalDataClient``
plus a read-only ``TradingClient`` for open interest, see that class's own docstring for
why both are needed). Can only be verified locally up to "constructs the request correctly
and would hit the right endpoint" -- no local Alpaca options-capable credentials, same wall
every other credentialed S4 piece in this project has already hit.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from typing import Literal

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.models import Quote
from alpaca.data.requests import OptionBarsRequest, OptionChainRequest, StockQuotesRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType
from dotenv import load_dotenv


class AlpacaMarketDataClient:
    def __init__(self, client: StockHistoricalDataClient) -> None:
        self._client = client

    @classmethod
    def from_env(
        cls, key_env: str = "ALPACA_API_KEY", secret_env: str = "ALPACA_SECRET_KEY"
    ) -> AlpacaMarketDataClient:
        """Reuses whichever ``.env`` key pair ``AlpacaPaperBroker.from_env`` would use --
        no new secret, no new `.env.example` entry. ``key_env``/``secret_env`` default to
        s0/s3's pair, unchanged from before this parameter existed; pass S4's own pair
        (``"ALPACA_S4_API_KEY"``/``"ALPACA_S4_SECRET_KEY"``) when reading quotes on its
        behalf, for the same account-separation reason as ``AlpacaPaperBroker.from_env``.
        """
        load_dotenv()
        api_key = os.environ.get(key_env)
        secret_key = os.environ.get(secret_env)
        if not api_key or not secret_key:
            raise RuntimeError(
                f"{key_env}/{secret_env} not set -- copy .env.example to .env and fill in "
                "a paper account's keys"
            )
        return cls(StockHistoricalDataClient(api_key=api_key, secret_key=secret_key))

    def _nearest_quote(
        self, symbol: str, at: dt.datetime, window: dt.timedelta, feed: DataFeed
    ) -> Quote | None:
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.UTC)
        request = StockQuotesRequest(
            symbol_or_symbols=symbol, start=at - window, end=at + window, feed=feed
        )
        response = self._client.get_stock_quotes(request)
        quotes = response.data.get(symbol, [])
        if not quotes:
            return None
        return min(quotes, key=lambda q: abs((q.timestamp - at).total_seconds()))

    def arrival_price(
        self,
        symbol: str,
        at: dt.datetime,
        window: dt.timedelta = dt.timedelta(minutes=5),
        feed: DataFeed = DataFeed.IEX,
    ) -> float | None:
        """The NBBO midpoint of the quote nearest ``at``, within ``+/- window``.

        ``at`` may be naive or aware -- ``Quote.timestamp`` always comes back aware UTC
        (confirmed directly against a real account; unlike the *trading* API's naive-
        Clock convention in ``alpaca_broker.py``, nothing here strips tzinfo), so a naive
        ``at`` is assumed UTC rather than compared directly and raising.

        ``None`` if no quote comes back in that window (an illiquid moment, a bad symbol,
        a feed with no coverage) -- a single missing quote must not crash a whole cost
        report; the caller (``analytics/execution_cost.py``) skips that fill instead.
        """
        nearest = self._nearest_quote(symbol, at, window, feed)
        if nearest is None:
            return None
        if nearest.bid_price is None or nearest.ask_price is None:
            return None
        if nearest.bid_price <= 0 or nearest.ask_price <= 0:
            return None
        return (nearest.bid_price + nearest.ask_price) / 2.0

    def latest_quote(
        self,
        symbol: str,
        at: dt.datetime,
        window: dt.timedelta = dt.timedelta(minutes=5),
        feed: DataFeed = DataFeed.IEX,
    ) -> Quote | None:
        """The whole quote nearest ``at`` (bid, ask, and its own timestamp) -- what
        ``intraday/pretrade.py::check_spread_gate`` needs (S4, Phase 5) and a plain
        midpoint (``arrival_price``) can't provide. Same nearest-quote lookup, same
        ``None``-on-no-coverage contract."""
        return self._nearest_quote(symbol, at, window, feed)


def _api_status_code(exc: APIError) -> int | None:
    """Same defensive extraction as ``alpaca_broker.py::_parse_api_error`` (that one is
    module-private there and this file has no other need of its message half) --
    ``APIError.status_code`` parses the raw response body and can itself raise on a
    malformed body, which must never mask the original failure with a second traceback."""
    try:
        return exc.status_code
    except Exception:  # noqa: BLE001 - never let error parsing hide the real error
        return None


@dataclass(frozen=True)
class OptionMarketQuote:
    """One option contract's quote plus the liquidity figures Stage 4's gates need
    (``intraday/options.py::OptionQuote``, which this gets converted into 1:1 at the
    ``intraday/jobs.py`` call site) -- kept as this module's own type, not that one,
    so ``execution/`` never imports ``intraday/`` (this project's layering runs the other
    way: intraday builds on execution, matching every other client this file/``
    alpaca_broker.py`` already export).
    """

    occ_symbol: str
    bid: float
    ask: float
    open_interest: int
    volume: int
    # Which Alpaca plan actually answered -- Stage 4's own stated purpose is judging
    # whether *this* feed produces sane pricing, so which one served each quote is part
    # of the record, not an implementation detail. See :meth:`AlpacaOptionsMarketDataClient
    # .quote_contract` for how this is actually determined (an OPRA request that fails
    # with an entitlement error retries against the indicative feed).
    feed: Literal["indicative", "opra"]


class AlpacaOptionsMarketDataClient:
    """S4's own options-quote source (Phase 12b, ``BRIEF_INTRADAY.md``'s "## Options"
    section) -- a third Alpaca client alongside :class:`AlpacaMarketDataClient` (equities
    data) and ``alpaca_broker.py``'s ``AlpacaPaperBroker`` (order submission), same
    "one client per concern, one key pair" shape those two already establish.

    Needs both ``OptionHistoricalDataClient`` (chain/quote/bars -- market data) and
    ``TradingClient`` (contract metadata's own ``open_interest`` field, which the data API
    doesn't expose at all -- confirmed directly against the installed alpaca-py 0.44.0
    models: ``OptionsSnapshot`` carries only ``latest_trade``/``latest_quote``/
    ``implied_volatility``/``greeks``, no open interest, no daily volume).
    ``TradingClient`` here is used strictly read-only (``get_option_contract``), the same
    "``paper=True`` hardcoded, no live-trading path" guarantee ``AlpacaPaperBroker.
    from_env`` already makes -- this class never calls ``submit_order``.
    """

    def __init__(
        self, data_client: OptionHistoricalDataClient, trading_client: TradingClient
    ) -> None:
        self._data = data_client
        self._trading = trading_client

    @classmethod
    def from_env(
        cls, key_env: str = "ALPACA_S4_API_KEY", secret_env: str = "ALPACA_S4_SECRET_KEY"
    ) -> AlpacaOptionsMarketDataClient:
        """``key_env``/``secret_env`` default to S4's own pair (unlike
        ``AlpacaMarketDataClient.from_env``, which defaults to s0/s3's) -- S4 is currently
        this class's only caller and always trades its own, separate paper account."""
        load_dotenv()
        api_key = os.environ.get(key_env)
        secret_key = os.environ.get(secret_env)
        if not api_key or not secret_key:
            raise RuntimeError(
                f"{key_env}/{secret_env} not set -- copy .env.example to .env and fill in "
                "a paper account's keys"
            )
        return cls(
            OptionHistoricalDataClient(api_key=api_key, secret_key=secret_key),
            TradingClient(api_key=api_key, secret_key=secret_key, paper=True),
        )

    def _fetch_chain_entry(
        self,
        underlying_symbol: str,
        expiry: dt.date,
        strike: float,
        contract_type: ContractType,
        feed: OptionsFeed,
    ) -> tuple[str, object] | None:
        request = OptionChainRequest(
            underlying_symbol=underlying_symbol,
            strike_price_gte=strike,
            strike_price_lte=strike,
            expiration_date=expiry,
            type=contract_type,
            feed=feed,
        )
        response = self._data.get_option_chain(request)
        if not response:
            return None
        return next(iter(response.items()))

    def _fetch_open_interest(self, occ_symbol: str) -> int:
        try:
            contract = self._trading.get_option_contract(occ_symbol)
        except APIError:
            return 0
        if contract is None or contract.open_interest is None:
            return 0
        try:
            return int(contract.open_interest)
        except (TypeError, ValueError):
            return 0

    def _fetch_volume(self, occ_symbol: str, as_of: dt.datetime) -> int:
        """Today's contract volume via the daily bar -- the only Alpaca surface that
        carries it at all (see this class's own docstring). ``0`` if no bar has formed yet
        today (thin trading, or called before Alpaca's own aggregation catches up) rather
        than raising -- a genuinely illiquid contract is exactly what ``check_liquidity``
        (``intraday/options.py``) exists to reject, not something this fetch should crash
        on."""
        request = OptionBarsRequest(
            symbol_or_symbols=occ_symbol,
            start=as_of.replace(hour=0, minute=0, second=0, microsecond=0),
            end=as_of,
            timeframe=TimeFrame.Day,
        )
        try:
            response = self._data.get_option_bars(request)
        except APIError:
            return 0
        bars = response.data.get(occ_symbol, [])
        if not bars:
            return 0
        return int(bars[-1].volume)

    def quote_contract(
        self,
        underlying_symbol: str,
        expiry: dt.date,
        strike: float,
        right: Literal["call", "put"],
        as_of: dt.datetime,
    ) -> OptionMarketQuote | None:
        """One already-fully-specified contract's quote, open interest, and today's
        volume -- ``None`` if no matching contract exists or it has no live quote (a bad
        strike/expiry, an illiquid moment) rather than raising; a single missing option
        quote must not crash a whole cycle, same tolerance ``arrival_price`` already
        applies to equities.

        Tries the real-time OPRA feed first; on an entitlement error (403 -- this paper
        account isn't subscribed), falls back to the free indicative feed and records
        that on the result. This *is* how Stage 4 answers its own stated question ("does
        the available feed produce sane pricing") -- which branch actually ran is exactly
        the fact this method exists to surface, not something a caller reasons about
        separately.
        """
        contract_type = ContractType.CALL if right == "call" else ContractType.PUT
        try:
            entry = self._fetch_chain_entry(
                underlying_symbol, expiry, strike, contract_type, OptionsFeed.OPRA
            )
            feed_used: Literal["indicative", "opra"] = "opra"
        except APIError as exc:
            if _api_status_code(exc) not in (401, 403):
                raise
            entry = self._fetch_chain_entry(
                underlying_symbol, expiry, strike, contract_type, OptionsFeed.INDICATIVE
            )
            feed_used = "indicative"
        if entry is None:
            return None
        occ_symbol, snapshot = entry
        quote = snapshot.latest_quote
        if quote is None or quote.bid_price is None or quote.ask_price is None:
            return None
        if quote.bid_price <= 0 or quote.ask_price <= 0:
            return None
        return OptionMarketQuote(
            occ_symbol=occ_symbol,
            bid=quote.bid_price,
            ask=quote.ask_price,
            open_interest=self._fetch_open_interest(occ_symbol),
            volume=self._fetch_volume(occ_symbol, as_of),
            feed=feed_used,
        )
