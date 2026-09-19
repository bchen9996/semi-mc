"""Opt-in sandbox integration tests against a real Alpaca paper account.

Every test here is `@pytest.mark.network` (excluded by default -- `uv run pytest` passes
`-m "not network"`; run these deliberately with `uv run pytest -m network`) and skips with
a clear reason if `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` aren't set, rather than failing --
a contributor without keys should see "skipped," not a red build.

Read-only checks first (confirm credentials work and the translation layer holds up
against a real response), then the two behaviors this phase could only guess at from
documentation: whether a duplicate `client_order_id` really is rejected the way
`docs/alpaca_notes.md` now documents, and a full submit/verify/cancel round trip on a
small, cheap, fractionable symbol. Never runs against anything but the paper endpoint --
`AlpacaPaperBroker.from_env` hardcodes `paper=True`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from dotenv import load_dotenv

from semimc.execution.alpaca_broker import AlpacaPaperBroker
from semimc.execution.broker import OrderRequest

# AlpacaPaperBroker.from_env() also calls this, but the skipif below reads os.environ
# directly at collection time -- before any test body (and its from_env() call) runs --
# so without loading .env here too, this suite would always skip even with real keys on
# disk, never actually running.
load_dotenv()

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not (os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY")),
        reason="ALPACA_API_KEY/ALPACA_SECRET_KEY not set -- copy .env.example to .env "
        "and fill in a paper account's keys to run this suite",
    ),
]

# A small, liquid, definitely-fractionable, definitely-tradable symbol -- kept separate
# from this project's semis universe so a stray test order is never mistaken for one.
SMOKE_SYMBOL = "AAPL"


@pytest.fixture
def broker() -> AlpacaPaperBroker:
    return AlpacaPaperBroker.from_env()


def test_get_account_returns_a_paper_account(broker):
    account = broker.get_account()
    assert account.account_number
    assert account.equity >= 0.0


def test_get_clock_and_calendar_round_trip(broker):
    clock = broker.get_clock()
    assert clock.timestamp.tzinfo is None
    days = broker.get_calendar(clock.timestamp.date(), clock.timestamp.date())
    assert len(days) <= 1  # zero on a weekend/holiday, one on a trading day


def test_list_positions_does_not_raise(broker):
    broker.list_positions()  # empty or not, this must not raise on a real account


def test_get_asset_translates_a_real_symbol(broker):
    asset = broker.get_asset(SMOKE_SYMBOL)
    assert asset.symbol == SMOKE_SYMBOL
    assert asset.tradable is True


def test_duplicate_client_order_id_falls_back_to_lookup(broker):
    """The behavior `docs/alpaca_notes.md` now documents from research, confirmed live:
    resubmitting the same client_order_id must resolve to the original order, not raise."""
    client_order_id = f"semimc-test-{uuid.uuid4().hex[:16]}"
    request = OrderRequest(
        symbol=SMOKE_SYMBOL,
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id=client_order_id,
        qty=1.0,
        limit_price=1.0,  # far below market: rests, never fills, safe to leave/cancel
    )
    try:
        first = broker.submit_order(request)
        second = broker.submit_order(request)
        assert second.id == first.id
        assert second.client_order_id == client_order_id
    finally:
        broker.cancel_order(first.id)


def test_submit_verify_cancel_round_trip(broker):
    client_order_id = f"semimc-test-{uuid.uuid4().hex[:16]}"
    request = OrderRequest(
        symbol=SMOKE_SYMBOL,
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id=client_order_id,
        qty=1.0,
        limit_price=1.0,
    )
    submitted = broker.submit_order(request)
    assert submitted.status in ("new", "accepted")

    found = broker.get_order_by_client_id(client_order_id)
    assert found is not None
    assert found.id == submitted.id

    broker.cancel_order(submitted.id)
    # Confirmed against a real paper account (2026-09-18): canceling an already-canceled
    # order succeeds silently rather than raising 422 -- see alpaca_broker.py's module
    # docstring. FakeBroker raises ValueError for this same case; real Alpaca does not.
    broker.cancel_order(submitted.id)
