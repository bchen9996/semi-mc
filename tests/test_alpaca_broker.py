"""`execution/alpaca_broker.py`: translating real `alpaca-py` SDK responses into this
project's `Broker` Protocol dataclasses. No network -- `TradingClient` is replaced with a
plain mock per test; real `alpaca-py` model instances (via `model_construct`, which skips
validation so a test only needs to set the fields it cares about) stand in for what the
SDK would actually return, so the translation code exercises real attribute/enum shapes,
not a generic mock's fabricated attributes.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import MagicMock

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import AccountStatus, AssetStatus, OrderClass, PositionSide
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.enums import OrderStatus as AlpacaStatus
from alpaca.trading.enums import OrderType as AlpacaType
from alpaca.trading.enums import TimeInForce as AlpacaTIF
from alpaca.trading.models import Asset as AlpacaAsset
from alpaca.trading.models import Calendar as AlpacaCalendar
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount

from semimc.execution.alpaca_broker import AlpacaPaperBroker
from semimc.execution.broker import BrokerError, OrderRequest


def _api_error(status_code: int, message: str) -> APIError:
    response = MagicMock()
    response.status_code = status_code
    http_error = MagicMock()
    http_error.response = response
    body = json.dumps({"code": status_code, "message": message})
    return APIError(body, http_error)


def _order(**overrides) -> AlpacaOrder:
    fields = {
        "id": "b6e1c1e2-0000-4000-8000-000000000000",
        "client_order_id": "s0-20260101-NVDA-abc",
        "symbol": "NVDA",
        "side": AlpacaSide.BUY,
        "type": AlpacaType.LIMIT,
        "order_class": OrderClass.SIMPLE,
        "time_in_force": AlpacaTIF.DAY,
        "status": AlpacaStatus.NEW,
        "qty": "10",
        "notional": None,
        "filled_qty": "0",
        "filled_avg_price": None,
        "limit_price": "100.0",
        "stop_price": None,
        "submitted_at": dt.datetime(2026, 1, 5, 15, 0, tzinfo=dt.UTC),
        "filled_at": None,
    }
    fields.update(overrides)
    return AlpacaOrder.model_construct(**fields)


def _broker_with(client: MagicMock) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(client)


# -- account / positions / assets / clock ------------------------------------------------


def test_get_account_coerces_string_numerics():
    client = MagicMock()
    client.get_account.return_value = TradeAccount.model_construct(
        account_number="PA123",
        status=AccountStatus.ACTIVE,
        equity="10000.50",
        cash="5000.25",
        buying_power="5000.25",
    )
    account = _broker_with(client).get_account()
    assert account.account_number == "PA123"
    assert account.status == "ACTIVE"
    assert account.equity == pytest.approx(10000.50)
    assert isinstance(account.equity, float)


def test_get_account_raises_when_a_money_field_is_missing():
    client = MagicMock()
    client.get_account.return_value = TradeAccount.model_construct(
        account_number="PA123",
        status=AccountStatus.ACTIVE,
        equity=None,
        cash="1.0",
        buying_power="1.0",
    )
    with pytest.raises(BrokerError, match="equity"):
        _broker_with(client).get_account()


def test_list_positions_defaults_a_missing_market_value_to_zero():
    client = MagicMock()
    client.get_all_positions.return_value = [
        AlpacaPosition.model_construct(
            symbol="NVDA",
            qty="10",
            side=PositionSide.LONG,
            market_value=None,
            avg_entry_price="100.0",
        )
    ]
    positions = _broker_with(client).list_positions()
    assert positions[0].market_value == 0.0
    assert positions[0].qty == pytest.approx(10.0)
    assert positions[0].side == "long"


def test_get_asset_translates_flags():
    client = MagicMock()
    client.get_asset.return_value = AlpacaAsset.model_construct(
        symbol="NVDA",
        status=AssetStatus.ACTIVE,
        tradable=True,
        fractionable=False,
        shortable=True,
        easy_to_borrow=False,
    )
    asset = _broker_with(client).get_asset("NVDA")
    assert asset.tradable is True
    assert asset.fractionable is False


def test_get_clock_strips_timezone_to_match_fakebroker():
    aware = dt.datetime(2026, 1, 5, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    client = MagicMock()
    client.get_clock.return_value = AlpacaClock.model_construct(
        timestamp=aware,
        is_open=True,
        next_open=aware,
        next_close=aware,
    )
    clock = _broker_with(client).get_clock()
    assert clock.timestamp.tzinfo is None
    assert clock.timestamp.replace(tzinfo=None) == aware.replace(tzinfo=None)


def test_get_calendar_extracts_time_of_day_with_no_conversion():
    client = MagicMock()
    client.get_calendar.return_value = [
        AlpacaCalendar(date="2026-01-05", open="09:30", close="16:00")
    ]
    days = _broker_with(client).get_calendar(dt.date(2026, 1, 5), dt.date(2026, 1, 5))
    assert days[0].date == dt.date(2026, 1, 5)
    assert days[0].open == dt.time(9, 30)
    assert days[0].close == dt.time(16, 0)


# -- orders --------------------------------------------------------------------------


def _request(**overrides) -> OrderRequest:
    fields = dict(
        symbol="NVDA",
        side="buy",
        type="limit",
        time_in_force="day",
        client_order_id="s0-20260101-NVDA-abc",
        qty=10.0,
        limit_price=100.0,
    )
    fields.update(overrides)
    return OrderRequest(**fields)


def test_submit_order_translates_the_response():
    client = MagicMock()
    client.submit_order.return_value = _order(status=AlpacaStatus.FILLED, filled_qty="10")
    order = _broker_with(client).submit_order(_request())
    assert order.status == "filled"
    assert order.client_order_id == "s0-20260101-NVDA-abc"
    assert order.limit_price == pytest.approx(100.0)
    submitted_kwargs = client.submit_order.call_args.kwargs
    assert submitted_kwargs["order_data"].qty == 10.0


def test_submit_order_attaches_an_oto_stop_loss_leg():
    """S4's protective stop (Phase 3) -- a real order resting at Alpaca, not a second
    order submitted later by a polling process."""
    client = MagicMock()
    client.submit_order.return_value = _order(status=AlpacaStatus.NEW)
    request = _request(order_class="oto", stop_loss_price=185.0)
    _broker_with(client).submit_order(request)
    built = client.submit_order.call_args.kwargs["order_data"]
    assert built.order_class == OrderClass.OTO
    assert built.stop_loss.stop_price == 185.0
    assert built.stop_loss.limit_price is None


def test_submit_order_leaves_order_class_and_stop_loss_unset_for_a_simple_order():
    """Confirmed against the installed alpaca-py 0.44.0 model: `order_class`/`stop_loss`
    default to `None`, not `OrderClass.SIMPLE` -- this code only sets them for `"oto"`."""
    client = MagicMock()
    client.submit_order.return_value = _order(status=AlpacaStatus.NEW)
    _broker_with(client).submit_order(_request())
    built = client.submit_order.call_args.kwargs["order_data"]
    assert built.order_class is None
    assert built.stop_loss is None


def test_translate_order_recurses_into_legs():
    """S4's close/trim path (Phase 4) needs to find a filled entry's resting stop-loss
    leg -- `Order.legs` must carry it, translated the same way the parent is."""
    stop_leg = _order(
        id="leg-1",
        client_order_id="s0-20260101-NVDA-abc::stop",
        side=AlpacaSide.SELL,
        type=AlpacaType.STOP,
        order_class=OrderClass.OTO,
        status=AlpacaStatus.NEW,
    )
    parent = _order(status=AlpacaStatus.NEW, order_class=OrderClass.OTO, legs=[stop_leg])
    client = MagicMock()
    client.submit_order.return_value = parent

    order = _broker_with(client).submit_order(_request(order_class="oto", stop_loss_price=95.0))

    assert order.legs is not None
    assert len(order.legs) == 1
    assert order.legs[0].id == "leg-1"
    assert order.legs[0].client_order_id == "s0-20260101-NVDA-abc::stop"
    assert order.legs[0].side == "sell"
    assert order.legs[0].type == "stop"


def test_translate_order_leaves_legs_none_for_a_simple_order():
    client = MagicMock()
    client.submit_order.return_value = _order(status=AlpacaStatus.NEW)
    order = _broker_with(client).submit_order(_request())
    assert order.legs is None


def test_submit_order_falls_back_to_lookup_on_a_duplicate_client_order_id():
    client = MagicMock()
    client.submit_order.side_effect = _api_error(422, "client_order_id must be unique")
    client.get_order_by_client_id.return_value = _order()
    order = _broker_with(client).submit_order(_request())
    assert order.client_order_id == "s0-20260101-NVDA-abc"


def test_submit_order_raises_if_duplicate_lookup_finds_nothing():
    client = MagicMock()
    client.submit_order.side_effect = _api_error(422, "client_order_id must be unique")
    client.get_order_by_client_id.side_effect = _api_error(404, "not found")
    with pytest.raises(BrokerError, match="duplicate"):
        _broker_with(client).submit_order(_request())


def test_submit_order_translates_rate_limit():
    from semimc.execution.broker import RateLimitError

    client = MagicMock()
    client.submit_order.side_effect = _api_error(429, "too many requests")
    with pytest.raises(RateLimitError):
        _broker_with(client).submit_order(_request())


def test_submit_order_translates_a_wash_trade_shaped_403():
    from semimc.execution.broker import WashTradeError

    client = MagicMock()
    client.submit_order.side_effect = _api_error(403, "potential wash trade detected")
    with pytest.raises(WashTradeError):
        _broker_with(client).submit_order(_request())


def test_submit_order_falls_back_to_generic_broker_error_for_an_unmatched_403():
    client = MagicMock()
    client.submit_order.side_effect = _api_error(403, "account is not authorized to trade")
    with pytest.raises(BrokerError):
        _broker_with(client).submit_order(_request())


def test_get_order_by_client_id_returns_none_on_404():
    client = MagicMock()
    client.get_order_by_client_id.side_effect = _api_error(404, "order not found")
    assert _broker_with(client).get_order_by_client_id("missing") is None


def test_cancel_order_maps_404_to_key_error():
    client = MagicMock()
    client.cancel_order_by_id.side_effect = _api_error(404, "order not found")
    with pytest.raises(KeyError):
        _broker_with(client).cancel_order("missing")


def test_cancel_order_maps_422_to_value_error():
    client = MagicMock()
    client.cancel_order_by_id.side_effect = _api_error(422, "order not cancelable")
    with pytest.raises(ValueError, match="not cancelable"):
        _broker_with(client).cancel_order("already-filled")


def test_list_open_orders_applies_the_fine_grained_status_filter():
    client = MagicMock()
    client.get_orders.return_value = [
        _order(client_order_id="open-1", status=AlpacaStatus.ACCEPTED),
        _order(client_order_id="filled-1", status=AlpacaStatus.FILLED, filled_qty="10"),
    ]
    open_orders = _broker_with(client).list_open_orders()
    assert [o.client_order_id for o in open_orders] == ["open-1"]


def test_list_open_orders_skips_multi_leg_orders():
    client = MagicMock()
    client.get_orders.return_value = [
        _order(
            client_order_id="bracket-1",
            order_class=OrderClass.BRACKET,
            side=None,
            type=None,
        ),
        _order(client_order_id="simple-1", status=AlpacaStatus.NEW),
    ]
    open_orders = _broker_with(client).list_open_orders()
    assert [o.client_order_id for o in open_orders] == ["simple-1"]


# -- status mapping --------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,filled_qty,qty,expected",
    [
        (AlpacaStatus.NEW, "0", "10", "new"),
        (AlpacaStatus.PARTIALLY_FILLED, "5", "10", "partially_filled"),
        (AlpacaStatus.FILLED, "10", "10", "filled"),
        (AlpacaStatus.CANCELED, "0", "10", "canceled"),
        (AlpacaStatus.EXPIRED, "0", "10", "expired"),
        (AlpacaStatus.REJECTED, "0", "10", "rejected"),
        (AlpacaStatus.REPLACED, "0", "10", "canceled"),
        (AlpacaStatus.SUSPENDED, "0", "10", "rejected"),
        (AlpacaStatus.PENDING_NEW, "0", "10", "accepted"),
        (AlpacaStatus.HELD, "0", "10", "accepted"),
        (AlpacaStatus.DONE_FOR_DAY, "10", "10", "filled"),  # fully filled -> filled
        (AlpacaStatus.DONE_FOR_DAY, "4", "10", "expired"),  # partial, day ended -> expired
        (AlpacaStatus.CALCULATED, "10", "10", "filled"),
        (AlpacaStatus.CALCULATED, "0", "10", "expired"),
    ],
)
def test_status_mapping(status, filled_qty, qty, expected):
    client = MagicMock()
    client.submit_order.return_value = _order(status=status, filled_qty=filled_qty, qty=qty)
    order = _broker_with(client).submit_order(_request())
    assert order.status == expected


# -- from_env ------------------------------------------------------------------------


def test_from_env_raises_a_clear_error_when_keys_are_missing(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    # load_dotenv() with no path searches upward from alpaca_broker.py's own location,
    # not from cwd -- chdir alone can't isolate this test from a real .env on disk (this
    # repo now has one). Stub it out so the test only exercises the missing-keys branch.
    monkeypatch.setattr("semimc.execution.alpaca_broker.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match=".env"):
        AlpacaPaperBroker.from_env()


def test_from_env_never_constructs_a_live_client(monkeypatch):
    import inspect

    from alpaca.trading.client import TradingClient

    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    broker = AlpacaPaperBroker.from_env()
    assert isinstance(broker._client, TradingClient)
    # The one and only place `paper=` is set is hardcoded True -- assert via the actual
    # construction line, so this test fails loudly if `from_env` is ever changed to take
    # it as a parameter. Excludes the docstring, which also mentions "paper=True" in prose.
    body_lines = [
        line
        for line in inspect.getsource(AlpacaPaperBroker.from_env).splitlines()
        if "return cls(" in line
    ]
    assert len(body_lines) == 1
    assert "paper=True" in body_lines[0]


def test_from_env_reads_a_named_key_pair_for_a_second_account(monkeypatch):
    """S4 (BRIEF_INTRADAY.md) trades its own Alpaca paper account and must never share
    credentials with s0/s3 -- `from_env` accepts which env vars to read instead of a
    second broker class."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_S4_API_KEY", "s4-key")
    monkeypatch.setenv("ALPACA_S4_SECRET_KEY", "s4-secret")
    broker = AlpacaPaperBroker.from_env("ALPACA_S4_API_KEY", "ALPACA_S4_SECRET_KEY")
    assert broker._client is not None


def test_from_env_error_names_the_specific_missing_vars(monkeypatch):
    monkeypatch.delenv("ALPACA_S4_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_S4_SECRET_KEY", raising=False)
    monkeypatch.setattr("semimc.execution.alpaca_broker.load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="ALPACA_S4_API_KEY/ALPACA_S4_SECRET_KEY"):
        AlpacaPaperBroker.from_env("ALPACA_S4_API_KEY", "ALPACA_S4_SECRET_KEY")
