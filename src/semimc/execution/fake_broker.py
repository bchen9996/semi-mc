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
    BrokerError,
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
    cancel_delay_lookups
        How many :meth:`get_order_by_client_id` lookups a canceled order still reports as
        open before it reads ``"canceled"``. Alpaca cancels asynchronously: the order sits
        in ``pending_cancel`` (which ``AlpacaPaperBroker`` maps to ``"accepted"``) and
        keeps holding its shares until the cancel completes. ``0`` (the default) cancels
        at once.
    enforce_held_qty
        When ``True``, an order that reduces a position (a sell against a long, a buy
        against a short) for more shares than the position less what other open orders on
        the same side already hold is rejected with :class:`BrokerError`, the way Alpaca
        answers "insufficient qty available for order". A stop still pending cancel holds
        its shares too. Off by default, so every existing test keeps its behavior.
    clock
        When set, :meth:`get_clock` returns it instead of a clock built from
        ``market_open`` and ``now``. The intraday shadow broker carries the real
        exchange clock this way (``cli.py::_intraday_execution_broker``): its fills are
        simulated, whether the market is open is not. ``submit_order`` still reads only
        ``market_open``. Not persisted by ``to_state``.

    An ``OrderRequest`` with ``order_class="oto"`` (S4's protective stop, Phase 3) fills
    its entry leg exactly like a simple order -- ``submit_order`` never reads
    ``stop_loss_price``/``stop_loss_limit_price`` at all. Any order of ``type="stop"`` or
    ``"stop_limit"`` -- whether an OTO entry's attached leg, or a standalone request
    (S4's replacement stop after a trim, Phase 5) -- rests at ``status="new"`` and is
    never simulated as filling or triggering: real stop-triggering is Alpaca's own
    server-side job, and ``AlpacaPaperBroker`` itself is already exercised only by opt-in
    network-marked integration tests, not mirrored here. A test that needs a stop to fire
    calls :meth:`trigger_stop` explicitly.
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
    cancel_delay_lookups: int = 0
    enforce_held_qty: bool = False
    clock: Clock | None = None
    now: dt.datetime = field(default_factory=lambda: dt.datetime(2026, 1, 2, 15, 0))

    _orders: dict[str, Order] = field(default_factory=dict, repr=False, compare=False)
    _positions: dict[str, Position] = field(default_factory=dict, repr=False, compare=False)
    _open_sides: dict[str, set[str]] = field(default_factory=dict, repr=False, compare=False)
    # Order id -> lookups left before a cancel completes (`cancel_delay_lookups`). Not
    # persisted by `to_state`: the shadow broker never delays a cancel.
    _pending_cancels: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

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
        if self.clock is not None:
            return self.clock
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
        if self.enforce_held_qty:
            self._check_held_qty(request)

        if request.type in ("stop", "stop_limit"):
            # A stop (standalone -- S4's own replacement stop, Phase 5 -- or an OTO
            # entry's attached leg, Phase 4) rests untriggered, exactly like real Alpaca:
            # nothing here simulates the trigger price ever being touched. Previously
            # only the OTO leg branch below got this treatment; a standalone stop
            # submitted directly used to fall through to the fill logic meant for
            # market/limit orders and filled immediately -- wrong, and inconsistent with
            # the OTO leg's own "never simulated as triggering" rule for the same order
            # type.
            filled_qty = 0.0
            status = "new"
        else:
            fraction = min(max(self.partial_fill_fraction, 0.0), 1.0)
            filled_qty = (request.qty or 0.0) * fraction
            if fraction >= 1.0:
                status = "filled"
            elif fraction > 0:
                status = "partially_filled"
            else:
                status = "accepted"

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
        order = self._orders.get(client_order_id)
        if order is not None and order.id in self._pending_cancels:
            self._pending_cancels[order.id] -= 1
            if self._pending_cancels[order.id] <= 0:
                del self._pending_cancels[order.id]
                self._finish_cancel(order)
        return order

    def cancel_order(self, order_id: str) -> None:
        for order in self._orders.values():
            if order.id == order_id:
                if order.status in ("filled", "canceled", "expired"):
                    raise ValueError(f"order {order_id} is already {order.status}")
                if order.id in self._pending_cancels:
                    return  # already canceling
                if self.cancel_delay_lookups > 0:
                    order.status = "accepted"  # pending_cancel, as AlpacaPaperBroker maps it
                    self._pending_cancels[order.id] = self.cancel_delay_lookups
                    return
                self._finish_cancel(order)
                return
        raise KeyError(f"no order with id {order_id}")

    def trigger_stop(self, client_order_id: str, price: float) -> Order:
        """Fills a resting stop at ``price`` as if the market had traded through it, and
        applies the fill to the position. Nothing calls this on its own (see the class
        docstring); a test does, to put a stop that fired before a cancel reached it."""
        order = self._orders[client_order_id]
        if order.type not in ("stop", "stop_limit") or order.status not in ("new", "accepted"):
            raise ValueError(f"{client_order_id} is not a resting stop")
        return self.fill_order(client_order_id, price)

    def expire_day_orders(self) -> list[Order]:
        """Expires every working `day` order, as Alpaca does at the close, and returns
        them. Nothing calls this on its own; a test does, to carry a position overnight
        (every S4 stop is a `day` order, so this takes them all)."""
        expired = [order for order in self.list_open_orders() if order.time_in_force == "day"]
        for order in expired:
            self._pending_cancels.pop(order.id, None)
            order.status = "expired"
            self._open_sides.get(order.symbol, set()).discard(order.side)
        return expired

    def fill_order(self, client_order_id: str, price: float, qty: float | None = None) -> Order:
        """Fills ``qty`` (default: all that's left) of a working order at ``price`` and
        applies it to the position: the real paper account fills an order a second or more
        after `submit_order` has already answered (`docs/alpaca_notes.md`). Nothing calls
        this on its own; a test does, after `partial_fill_fraction=0.0` left the order
        working, to put a fill the submit response never saw."""
        order = self._orders[client_order_id]
        if order.status not in ("new", "accepted", "partially_filled") or order.qty is None:
            raise ValueError(f"{client_order_id} is not a working qty order")
        remaining = order.qty - order.filled_qty
        increment = remaining if qty is None else min(qty, remaining)
        total = order.filled_qty + increment
        order.filled_avg_price = (
            (order.filled_avg_price or 0.0) * order.filled_qty + price * increment
        ) / total
        order.filled_qty = total
        order.filled_at = self.now
        if total >= order.qty - 1e-9:
            order.status = "filled"
            self._pending_cancels.pop(order.id, None)
            self._open_sides.get(order.symbol, set()).discard(order.side)
        else:
            order.status = "partially_filled"
        self._apply_fill_to_position(order, increment, price)
        return order

    def list_open_orders(self) -> list[Order]:
        open_states = ("new", "accepted", "partially_filled")
        return [o for o in self._orders.values() if o.status in open_states]

    # -- internal --------------------------------------------------------------------

    def _finish_cancel(self, order: Order) -> None:
        order.status = "canceled"
        self._open_sides.get(order.symbol, set()).discard(order.side)

    def _check_held_qty(self, request: OrderRequest) -> None:
        """`enforce_held_qty`: reject an order that reduces a position by more shares than
        are free, counting every open order on the same side as holding its shares."""
        position = self._positions.get(request.symbol)
        if position is None or position.qty == 0 or request.qty is None:
            return
        reducing = (position.qty > 0 and request.side == "sell") or (
            position.qty < 0 and request.side == "buy"
        )
        if not reducing:
            return
        held = sum(
            (order.qty or 0.0) - order.filled_qty
            for order in self.list_open_orders()
            if order.symbol == request.symbol and order.side == request.side
        )
        available = abs(position.qty) - held
        if request.qty > available + 1e-9:
            raise BrokerError(
                f"insufficient qty available for order (requested: {request.qty:g}, "
                f"available: {max(available, 0.0):g})"
            )

    def _apply_fill_to_position(
        self, order: Order, qty: float | None = None, price: float | None = None
    ) -> None:
        """Applies `qty` shares at `price` (default: everything `order` has filled, at its
        average price) to the position."""
        qty = order.filled_qty if qty is None else qty
        if qty <= 0:
            return
        signed = qty if order.side == "buy" else -qty
        price = (order.filled_avg_price or 0.0) if price is None else price
        position = self._positions.get(order.symbol)
        new_qty = signed if position is None else position.qty + signed
        self._positions[order.symbol] = Position(
            symbol=order.symbol,
            qty=new_qty,
            side="long" if new_qty >= 0 else "short",
            market_value=new_qty * price,
            avg_entry_price=price,
        )

    # -- state (de)serialization (S4's placebo agent, Phase 7) --------------------------

    def to_state(self) -> dict:
        """A JSON-safe snapshot of everything a `FakeBroker` needs to be reconstructed as
        the same broker later -- orders (recursively, including an OTO entry's own resting
        stop leg), positions, open sides, and account identity fields. Datetimes become
        isoformat strings.

        Deliberately excludes `equity`/`cash`: `FakeBroker` never updates either on its own
        (`_apply_fill_to_position` only ever touches `_positions`), so persisting them
        would just persist a number nothing keeps correct. `intraday/placebo.py`'s
        mark-to-market derives both fresh, every call, from the owning ledger's own fill
        history plus a live quote -- the single source of truth, not a second one here.
        """
        return {
            "account_number": self.account_number,
            "account_status": self.account_status,
            "orders": {cid: _order_to_state(order) for cid, order in self._orders.items()},
            "positions": {symbol: _position_to_state(p) for symbol, p in self._positions.items()},
            "open_sides": {symbol: sorted(sides) for symbol, sides in self._open_sides.items()},
        }

    @classmethod
    def from_state(cls, state: dict | None, *, now: dt.datetime) -> FakeBroker:
        """Reconstructs a `FakeBroker` from `to_state()`'s own output. `state=None` (no
        snapshot saved yet -- the very first run) returns a fresh, empty broker, same
        as calling `FakeBroker(now=now)` directly. `now` always comes from the caller,
        never a persisted value -- every job run has its own real wall-clock time.
        """
        if state is None:
            return cls(now=now)
        return cls(
            account_number=state["account_number"],
            account_status=state["account_status"],
            now=now,
            _orders={cid: _order_from_state(o) for cid, o in state["orders"].items()},
            _positions={sym: _position_from_state(p) for sym, p in state["positions"].items()},
            _open_sides={sym: set(sides) for sym, sides in state["open_sides"].items()},
        )


def _order_to_state(order: Order) -> dict:
    return {
        "id": order.id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side,
        "type": order.type,
        "time_in_force": order.time_in_force,
        "status": order.status,
        "qty": order.qty,
        "notional": order.notional,
        "filled_qty": order.filled_qty,
        "filled_avg_price": order.filled_avg_price,
        "limit_price": order.limit_price,
        "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        "legs": [_order_to_state(leg) for leg in order.legs] if order.legs else None,
    }


def _order_from_state(data: dict) -> Order:
    return Order(
        id=data["id"],
        client_order_id=data["client_order_id"],
        symbol=data["symbol"],
        side=data["side"],
        type=data["type"],
        time_in_force=data["time_in_force"],
        status=data["status"],
        qty=data["qty"],
        notional=data["notional"],
        filled_qty=data["filled_qty"],
        filled_avg_price=data["filled_avg_price"],
        limit_price=data["limit_price"],
        submitted_at=(
            dt.datetime.fromisoformat(data["submitted_at"]) if data["submitted_at"] else None
        ),
        filled_at=dt.datetime.fromisoformat(data["filled_at"]) if data["filled_at"] else None,
        legs=[_order_from_state(leg) for leg in data["legs"]] if data["legs"] else None,
    )


def _position_to_state(position: Position) -> dict:
    return {
        "symbol": position.symbol,
        "qty": position.qty,
        "side": position.side,
        "market_value": position.market_value,
        "avg_entry_price": position.avg_entry_price,
    }


def _position_from_state(data: dict) -> Position:
    return Position(**data)
