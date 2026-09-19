"""Pretrade checks and the kill switch: the last gate before an :class:`~semimc.execution.
broker.OrderRequest` reaches a broker.

A later phase's ``live/execute.py`` job must call :func:`check_pretrade` once per sleeve
per run and refuse to submit anything if it returns any failure, then
:func:`check_order_tradable` per order. Every check here is read-only against the broker
(account, clock, asset) plus the config and a plain sentinel file -- nothing here ever
calls ``submit_order``, so it is exercised the same way the rest of this project's
execution code is: against :class:`~semimc.execution.fake_broker.FakeBroker`, no network.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from .broker import Broker, OrderRequest


class KillSwitchActive(Exception):
    """Raised by :func:`check_kill_switch` -- trading is halted until the sentinel file is
    removed."""


def check_kill_switch(path: Path) -> None:
    """A plain sentinel file: its mere existence halts all trading.

    No infrastructure, no new dependency -- creating or deleting
    ``config.paper.kill_switch_path`` is the entire interface, deliberately simple enough
    to use under stress (a bad fill, a runaway loop, anything that needs trading stopped
    *now* by whoever is watching, not by a config edit and a redeploy).
    """
    if path.exists():
        raise KillSwitchActive(f"kill switch active at {path}; remove it to resume trading")


@dataclass(frozen=True)
class PretradeContext:
    """Everything :func:`check_pretrade` needs for one sleeve's decision, gathered once by
    the caller so every check runs against the same account/clock read rather than each
    check re-querying the broker."""

    sleeve: str
    now: dt.datetime
    config: Config
    broker: Broker


def check_pretrade(context: PretradeContext) -> list[str]:
    """Every gate that must clear before submitting *any* order for this sleeve today.

    Returns every failure found, not just the first, so one log line or one incident
    record can say everything that is wrong at once rather than requiring a fix-and-rerun
    loop to discover the second problem.
    """
    failures: list[str] = []

    try:
        check_kill_switch(context.config.path(context.config.paper.kill_switch_path))
    except KillSwitchActive as exc:
        failures.append(str(exc))

    sleeve_config = context.config.paper.sleeves.get(context.sleeve)
    if sleeve_config is None or not sleeve_config.trading_enabled:
        failures.append(f"sleeve {context.sleeve!r} is not trading_enabled")

    account = context.broker.get_account()
    if account.account_number not in context.config.paper.registered_accounts:
        failures.append(
            f"account {account.account_number!r} is not in paper.registered_accounts -- "
            "refusing to trade against an unregistered account"
        )
    if account.status != "ACTIVE":
        failures.append(f"account status is {account.status}")

    if not context.broker.get_clock().is_open:
        failures.append("market is closed")

    execution = context.config.paper.execution
    current_time = context.now.time()
    if not (execution.start_time <= current_time <= execution.cutoff_time):
        failures.append(
            f"{current_time} is outside the execution window "
            f"{execution.start_time}-{execution.cutoff_time} ({execution.timezone}); "
            "`now` must already be localized to that timezone"
        )

    return failures


def check_order_tradable(order: OrderRequest, broker: Broker) -> str | None:
    """Per-order asset check -- run once per order, after :func:`check_pretrade` has
    cleared for the sleeve as a whole."""
    asset = broker.get_asset(order.symbol)
    if not asset.tradable:
        return f"{order.symbol} is not tradable"
    return None
