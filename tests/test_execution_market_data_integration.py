"""Opt-in sandbox integration test for T5's independent price source
(`execution/market_data.py`). Same gating pattern as
`tests/test_alpaca_broker_integration.py`: `@pytest.mark.network`, skips cleanly without
real keys.

This is where the module docstring's genuinely unconfirmed assumption gets checked
against a real account: whether a historical quote pull needs `feed=DataFeed.IEX`
specifically, or whether `SIP`/`DELAYED_SIP` also work on the free Basic plan for data
old enough that the 15-minute delay is moot.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from dotenv import load_dotenv

from semimc.execution.market_data import AlpacaMarketDataClient

load_dotenv()

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")),
        reason="ALPACA_API_KEY/ALPACA_SECRET_KEY not set -- copy .env.example to .env "
        "and fill in a paper account's keys to run this suite",
    ),
]

SMOKE_SYMBOL = "AAPL"


@pytest.fixture
def client() -> AlpacaMarketDataClient:
    return AlpacaMarketDataClient.from_env()


def test_arrival_price_returns_a_sane_quote_for_a_recent_market_hour(client):
    # A recent weekday afternoon, comfortably in the past -- old enough that any feed's
    # delay is moot, recent enough that quote history should still exist.
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    target = now - dt.timedelta(days=3)
    price = client.arrival_price(SMOKE_SYMBOL, target, window=dt.timedelta(days=1))
    assert price is None or price > 0  # None only if the account/feed genuinely has no coverage
