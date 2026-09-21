"""A real :class:`~semimc.execution.broker.Broker` implementation against Alpaca's actual
paper-trading REST API via ``alpaca-py``.

Always paper -- ``TradingClient(paper=True)`` is hardcoded in :meth:`AlpacaPaperBroker.
from_env`, never a parameter, so no code path here can construct a live-trading client.

What gets translated 1:1, and what needs help, was verified directly against the installed
``alpaca-py`` 0.44.0 models rather than assumed:

- **Numeric fields are strings on the wire.** ``Position.qty``/``.market_value``/
  ``.avg_entry_price``, ``Order.qty``/``.filled_qty``/``.filled_avg_price``/``.limit_price``,
  and ``TradeAccount.equity``/``.cash``/``.buying_power`` are all ``str`` or
  ``Optional[str]`` on the alpaca-py models. :func:`_to_float`/:func:`_optional_float`
  coerce every one; a missing ``Account`` money field raises (``execution/orders.py``
  sizes off it), a missing ``Position.market_value`` defaults to ``0.0`` (it only feeds
  ledger display, never sizing).
- **``Order.status`` has 18 values, this Protocol's has 7.** :func:`_map_order_status`
  buckets them; ``done_for_day``/``calculated`` are disambiguated by the order's own
  ``filled_qty`` rather than a fixed guess, since either can mean "fully filled" or
  "ended without a full fill" depending on the specific order.
- **``Order.side``/``.type`` are ``None`` for a multi-leg order** (``order_class`` other
  than ``simple`` -- a bracket/OCO/OTO order, e.g. one placed by hand on the dashboard).
  :meth:`AlpacaPaperBroker.list_open_orders` skips these rather than crash translating
  one. As of Phase 3 (S4, ``BRIEF_INTRADAY.md``), :meth:`submit_order` *does* build one
  kind itself -- ``OrderClass.OTO`` with a ``stop_loss`` leg only, never
  ``OrderClass.BRACKET`` (that needs a take-profit leg too, and nothing upstream of this
  ever supplies one). ``list_open_orders``'s skip is deliberately left as-is rather than
  taught to translate S4's own OTO parents: S4 tracks its own open orders via
  ``IntradayLedger.open_orders()`` instead, so this filter changing behavior for s0/s3
  (which never submits a non-simple order) would be a real-behavior change with nothing to
  gain from it.
- **``Calendar.open``/``.close`` are already naive, exchange-local wall-clock times** --
  ``alpaca.trading.models.Calendar.__init__`` builds them with
  ``datetime.strptime(date + " " + "09:30", "%Y-%m-%d %H:%M")`` directly off the raw API
  string, no timezone attached. ``.time()`` extraction needs no conversion.
- **``Clock``'s four timestamps are documented as eastern time**, aware or naive depending
  on what the API actually sends (not confirmable without a live call) -- stripped to
  naive unconditionally in :meth:`AlpacaPaperBroker.get_clock` to match
  :class:`~semimc.execution.fake_broker.FakeBroker`'s convention. ``Order.submitted_at``/
  ``.filled_at`` are left exactly as Alpaca returns them (aware UTC) -- audit-trail
  timestamps, where an unambiguous absolute time matters more than matching the fake
  clock's simplified convention.
- **A duplicate ``client_order_id`` is not idempotent at the real API -- it is rejected**
  (HTTP 422, ``"client_order_id must be unique"``), unlike
  :class:`~semimc.execution.fake_broker.FakeBroker`, which returns the original order
  directly. :meth:`AlpacaPaperBroker.submit_order` catches that specific rejection and
  falls back to :meth:`get_order_by_client_id`, so a retried submit still resolves to the
  one order the decision produced -- see ``execution/orders.py``'s ``client_order_id``
  docstring.
- **No proactive pre-submit checks** (market open / account status / asset tradable) --
  unlike ``FakeBroker``, which has no real server and must simulate rejections itself,
  this broker lets Alpaca's own server decide and translates what comes back.
  ``execution/pretrade.py`` is the one real gate in the intended execution path; a caller
  that bypasses it gets Alpaca's actual behavior, including that a day order submitted
  while the market is closed is typically queued, not rejected -- so
  ``MarketClosedError`` mostly cannot be reconstructed from a submit error at all.
- **``get_asset`` can 404 on a genuinely unknown symbol**, unlike ``FakeBroker``, which
  defaults an unknown symbol to fully tradable. Nothing calls this with an untrusted
  symbol today.
- **Canceling an already-canceled order succeeds silently at the real API** (confirmed
  against a live paper account, 2026-09-18) -- it does not raise 422 the way
  :class:`~semimc.execution.fake_broker.FakeBroker` raises ``ValueError`` for the same
  case. :meth:`cancel_order` does not special-case this to force parity: nothing in this
  project relies on a re-cancel raising, so real Alpaca's more forgiving (idempotent)
  behavior just passes through. Still unconfirmed: whether canceling a *filled* or
  *expired* order raises 422 the way ``FakeBroker`` assumes -- only the already-canceled
  case has been observed directly.

Still unverified against a real account (see ``docs/alpaca_notes.md``): the exact
wash-trade rejection wording (:func:`_translate_submit_error`'s 403 branch is a
best-effort guess), and everything else in that doc's "still to verify" section.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, QueryOrderStatus
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
)
from dotenv import load_dotenv

from .broker import (
    Account,
    Asset,
    BrokerError,
    CalendarDay,
    Clock,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    RateLimitError,
    WashTradeError,
)

_ORDER_REQUEST_CLASSES: dict[OrderType, type] = {
    "market": MarketOrderRequest,
    "limit": LimitOrderRequest,
    "stop": StopOrderRequest,
    "stop_limit": StopLimitOrderRequest,
}

# Direct matches first; everything still being worked (a future poll resolves it) maps to
# "accepted"; unambiguous terminal states map to their closest equivalent.
# "done_for_day"/"calculated" are handled separately in _map_order_status.
_STATUS_DIRECT: dict[str, OrderStatus] = {
    "new": "new",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "expired": "expired",
    "accepted": "accepted",
    "rejected": "rejected",
    "replaced": "canceled",
    "suspended": "rejected",
    "pending_new": "accepted",
    "accepted_for_bidding": "accepted",
    "pending_review": "accepted",
    "held": "accepted",
    "pending_cancel": "accepted",
    "pending_replace": "accepted",
    "stopped": "accepted",
}
_TERMINAL_AMBIGUOUS = {"done_for_day", "calculated"}
_OPEN_STATUSES = ("new", "accepted", "partially_filled")


def _to_float(value: str | float | None, *, field: str, allow_none: bool = False) -> float:
    if value is None:
        if allow_none:
            return 0.0
        raise BrokerError(f"Alpaca returned no value for {field!r}")
    return float(value)


def _optional_float(value: str | float | None, *, field: str) -> float | None:
    return None if value is None else _to_float(value, field=field)


def _order_qty(order: AlpacaOrder) -> float:
    """The quantity a fill-completeness check compares against. A notional order has no
    share-count target to compare to without a price, so any fill on one counts as full."""
    qty = _optional_float(order.qty, field="qty")
    if qty is not None:
        return qty
    return _to_float(order.filled_qty, field="filled_qty", allow_none=True)


def _map_order_status(order: AlpacaOrder) -> OrderStatus:
    raw = order.status.value
    if raw in _TERMINAL_AMBIGUOUS:
        filled = _to_float(order.filled_qty, field="filled_qty", allow_none=True)
        return "filled" if filled > 0 and filled >= _order_qty(order) else "expired"
    return _STATUS_DIRECT.get(raw, "accepted")  # an unmodeled future status: treat as open


def _safe_enum_value(value: Any, allowed: set[str], default: str) -> str:
    raw = getattr(value, "value", value)
    return raw if raw in allowed else default


def _parse_api_error(exc: APIError) -> tuple[int | None, str]:
    """Defensive status/message extraction -- ``APIError.message``/``.code`` parse the raw
    response body as JSON and can themselves raise if it isn't (a proxy error, an outage),
    which must never mask the original failure with a second traceback."""
    try:
        status_code = exc.status_code
    except Exception:  # noqa: BLE001 - never let error parsing hide the real error
        status_code = None
    try:
        message = exc.message
    except Exception:  # noqa: BLE001
        message = str(exc)
    return status_code, message


def _translate_submit_error(exc: APIError) -> BrokerError:
    status_code, message = _parse_api_error(exc)
    if status_code == 429:
        return RateLimitError(message)
    if status_code == 403 and "wash" in message.lower():
        return WashTradeError(message)
    return BrokerError(message)


def _is_duplicate_client_order_id(exc: APIError) -> bool:
    status_code, message = _parse_api_error(exc)
    return status_code == 422 and "client_order_id" in message.lower()


class AlpacaPaperBroker:
    """The :class:`~semimc.execution.broker.Broker` Protocol against Alpaca's real
    paper-trading API. See the module docstring for what's translated, coerced, and
    approximated."""

    def __init__(self, client: TradingClient) -> None:
        self._client = client

    @classmethod
    def from_env(
        cls, key_env: str = "ALPACA_API_KEY", secret_env: str = "ALPACA_SECRET_KEY"
    ) -> AlpacaPaperBroker:
        """Load a key pair from ``.env`` (or the real environment) and build a paper-only
        client. ``paper=True`` is hardcoded here, not a parameter -- there is no way to
        call this and get a live-trading client.

        ``key_env``/``secret_env`` default to s0/s3's pair, unchanged from before this
        parameter existed. S4 (``BRIEF_INTRADAY.md``) trades its own, separate Alpaca
        paper account and must never share credentials with s0/s3 -- it calls this with
        ``key_env="ALPACA_S4_API_KEY", secret_env="ALPACA_S4_SECRET_KEY"`` instead.
        """
        load_dotenv()
        api_key = os.environ.get(key_env)
        secret_key = os.environ.get(secret_env)
        if not api_key or not secret_key:
            raise RuntimeError(
                f"{key_env}/{secret_env} not set. Copy .env.example to .env and fill in "
                "your paper account's keys (app.alpaca.markets/paper/dashboard)."
            )
        return cls(TradingClient(api_key=api_key, secret_key=secret_key, paper=True))

    # -- account / positions / assets / clock ------------------------------------------

    def get_account(self) -> Account:
        account = self._client.get_account()
        return Account(
            account_number=account.account_number,
            status=account.status.value,
            equity=_to_float(account.equity, field="equity"),
            cash=_to_float(account.cash, field="cash"),
            buying_power=_to_float(account.buying_power, field="buying_power"),
        )

    def list_positions(self) -> list[Position]:
        return [
            Position(
                symbol=p.symbol,
                qty=_to_float(p.qty, field="qty"),
                side=p.side.value,
                market_value=_to_float(p.market_value, field="market_value", allow_none=True),
                avg_entry_price=_to_float(p.avg_entry_price, field="avg_entry_price"),
            )
            for p in self._client.get_all_positions()
        ]

    def get_asset(self, symbol: str) -> Asset:
        asset = self._client.get_asset(symbol)
        return Asset(
            symbol=asset.symbol,
            tradable=bool(asset.tradable),
            fractionable=bool(asset.fractionable),
            shortable=bool(asset.shortable),
            easy_to_borrow=bool(asset.easy_to_borrow),
        )

    def get_clock(self) -> Clock:
        clock = self._client.get_clock()
        return Clock(
            timestamp=clock.timestamp.replace(tzinfo=None),
            is_open=clock.is_open,
            next_open=clock.next_open.replace(tzinfo=None),
            next_close=clock.next_close.replace(tzinfo=None),
        )

    def get_calendar(self, start: dt.date, end: dt.date) -> list[CalendarDay]:
        days = self._client.get_calendar(filters=GetCalendarRequest(start=start, end=end))
        return [
            CalendarDay(date=day.date, open=day.open.time(), close=day.close.time()) for day in days
        ]

    # -- orders --------------------------------------------------------------------

    def submit_order(self, request: OrderRequest) -> Order:
        request_cls = _ORDER_REQUEST_CLASSES[request.type]
        kwargs: dict[str, Any] = {
            "symbol": request.symbol,
            "side": request.side,
            "time_in_force": request.time_in_force,
            "client_order_id": request.client_order_id,
            "extended_hours": request.extended_hours,
        }
        if request.qty is not None:
            kwargs["qty"] = request.qty
        else:
            kwargs["notional"] = request.notional
        if request.type in ("limit", "stop_limit"):
            kwargs["limit_price"] = request.limit_price
        if request.type in ("stop", "stop_limit"):
            kwargs["stop_price"] = request.stop_price
        if request.order_class == "oto":
            kwargs["order_class"] = OrderClass.OTO
            kwargs["stop_loss"] = StopLossRequest(
                stop_price=request.stop_loss_price, limit_price=request.stop_loss_limit_price
            )

        try:
            order = self._client.submit_order(order_data=request_cls(**kwargs))
        except APIError as exc:
            if _is_duplicate_client_order_id(exc):
                existing = self.get_order_by_client_id(request.client_order_id)
                if existing is not None:
                    return existing
                raise BrokerError(
                    f"Alpaca rejected {request.client_order_id!r} as a duplicate, but no "
                    "order with that id could be found"
                ) from exc
            raise _translate_submit_error(exc) from exc
        return self._translate_order(order)

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        try:
            order = self._client.get_order_by_client_id(client_order_id)
        except APIError as exc:
            status_code, message = _parse_api_error(exc)
            if status_code == 404:
                return None
            raise BrokerError(message) from exc
        return self._translate_order(order)

    def cancel_order(self, order_id: str) -> None:
        try:
            self._client.cancel_order_by_id(order_id)
        except APIError as exc:
            status_code, message = _parse_api_error(exc)
            if status_code == 404:
                raise KeyError(f"no order with id {order_id}") from exc
            if status_code == 422:
                raise ValueError(message) from exc
            raise BrokerError(message) from exc

    def list_open_orders(self) -> list[Order]:
        orders = self._client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        translated = [
            self._translate_order(order)
            for order in orders
            if order.order_class == OrderClass.SIMPLE  # multi-leg: side/type are per-leg
        ]
        return [o for o in translated if o.status in _OPEN_STATUSES]

    # -- internal --------------------------------------------------------------------

    def _translate_order(self, order: AlpacaOrder) -> Order:
        return Order(
            id=str(order.id),
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=_safe_enum_value(order.side, {"buy", "sell"}, "buy"),
            type=_safe_enum_value(order.type, {"market", "limit", "stop", "stop_limit"}, "limit"),
            time_in_force=_safe_enum_value(
                order.time_in_force, {"day", "gtc", "ioc", "fok"}, "day"
            ),
            status=_map_order_status(order),
            qty=_optional_float(order.qty, field="qty"),
            notional=_optional_float(order.notional, field="notional"),
            filled_qty=_to_float(order.filled_qty, field="filled_qty", allow_none=True),
            filled_avg_price=_optional_float(order.filled_avg_price, field="filled_avg_price"),
            limit_price=_optional_float(order.limit_price, field="limit_price"),
            submitted_at=order.submitted_at,
            filled_at=order.filled_at,
        )
