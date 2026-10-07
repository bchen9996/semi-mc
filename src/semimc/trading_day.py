"""Which trading session a moment belongs to.

A session starts with pre-market at 04:00 New York time, not at midnight: anything after
the close and before 04:00 belongs to the session that just ended. Dating by the clock's
own date used to go wrong two ways. A job's default `--as-of` was `pd.Timestamp.now()`,
which on GitHub's runners is UTC and already tomorrow from 20:00 New York time, so a late
cron copy or an evening test run filed itself under the next day and could take that
day's run-once slot (found 2026-09-30: test runs at 23:52 Central, 00:52 New York, wrote
rows dated 2026-10-01 -- which is why the rollover is 04:00, not midnight). And the
intraday ledger dated incidents by their UTC date, so a late job's incident counted
against the next day (found 2026-10-06: GitHub's late copy of carry ran at 19:57 Central
and its error landed on the next day's readiness row). Both now use :func:`trading_day`.
"""

from __future__ import annotations

import pandas as pd

EXCHANGE_TZ = "America/New_York"
ROLLOVER = pd.Timedelta(hours=4)


def trading_day(now: pd.Timestamp | str | None = None) -> pd.Timestamp:
    """The session date of `now` as a naive midnight, the same shape an explicit
    `--as-of` parses to. `now` (aware, or naive read as UTC; an ISO string is fine)
    defaults to the clock."""
    stamp = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    local = stamp.tz_convert(EXCHANGE_TZ) - ROLLOVER
    return local.normalize().tz_localize(None)
