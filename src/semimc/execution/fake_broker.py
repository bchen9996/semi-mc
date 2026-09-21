"""An in-memory :class:`~semimc.execution.broker.Broker` with injectable fills and
failures -- the only broker every unit test in this project is allowed to construct
directly. ``AlpacaPaperBroker`` (a later phase) is exercised only by opt-in,
network-marked sandbox integration tests, matching this repo's existing convention of
never touching the network in the default test run.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from .broker import (
    Account,
    AccountBlockedError,
    Asset,
    AssetNotTradableError,
    CalendarDay,
    Clock,
    MarketClosedError,
    Order,
    OrderRequest,
    Position,
    RateLimitError,
    WashTradeError,
)


@dataclass
class FakeBroker:
    """Every injectable failure mode the paper-trading brief names, on plain attributes.

    Parameters
    ----------
    market_open
        ``submit_order`` raises :class:`MarketClosedError` when this is ``False``.
    account_status
        ``submit_order`` raises :class:`AccountBlockedError` when this is not ``"ACTIVE"``.
    wash_trade_symbols
        Submitting an order in one of these symbols raises :class:`WashTradeError` if the
        opposite side is already open in that symbol -- mirroring Alpaca rejecting an
        order that could interact with one already open.
    rate_limit_calls_remaining
        ``submit_order`` raises :class:`RateLimitError` this many times before succeeding.
    timeout_after_success_ids
        Client order ids in this set: the order is recorded exactly as a real submit
        would record it (so :meth:`get_order_by_client_id` finds it afterward), but
        ``submit_order`` itself raises ``TimeoutError`` -- the "timed out after a
        successful submit" case the brief names, which is exactly why a caller must look
        an order up by id before retrying rather than assuming a timeout means nothing
        happened.
    partial_fill_fraction
        Fraction of ``qty``/``notional`` filled per call, when less than 1.0. The real
        paper account fills partially "a random size 10% of the time"; a test picks the
        fraction deterministically instead of leaving it to chance.

    An ``OrderRequest`` with ``order_class="oto"`` (S4's protective stop, Phase 3) fills
    its entry leg exactly like a simple order -- ``submit_order`` never reads
    ``stop_loss_price``/``stop_loss_limit_price`` at all. It deliberately does **not**
    simulate that stop leg ever triggering later: real stop-triggering is Alpaca's own
    server-side job once ``order_class=oto``, and ``AlpacaPaperBroker`` itself is already
    exercised only by opt-in network-marked integration tests, not mirrored here.
    """

    equity: float = 100_000.0
    cash: float = 100_000.0
    account_number: str = "FAKE-PAPER-0001"
    account_status: str = "ACTIVE"
    market_open: bool = True
    assets: dict[str, Asset] = field(default_factory=dict)
    wash_trade_symbols: set[str] = field(default_factory=set)
    rate_limit_calls_remaining: int = 0
    timeout_after_success_ids: set[str] = field(default_factory=set)
    partial_fill_fraction: float = 1.0
    now: dt.datetime = field(default_factory=lambda: dt.datetime(2026, 1, 2, 15, 0))

    _orders: dict[str, Order] = field(default_factory=dict, repr=False, compare=False)
    _positions: dict[str, Position] = field(default_factory=dict, repr=False, compare=False)
    _open_sides: dict[str, set[str]] = field(default_factory=dict, repr=False, compare=False)

    # -- account / positions / assets / clock ------------------------------------------

    def get_account(self) -> Account:
        return Account(
            account_number=self.account_number,
            status=self.account_status,
            equity=self.equity,
            cash=self.cash,
            buying_power=self.cash,
        )

    def list_positions(self) -> list[Position]:
        return [p for p in self._positions.values() if p.qty != 0]

    def get_asset(self, symbol: str) -> Asset:
        return self.assets.get(symbol, Asset(symbol=symbol))

    def get_clock(self) -> Clock:
        return Clock(
            timestamp=self.now, is_open=self.market_open, next_open=self.now, next_close=self.now
        )

    def get_calendar(self, start: dt.date, end: dt.date) -> list[CalendarDay]:
        """Weekdays only -- a ``FakeBroker`` doesn't model holidays or early closes."""
        days: list[CalendarDay] = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                days.append(CalendarDay(date=current, open=dt.time(9, 30), close=dt.time(16, 0)))
            current += dt.timedelta(days=1)
        return days

    # -- orders --------------------------------------------------------------------

    def submit_order(self, request: OrderRequest) -> Order:
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            return existing  # a caller must check this before retrying, not resubmit blind

        if self.rate_limit_calls_remaining > 0:
            self.rate_limit_calls_remaining -= 1
            raise RateLimitError(f"rate limited; {self.rate_limit_calls_remaining} retries left")
        if not self.market_open:
            raise MarketClosedError("market is closed")
        if self.account_status != "ACTIVE":
            raise AccountBlockedError(f"account status is {self.account_status}")
        if not self.get_asset(request.symbol).tradable:
            raise AssetNotTradableError(f"{request.symbol} is not tradable")
        if request.symbol in self.wash_trade_symbols:
            other_side = "sell" if request.side == "buy" else "buy"
            if other_side in self._open_sides.get(request.symbol, set()):
                raise WashTradeError(
                    f"a {request.symbol} order could interact with one already open"
                )

        fraction = min(max(self.partial_fill_fraction, 0.0), 1.0)
        filled_qty = (request.qty or 0.0) * fraction
        status = "filled" if fraction >= 1.0 else "partially_filled" if fraction > 0 else "accepted"

        legs = None
        if request.order_class == "oto":
            # The resting stop-loss leg an "oto" entry attaches (S4, Phase 4) -- a real,
            # separately cancelable order, registered in self._orders like any other, so
            # close/trim's find-and-cancel path is genuinely testable without a real
            # account. Never simulated as triggering on its own -- see the class
            # docstring above.
            stop_leg = Order(
                id=str(uuid.uuid4()),
                client_order_id=f"{request.client_order_id}::stop",
                symbol=request.symbol,
                side="sell" if request.side == "buy" else "buy",
                type="stop",
                time_in_force=request.time_in_force,
                status="new",
                qty=request.qty,
                notional=request.notional,
                submitted_at=self.now,
            )
            self._orders[stop_leg.client_order_id] = stop_leg
            # Open like any other resting order, so a wash_trade_symbols-flagged
            # collision is genuinely simulated: a same-symbol opposite-side close/trim
            # correctly raises WashTradeError unless it's canceled first (real Alpaca
            # exempts only bracket/OCO/trailing-stop, not a plain close/trim order, from
            # this check -- Phase 3's confirmed quote, docs/alpaca_notes.md).
            self._open_sides.setdefault(request.symbol, set()).add(stop_leg.side)
            legs = [stop_leg]

        order = Order(
            id=str(uuid.uuid4()),
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            type=request.type,
            time_in_force=request.time_in_force,
            status=status,
            qty=request.qty,
            notional=request.notional,
            filled_qty=filled_qty,
            filled_avg_price=request.limit_price if filled_qty > 0 else None,
            limit_price=request.limit_price,
            submitted_at=self.now,
            filled_at=self.now if status == "filled" else None,
            legs=legs,
        )
        self._orders[request.client_order_id] = order
        if status != "filled":
            self._open_sides.setdefault(request.symbol, set()).add(request.side)
        self._apply_fill_to_position(order)

        if request.client_order_id in self.timeout_after_success_ids:
            self.timeout_after_success_ids.discard(request.client_order_id)
            raise TimeoutError(
                f"request for client_order_id {request.client_order_id!r} timed out, but "
                "the order was recorded -- look it up by client_order_id, don't resubmit"
            )
        return order

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        return self._orders.get(client_order_id)

    def cancel_order(self, order_id: str) -> None:
        for order in self._orders.values():
            if order.id == order_id:
                if order.status in ("filled", "canceled", "expired"):
                    raise ValueError(f"order {order_id} is already {order.status}")
                order.status = "canceled"
                self._open_sides.get(order.symbol, set()).discard(order.side)
                return
        raise KeyError(f"no order with id {order_id}")

    def list_open_orders(self) -> list[Order]:
        open_states = ("new", "accepted", "partially_filled")
        return [o for o in self._orders.values() if o.status in open_states]

    # -- internal --------------------------------------------------------------------

    def _apply_fill_to_position(self, order: Order) -> None:
        if order.filled_qty <= 0:
            return
        signed = order.filled_qty if order.side == "buy" else -order.filled_qty
        price = order.filled_avg_price or 0.0
        position = self._positions.get(order.symbol)
        new_qty = signed if position is None else position.qty + signed
        self._positions[order.symbol] = Position(
            symbol=order.symbol,
            qty=new_qty,
            side="long" if new_qty >= 0 else "short",
            market_value=new_qty * price,
            avg_entry_price=price,
        )
