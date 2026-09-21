"""S4's news source for the Researcher (`BRIEF_INTRADAY.md`'s `research` row, Phase 6).

New capability, not reuse of something already built: this project has never called
``alpaca.data.historical.news`` anywhere before this. Mirrors ``market_data.py``'s
``AlpacaMarketDataClient`` shape exactly -- a plain constructor over an injected SDK client
for unit tests, a ``from_env()`` for real use -- but deviates in one deliberate way:
``from_env``'s default ``key_env``/``secret_env`` are S4's own pair
(``ALPACA_S4_API_KEY``/``ALPACA_S4_SECRET_KEY``), not s0/s3's, since nothing outside S4 will
ever call this client (unlike ``AlpacaMarketDataClient``, which s0/s3's own arrival-price
cost report already used before S4 existed).

Verified directly against the installed ``alpaca-py`` package (not assumed): ``NewsClient``
takes ``api_key``/``secret_key`` directly, with no shared base client with
``StockHistoricalDataClient``. ``NewsRequest.symbols`` is one comma-joined string, not a
list. ``NewsClient.get_news`` returns a ``NewsSet`` whose ``.data`` is always
``{"news": [News, ...]}`` -- a flat list, not keyed by symbol despite the type hint on
``NewsSet.data``. Each ``News`` has ``.id``, ``.headline``, ``.source``, ``.url``,
``.summary``, ``.created_at`` (a required, aware ``datetime`` field per its own pydantic
schema -- same convention as ``market_data.py``'s ``Quote.timestamp``), ``.symbols``
(``list[str]``), ``.author``, ``.content``.
"""

from __future__ import annotations

import datetime as dt
import os

from alpaca.data.historical.news import NewsClient
from alpaca.data.models.news import News
from alpaca.data.requests import NewsRequest
from dotenv import load_dotenv


class AlpacaNewsClient:
    def __init__(self, client: NewsClient) -> None:
        self._client = client

    @classmethod
    def from_env(
        cls, key_env: str = "ALPACA_S4_API_KEY", secret_env: str = "ALPACA_S4_SECRET_KEY"
    ) -> AlpacaNewsClient:
        """Defaults to S4's own key pair -- see module docstring for why this differs from
        ``AlpacaMarketDataClient.from_env``'s s0/s3 default."""
        load_dotenv()
        api_key = os.environ.get(key_env)
        secret_key = os.environ.get(secret_env)
        if not api_key or not secret_key:
            raise RuntimeError(
                f"{key_env}/{secret_env} not set -- copy .env.example to .env and fill in "
                "a paper account's keys"
            )
        return cls(NewsClient(api_key=api_key, secret_key=secret_key))

    def get_news(
        self, symbols: list[str], start: dt.datetime, end: dt.datetime, limit: int = 50
    ) -> list[News]:
        """Every article mentioning any of ``symbols`` between ``start`` and ``end``,
        unchanged (same "return the SDK's own model" convention as
        ``market_data.py::latest_quote``). ``[]`` on no coverage, never ``None`` or an
        exception -- "no overnight news" is a completely routine outcome, unlike a missing
        quote mid-session (``market_data.py::arrival_price``'s own reasoning).
        """
        request = NewsRequest(
            symbols=",".join(symbols),
            start=start,
            end=end,
            limit=limit,
            exclude_contentless=True,
        )
        response = self._client.get_news(request)
        return list(response.data.get("news", []))
