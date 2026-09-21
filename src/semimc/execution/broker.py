"""The broker interface: the one seam between execution code and any real or fake broker.

Kept deliberately small -- account, positions, asset lookup, clock and calendar, submit,
look up an order by its ``client_order_id``, cancel, and list open orders -- per the
paper-trading brief's own list. A future ``AlpacaPaperBroker`` and :class:`FakeBroker`
(``fake_broker.py``) both implement this :class:`Broker` protocol; nothing above this
layer is allowed to import an Alpaca SDK type directly, so every test above it runs
against ``FakeBroker`` with no network and no real account.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, Protocol

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit", "stop", "stop_limit"]
TimeInForce = Literal["day", "gtc", "ioc", "fok"]
OrderStatus = Literal[
    "new", "accepted", "partially_filled", "filled", "canceled", "rejected", "expired"
]


@dataclass(frozen=True)
class Account:
    account_number: str
    status: str  # e.g. "ACTIVE", "ACCOUNT_BLOCKED"
    equity: float
    cash: float
    buying_power: float


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    side: Literal["long", "short"]
    market_value: float
    avg_entry_price: float


@dataclass(frozen=True)
class Asset:
    symbol: str
    tradable: bool = True
    fractionable: bool = True
    shortable: bool = True
    easy_to_borrow: bool = True


@dataclass(frozen=True)
class Clock:
    timestamp: dt.datetime
    is_open: bool
    next_open: dt.datetime
    next_close: dt.datetime


@dataclass(frozen=True)
class CalendarDay:
    date: dt.date
    open: dt.time
    close: dt.time


@dataclass(frozen=True)
class OrderRequest:
    """What :meth:`Broker.submit_order` is asked to place.

    Built by ``execution/orders.py`` (a later phase) -- never assembled ad hoc, so the
    deterministic ``client_order_id`` convention and the qty/notional exclusivity rule stay
    enforced in one place.
    """

    symbol: str
    side: OrderSide
    type: OrderType
    time_in_force: TimeInForce
    client_order_id: str
    qty: float | None = None
    notional: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    extended_hours: bool = False
    # A real protective stop resting at the broker (S4, BRIEF_INTRADAY.md), not a second
    # order submitted later by a polling process. "oto" (one-triggers-other) attaches
    # exactly a stop-loss leg -- there is no take-profit field here or in
    # intraday/proposals.py::TradeProposal, so this is never Alpaca's OrderClass.BRACKET
    # (which requires both legs). Every existing caller leaves these at their defaults.
    order_class: Literal["simple", "oto"] = "simple"
    stop_loss_price: float | None = None
    stop_loss_limit_price: float | None = None  # None = a plain stop leg; set = stop-limit

    def __post_init__(self) -> None:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("give exactly one of qty or notional")


@dataclass
class Order:
    """A broker's view of a submitted order."""

    id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    time_in_force: TimeInForce
    status: OrderStatus
    qty: float | None
    notional: float | None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    submitted_at: dt.datetime | None = None
    filled_at: dt.datetime | None = None


class BrokerError(Exception):
    """Base class for every error a :class:`Broker` implementation raises."""


class MarketClosedError(BrokerError):
    pass


class AccountBlockedError(BrokerError):
    pass


class AssetNotTradableError(BrokerError):
    pass


class WashTradeError(BrokerError):
    """An opposite-side order in the same symbol is still open (Alpaca: HTTP 403)."""


class RateLimitError(BrokerError):
    pass


class Broker(Protocol):
    """The one interface execution/live/strategy code is allowed to depend on."""

    def get_account(self) -> Account: ...

    def list_positions(self) -> list[Position]: ...

    def get_asset(self, symbol: str) -> Asset: ...

    def get_clock(self) -> Clock: ...

    def get_calendar(self, start: dt.date, end: dt.date) -> list[CalendarDay]: ...

    def submit_order(self, request: OrderRequest) -> Order: ...

    def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...

    def cancel_order(self, order_id: str) -> None: ...

    def list_open_orders(self) -> list[Order]: ...
