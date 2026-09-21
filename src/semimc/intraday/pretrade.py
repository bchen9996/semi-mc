"""S4's own sleeve-level pretrade gate (S4, `BRIEF_INTRADAY.md`, Phase 3).

Mirrors `execution/pretrade.py::PretradeContext`/`check_pretrade`'s exact shape, but reads
`config.intraday` instead of `config.paper` -- not generalized into one shared function,
for the same reason `IntradayConfig` is already a top-level sibling rather than nested
inside `PaperConfig`: `PaperConfig` is deliberately scoped to what s0/s3 need (its own
docstring says so), not a generalized schema. `config.intraday.registered_account` is a
single string, not a list, since S4 has exactly one account -- never
`paper.registered_accounts`, which guards a different account entirely.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..config import IntradayConfig
from ..execution.broker import Broker
from ..execution.pretrade import KillSwitchActive, check_kill_switch


@dataclass(frozen=True)
class IntradayPretradeContext:
    """Everything :func:`check_intraday_pretrade` needs, gathered once by the caller so
    every check runs against the same account/clock read rather than each check
    re-querying the broker."""

    now: dt.datetime
    config: IntradayConfig
    broker: Broker


def check_intraday_pretrade(context: IntradayPretradeContext) -> list[str]:
    """Every gate that must clear before submitting *any* S4 order today.

    Returns every failure found, not just the first, same as `execution/pretrade.py::
    check_pretrade`. No configured start/cutoff execution window the way paper's has --
    S4's time-of-day gating is the job schedule itself (Phase 4), not a second,
    overlapping window check here.
    """
    failures: list[str] = []

    try:
        check_kill_switch(context.config.path(context.config.intraday.kill_switch_path))
    except KillSwitchActive as exc:
        failures.append(str(exc))

    if not context.config.intraday.trading_enabled:
        failures.append("intraday trading is not trading_enabled")

    account = context.broker.get_account()
    if account.account_number != context.config.intraday.registered_account:
        # Never interpolate the account number itself into this message -- it is recorded
        # verbatim as a ledger incident that this project deliberately git-tracks, the
        # same rule execution/pretrade.py already follows and for the same reason (found
        # the hard way once already, 2026-09-19: see that module's docstring).
        failures.append(
            "account does not match intraday.registered_account -- refusing to trade "
            "against an unregistered account"
        )
    if account.status != "ACTIVE":
        failures.append(f"account status is {account.status}")

    if not context.broker.get_clock().is_open:
        failures.append("market is closed")

    return failures
