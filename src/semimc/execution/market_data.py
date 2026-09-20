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
"""

from __future__ import annotations

import datetime as dt
import os

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest
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
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.UTC)
        request = StockQuotesRequest(
            symbol_or_symbols=symbol, start=at - window, end=at + window, feed=feed
        )
        response = self._client.get_stock_quotes(request)
        quotes = response.data.get(symbol, [])
        if not quotes:
            return None

        nearest = min(quotes, key=lambda q: abs((q.timestamp - at).total_seconds()))
        if nearest.bid_price is None or nearest.ask_price is None:
            return None
        if nearest.bid_price <= 0 or nearest.ask_price <= 0:
            return None
        return (nearest.bid_price + nearest.ask_price) / 2.0
