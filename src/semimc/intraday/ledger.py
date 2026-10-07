"""S4's own SQLite ledger: briefs, watchlist candidates, every trade proposal (accepted
and rejected), orders, fills, snapshots, reconciliations, reflections, per-call model-cost
logs, and headlines already seen (``BRIEF_INTRADAY.md``).

Deliberately a **separate file** from ``live/ledger.py``'s ``ledger/paper.sqlite3``, and a
separate class rather than new tables bolted onto :class:`~semimc.live.ledger.Ledger`: S4
writes roughly every 20 minutes all trading day, versus s0/s3's monthly-decide-plus-
half-hourly-mark cadence, and sharing one file would mean every S4 cycle's git commit
contends with s0/s3's on the exact same file -- the push-race class of bug Phase 8 of
paper trading already had to fix once, at a much lower write frequency than S4's. A
separate file removes the contention instead of stretching that retry logic to cover it.

Also deliberately scoped to exactly one sleeve (there is only ever one S4): unlike
``live/ledger.py``'s tables, none of these carry a ``sleeve`` column -- it would be a
column whose value is always the same literal string, which is dead weight, not a real
degree of freedom, in a file that only ever holds S4's own data.

Same recipe as ``live/ledger.py`` (see that module's docstring): stdlib ``sqlite3``, one
:class:`IntradayLedger` per file, every public method is exactly one INSERT or one SELECT,
JSON columns via ``_dumps``/``_now()``. Nothing above this layer is allowed to write SQL
directly.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from ..trading_day import trading_day

_SCHEMA = """
CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    thesis TEXT NOT NULL,
    entry_trigger TEXT,
    invalidation TEXT,
    size_band TEXT,
    horizon TEXT,
    expected_move REAL,
    expected_cost_bps REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    cycle TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    playbook_version INTEGER,
    options_pricing_json TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    proposal_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    notional REAL,
    limit_price REAL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    submitted_at TEXT,
    updated_at TEXT NOT NULL,
    leg TEXT NOT NULL DEFAULT 'entry',
    tranche_index INTEGER NOT NULL DEFAULT 1,
    tranche_total INTEGER NOT NULL DEFAULT 1,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL UNIQUE,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    discrepancies_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    client_order_id TEXT,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    cycle TEXT NOT NULL,
    role TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS headlines_seen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    source TEXT NOT NULL,
    headline_hash TEXT NOT NULL,
    symbols TEXT,
    headline TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (source, headline_hash)
);

CREATE TABLE IF NOT EXISTS session_state (
    as_of TEXT PRIMARY KEY,
    equity_at_open REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_broker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playbook_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    text TEXT NOT NULL,
    source_proposal_path TEXT NOT NULL,
    adopted_at TEXT,
    review_date TEXT,
    resolved_at TEXT,
    resolution_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playbook_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    entry_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    cycle TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    gap_seconds REAL
);
"""

_OPEN_ORDER_STATUSES = ("new", "accepted", "partially_filled")

# `CREATE TABLE IF NOT EXISTS` (in `_SCHEMA` above) never adds a column to a table that
# already exists on disk -- the real, long-lived `ledger/intraday.sqlite3` predates
# `proposals.playbook_version` (added in Phase 10, well after that table was first
# created), so every `IntradayLedger.__init__` since then silently kept opening a
# `proposals` table one column short of what `record_proposal` inserts into. Never caught
# in tests (a fresh `tmp_path` ledger always gets the current, complete `_SCHEMA`) or in
# earlier real runs (Stage 1's first `run_cycle` calls that reached `record_proposal` for
# real only started 2026-09-25) -- found 2026-09-26 from a real `OperationalError: table
# proposals has no column named playbook_version`. Any future column added to a table that
# already shipped needs an entry here, not just in `_SCHEMA`.
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "proposals": [("playbook_version", "INTEGER"), ("options_pricing_json", "TEXT")],
    "orders": [("exit_reason", "TEXT")],
}


def _migrate_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp | dt.datetime | dt.date):
        return str(value)
    if hasattr(value, "item"):  # numpy scalars
        return value.item()
    return str(value)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, default=_json_default, sort_keys=True)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _unpack_proposal_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shared by `get_proposal`/`proposals_for` -- both unpack `payload_json` into
    `payload` and, since Phase 12b, `options_pricing_json` into `options_pricing` (`None`
    when the column is `NULL`, either an old row from before this column existed or a
    proposal that was never priced as an option)."""
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    pricing_json = data.pop("options_pricing_json")
    data["options_pricing"] = json.loads(pricing_json) if pricing_json is not None else None
    return data


class IntradayLedger:
    """A SQLite-backed record of everything S4 does. See module docstring for why this is
    a separate file/class from :class:`~semimc.live.ledger.Ledger` rather than an
    extension of it."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        _migrate_missing_columns(self._conn)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> IntradayLedger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- briefs -------------------------------------------------------------------------

    def record_brief(self, as_of: pd.Timestamp, content: dict[str, Any]) -> int:
        cursor = self._conn.execute(
            "INSERT INTO briefs (as_of, content_json, created_at) VALUES (?, ?, ?)",
            (str(pd.Timestamp(as_of)), _dumps(content), _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def latest_brief(self) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM briefs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        data = dict(row)
        data["content"] = json.loads(data.pop("content_json"))
        return data

    def briefs_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        """One trading day's research briefs -- `intraday/readiness.py` needs "did
        research run on day X" for every day in a window, which `latest_brief` alone
        can't answer."""
        rows = self._conn.execute(
            "SELECT * FROM briefs WHERE as_of = ? ORDER BY id", (str(pd.Timestamp(as_of)),)
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["content"] = json.loads(data.pop("content_json"))
            out.append(data)
        return out

    # -- watchlist ------------------------------------------------------------------------

    def record_watchlist_item(
        self,
        as_of: pd.Timestamp,
        symbol: str,
        thesis: str,
        entry_trigger: str | None = None,
        invalidation: str | None = None,
        size_band: str | None = None,
        horizon: str | None = None,
        expected_move: float | None = None,
        expected_cost_bps: float | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO watchlist
                (as_of, symbol, thesis, entry_trigger, invalidation, size_band, horizon,
                 expected_move, expected_cost_bps, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(pd.Timestamp(as_of)),
                symbol,
                thesis,
                entry_trigger,
                invalidation,
                size_band,
                horizon,
                expected_move,
                expected_cost_bps,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def watchlist_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM watchlist WHERE as_of = ? ORDER BY id", (str(pd.Timestamp(as_of)),)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- proposals ------------------------------------------------------------------------

    def record_proposal(
        self,
        as_of: pd.Timestamp,
        cycle: str,
        symbol: str,
        action: str,
        status: str,
        payload: dict[str, Any],
        reason: str | None = None,
        playbook_version: int | None = None,
        options_pricing: dict[str, Any] | None = None,
    ) -> int:
        """Every proposal, accepted or rejected -- a plain append, per the brief's own
        "blotter: every proposal including rejected ones." ``status`` carries the
        accept/reject outcome; ``reason`` is populated on a rejection.

        ``playbook_version`` (Phase 10) and ``options_pricing`` (Phase 12b) are each their
        own column, deliberately not folded into ``payload`` -- ``payload`` is exactly
        ``TradeProposal.model_dump()``, and more than one code path (`run_cycle`'s own
        tranche continuation, `intraday/reflect.py`) reconstructs a `TradeProposal` via
        `TradeProposal(**payload)`; that schema is `extra="forbid"`, so an extra key would
        raise a `ValidationError` everywhere it's read back, not just where it was written.
        ``options_pricing`` is the quote/greeks snapshot `run_cycle` priced this option
        proposal with -- `None` for every equity proposal, and for an option proposal
        whose action never reaches pricing (`trim`/`close`/`hold`/`no_trade`) or whose
        quote fetch came back empty.
        """
        cursor = self._conn.execute(
            """
            INSERT INTO proposals (as_of, cycle, symbol, action, status, reason,
                                    payload_json, created_at, playbook_version,
                                    options_pricing_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(pd.Timestamp(as_of)),
                cycle,
                symbol,
                action,
                status,
                reason,
                _dumps(payload),
                _now(),
                playbook_version,
                _dumps(options_pricing) if options_pricing is not None else None,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        """One proposal by its row id (S4, Phase 5) -- what `intraday/monitor.py` uses to
        recover the `invalidation_price`/`time_stop_minutes` an entry order was opened
        under, via the order row's own `proposal_id`."""
        row = self._conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            return None
        return _unpack_proposal_row(row)

    def proposals_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE as_of = ? ORDER BY id", (str(pd.Timestamp(as_of)),)
        ).fetchall()
        return [_unpack_proposal_row(row) for row in rows]

    # -- orders / fills -------------------------------------------------------------------

    def record_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        status: str,
        qty: float | None = None,
        notional: float | None = None,
        limit_price: float | None = None,
        broker_order_id: str | None = None,
        submitted_at: dt.datetime | None = None,
        proposal_id: int | None = None,
        leg: str = "entry",
        tranche_index: int = 1,
        tranche_total: int = 1,
        exit_reason: str | None = None,
    ) -> int:
        """Insert or update one order by its ``client_order_id`` -- a resubmission of the
        same id (idempotent at the broker) updates status in place, same reasoning as
        ``live/ledger.py``'s ``record_order``.

        ``leg`` distinguishes an entry order's row from its resting stop-loss leg's own
        row (S4, Phase 4) -- ``"entry"`` (the default, every order before Phase 4) or
        ``"stop"``. :func:`~semimc.intraday.orders.submit_order_with_fault_isolation`
        records a successful ``"oto"`` submission's leg this way automatically.

        ``tranche_index``/``tranche_total`` record which piece of a split entry this
        order is (S4, Phase 5) -- ``1``/``1`` (the default, every order before Phase 5,
        and every non-``open``/``add`` order) means "not split." See
        :meth:`pending_tranche_for`.

        ``exit_reason`` (2026-10-03) is why an exit was sent (``"invalidation"``,
        ``"time_stop"``, ``"trader_close"``, ``"trader_trim"``, ``"carry"``), kept so the
        reflection can still be written when the fill arrives after submission
        (`intraday/orders.py::sync_open_orders`). Set on insert only.
        """
        self._conn.execute(
            """
            INSERT INTO orders
                (client_order_id, proposal_id, symbol, side, qty, notional, limit_price,
                 status, broker_order_id, submitted_at, updated_at, leg, tranche_index,
                 tranche_total, exit_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                status = excluded.status,
                broker_order_id = excluded.broker_order_id,
                updated_at = excluded.updated_at
            """,
            (
                client_order_id,
                proposal_id,
                symbol,
                side,
                qty,
                notional,
                limit_price,
                status,
                broker_order_id,
                submitted_at.isoformat() if submitted_at else None,
                _now(),
                leg,
                tranche_index,
                tranche_total,
                exit_reason,
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return int(row["id"])

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def open_orders(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(_OPEN_ORDER_STATUSES))
        rows = self._conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders})", _OPEN_ORDER_STATUSES
        ).fetchall()
        return [dict(r) for r in rows]

    def open_orders_for(self, symbol: str, leg: str | None = None) -> list[dict[str, Any]]:
        """Every still-open order recorded for ``symbol``, optionally only one ``leg``,
        oldest first."""
        placeholders = ",".join("?" * len(_OPEN_ORDER_STATUSES))
        leg_clause = "" if leg is None else "AND leg = ?"
        rows = self._conn.execute(
            f"""
            SELECT * FROM orders
            WHERE symbol = ? {leg_clause} AND status IN ({placeholders})
            ORDER BY id
            """,
            (symbol, *([] if leg is None else [leg]), *_OPEN_ORDER_STATUSES),
        ).fetchall()
        return [dict(row) for row in rows]

    def unreflected_exits(self) -> list[dict[str, Any]]:
        """Exits and stop legs that are done, filled at least partly, and have no
        reflection yet, oldest first: what `intraday/orders.py::sync_open_orders` writes
        reflections for once a fill arrives after submission. Only exits that carry an
        ``exit_reason`` count, so exits recorded before that column existed (2026-10-03)
        are never reflected a second time. A stop leg needs none: it only ever fires at
        the invalidation price."""
        placeholders = ",".join("?" * len(_OPEN_ORDER_STATUSES))
        rows = self._conn.execute(
            f"""
            SELECT * FROM orders AS o
            WHERE (o.leg = 'stop' OR (o.leg = 'exit' AND o.exit_reason IS NOT NULL))
              AND o.status NOT IN ({placeholders})
              AND EXISTS (SELECT 1 FROM fills AS f WHERE f.client_order_id = o.client_order_id)
              AND NOT EXISTS (
                  SELECT 1 FROM reflections AS r WHERE r.client_order_id = o.client_order_id
              )
            ORDER BY o.id
            """,
            _OPEN_ORDER_STATUSES,
        ).fetchall()
        return [dict(row) for row in rows]

    def open_stop_legs_for(self, symbol: str) -> list[dict[str, Any]]:
        """Every still-open stop-loss leg recorded for ``symbol``, oldest first -- what
        `intraday/orders.py::cancel_resting_stops` cancels before a close/trim.

        A list since 2026-10-02. This used to return only the most recent leg, on the
        Phase 4 assumption that a symbol never has more than one resting stop. Phase 5's
        tranche-splitting broke that: each tranche's entry carries its own stop. The first
        two-tranche trade (NVDA, 3 + 3 shares) closed with only the second tranche's stop
        canceled, leaving a 3-share sell stop resting with nothing behind it -- harmless
        on the shadow broker, which never triggers stops, but on a real account it would
        have opened a short had NVDA traded down to it.
        """
        return self.open_orders_for(symbol, leg="stop")

    def pending_tranche_for(self, symbol: str) -> dict[str, Any] | None:
        """The most recently filled entry order for ``symbol`` (whichever tranche that
        was), if its own ``tranche_index < tranche_total`` -- i.e. more tranches remain
        to submit (S4, Phase 5). ``None`` if fully tranched in, never split, or there is
        no filled entry at all.

        Deliberately finds the most recent filled entry *first*, then checks its own
        tranche fields -- filtering on ``tranche_index < tranche_total`` inside the SQL
        `WHERE` clause would wrongly keep matching tranche 1's own row forever (its
        `tranche_index`/`tranche_total` never change after tranche 1 fills), even once a
        later tranche has already been submitted and itself reached `tranche_index ==
        tranche_total`.

        Singular, on the assumption that only one entry can be mid-tranche for a symbol
        at once (a symbol's resting stops, by contrast, are a list:
        :meth:`open_stop_legs_for`).
        """
        row = self._conn.execute(
            """
            SELECT * FROM orders
            WHERE symbol = ? AND leg = 'entry' AND status = 'filled'
            ORDER BY id DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        return data if data["tranche_index"] < data["tranche_total"] else None

    def latest_filled_entry_for(self, symbol: str) -> dict[str, Any] | None:
        """The most recently filled entry order for `symbol` (S4, Phase 5) -- what
        `intraday/monitor.py` starts from to find the proposal (and therefore the
        invalidation/time-stop) a currently-held position was opened under. Singular,
        same "most recent" convention as `pending_tranche_for`: a
        symbol closed and reopened same-day is a new, more recent row, so this always
        finds the entry that's actually still held."""
        row = self._conn.execute(
            """
            SELECT * FROM orders
            WHERE symbol = ? AND leg = 'entry' AND status = 'filled'
            ORDER BY id DESC LIMIT 1
            """,
            (symbol,),
        ).fetchone()
        return dict(row) if row is not None else None

    def orders_for_proposal(self, proposal_id: int, leg: str | None = None) -> list[dict[str, Any]]:
        """Every order recorded against ``proposal_id``, optionally filtered to one
        ``leg`` (S4, Phase 9a) -- needed because a single accepted proposal can produce
        more than one ``leg='entry'`` order once `entry_tranches` > 1 (Phase 5): summing
        a tranched entry's true filled qty/notional means reading every tranche's own
        order row, not just the most recent one (`latest_filled_entry_for`'s own
        "singular" convention is right for finding *the proposal*, since every tranche
        shares the same `proposal_id`, but wrong for summing fills)."""
        if leg is None:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE proposal_id = ? ORDER BY id", (proposal_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE proposal_id = ? AND leg = ? ORDER BY id",
                (proposal_id, leg),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_fill(
        self, client_order_id: str, qty: float, price: float, filled_at: dt.datetime
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO fills (client_order_id, qty, price, filled_at) VALUES (?, ?, ?, ?)",
            (client_order_id, qty, price, filled_at.isoformat()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def fills_with_orders(self) -> list[dict[str, Any]]:
        """Every fill with its order's ``symbol``, ``side``, ``leg`` and ``proposal_id``,
        in the order recorded: what `intraday/readiness.py` rebuilds positions from."""
        rows = self._conn.execute(
            """
            SELECT f.client_order_id, f.qty, f.price, f.filled_at,
                   o.symbol, o.side, o.leg, o.proposal_id
            FROM fills AS f JOIN orders AS o ON o.client_order_id = f.client_order_id
            ORDER BY f.id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def fills_for_order(self, client_order_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM fills WHERE client_order_id = ? ORDER BY id", (client_order_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- snapshots / reconciliations --------------------------------------------------

    def record_snapshot(
        self,
        as_of: pd.Timestamp,
        equity: float,
        cash: float,
        positions: dict[str, dict[str, float]],
    ) -> int:
        """Idempotent by ``as_of`` -- re-marking the same day overwrites rather than
        duplicates, same as ``live/ledger.py``'s ``record_snapshot``."""
        as_of_str = str(pd.Timestamp(as_of))
        self._conn.execute(
            """
            INSERT INTO snapshots (as_of, equity, cash, positions_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(as_of) DO UPDATE SET
                equity = excluded.equity,
                cash = excluded.cash,
                positions_json = excluded.positions_json,
                created_at = excluded.created_at
            """,
            (as_of_str, equity, cash, _dumps(positions), _now()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM snapshots WHERE as_of = ?", (as_of_str,)
        ).fetchone()
        return int(row["id"])

    def latest_snapshot(self) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM snapshots ORDER BY as_of DESC LIMIT 1").fetchone()
        if row is None:
            return None
        data = dict(row)
        data["positions"] = json.loads(data.pop("positions_json"))
        return data

    def record_reconciliation(
        self, as_of: pd.Timestamp, status: str, discrepancies: list[dict] | dict
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO reconciliations (as_of, status, discrepancies_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(pd.Timestamp(as_of)), status, _dumps(discrepancies), _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def latest_reconciliation(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM reconciliations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["discrepancies"] = json.loads(data.pop("discrepancies_json"))
        return data

    # -- session state (S4, Phase 5) -------------------------------------------------------

    def record_session_open_equity(self, as_of: pd.Timestamp, equity: float) -> None:
        """Idempotent by `as_of` (`ON CONFLICT DO NOTHING`) -- the first cycle to run
        today sets this; every later cycle's attempt to set it again is a no-op, since
        "the day's starting equity" must never drift once trading has begun. Feeds
        `ValidationContext.equity_at_session_open`'s daily-loss checks (Phase 1) -- the
        first real caller to populate that field; every caller before Phase 5 was a test
        supplying it directly.
        """
        self._conn.execute(
            """
            INSERT INTO session_state (as_of, equity_at_open, created_at) VALUES (?, ?, ?)
            ON CONFLICT(as_of) DO NOTHING
            """,
            (str(pd.Timestamp(as_of)), equity, _now()),
        )
        self._conn.commit()

    def session_open_equity(self, as_of: pd.Timestamp) -> float | None:
        row = self._conn.execute(
            "SELECT equity_at_open FROM session_state WHERE as_of = ?",
            (str(pd.Timestamp(as_of)),),
        ).fetchone()
        return float(row["equity_at_open"]) if row is not None else None

    # -- reflections ----------------------------------------------------------------------

    def record_reflection(
        self,
        as_of: pd.Timestamp,
        symbol: str,
        content: dict[str, Any],
        client_order_id: str | None = None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO reflections (as_of, symbol, client_order_id, content_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(pd.Timestamp(as_of)), symbol, client_order_id, _dumps(content), _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def reflections_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM reflections WHERE as_of = ? ORDER BY id", (str(pd.Timestamp(as_of)),)
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["content"] = json.loads(data.pop("content_json"))
            out.append(data)
        return out

    def update_reflection(self, reflection_id: int, content: dict[str, Any]) -> None:
        """Overwrites a reflection's `content_json` in place (S4, Phase 9b) -- how
        `eod_review` fills in `category`/`process_grade`/`lesson` on a row Phase 9a's
        `reflect.py::record_mechanical_reflection` already created with those fields
        `None`. Same upsert-by-id shape `save_shadow_broker_state` already uses for a
        different table."""
        self._conn.execute(
            "UPDATE reflections SET content_json = ? WHERE id = ?",
            (_dumps(content), reflection_id),
        )
        self._conn.commit()

    def all_reflections(self) -> list[dict[str, Any]]:
        """Every reflection ever recorded, across every trading day -- what
        `review.py`'s running confidence-calibration curve needs (`reflections_for`
        only ever returns one day at a time)."""
        rows = self._conn.execute("SELECT * FROM reflections ORDER BY id").fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["content"] = json.loads(data.pop("content_json"))
            out.append(data)
        return out

    # -- model calls (what budget.py sums) -------------------------------------------------

    def record_model_call(
        self,
        as_of: pd.Timestamp,
        cycle: str,
        role: str,
        model: str,
        prompt_hash: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO model_calls
                (as_of, cycle, role, model, prompt_hash, input_tokens, output_tokens,
                 cost_usd, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(pd.Timestamp(as_of)),
                cycle,
                role,
                model,
                prompt_hash,
                input_tokens,
                output_tokens,
                cost_usd,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def model_calls_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM model_calls WHERE as_of = ? ORDER BY id", (str(pd.Timestamp(as_of)),)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- headlines seen (dedup) -------------------------------------------------------------

    def record_headline_seen(
        self,
        as_of: pd.Timestamp,
        source: str,
        headline_hash: str,
        symbols: list[str] | None = None,
        headline: str | None = None,
    ) -> None:
        """``ON CONFLICT ... DO NOTHING`` -- the same headline from the same source is only
        ever logged once, regardless of how many cycles re-fetch it. `symbols`/`headline`
        (Phase 6) are optional so a caller with nothing but a dedup fingerprint (as in
        every pre-Phase-6 test) still works unchanged; `recent_headlines_for` needs both to
        show the Trader anything, so `research`/`preopen` always pass them."""
        self._conn.execute(
            """
            INSERT INTO headlines_seen (as_of, source, headline_hash, symbols, headline, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, headline_hash) DO NOTHING
            """,
            (
                str(pd.Timestamp(as_of)),
                source,
                headline_hash,
                ",".join(symbols) if symbols else None,
                headline,
                _now(),
            ),
        )
        self._conn.commit()

    def headline_already_seen(self, source: str, headline_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM headlines_seen WHERE source = ? AND headline_hash = ?",
            (source, headline_hash),
        ).fetchone()
        return row is not None

    def recent_headlines_for(self, as_of: pd.Timestamp, symbols: list[str]) -> list[dict[str, Any]]:
        """Every headline recorded today (`as_of`) mentioning at least one of `symbols` --
        one read path for both `preopen`'s own "what's new" log line and
        `build_trader_context`'s headlines section, since both just want "what's been seen
        today for names I care about." Filters in Python after a broad `as_of` pull
        (`symbols` is stored as one comma-joined string), the same "filter in Python once
        the SQL side gets awkward" pattern `pending_tranche_for` established in Phase 5.
        """
        rows = self._conn.execute(
            "SELECT * FROM headlines_seen WHERE as_of = ? AND headline IS NOT NULL",
            (str(pd.Timestamp(as_of)),),
        ).fetchall()
        wanted = set(symbols)
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            row_symbols = set(data["symbols"].split(",")) if data["symbols"] else set()
            if row_symbols & wanted:
                out.append(data)
        return out

    # -- incidents ------------------------------------------------------------------------

    def record_incident(self, severity: str, message: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO incidents (severity, message, created_at) VALUES (?, ?, ?)",
            (severity, message, _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def incidents(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def incidents_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        """`incidents` carries no `as_of` column (it's a plain timestamped log, not
        scoped to a trading day), so each row is dated by the session its `created_at`
        falls in (`semimc/trading_day.py`: New York date, rolling over at 04:00). This
        used to compare the UTC date, on the assumption that a trading day never crosses
        a UTC midnight; a late job does (2026-10-05: GitHub's late copy of carry ran at
        19:57 Central, and its error counted against the next day)."""
        day = pd.Timestamp(as_of).normalize()
        return [row for row in self.incidents() if trading_day(row["created_at"]) == day]

    def last_stopout_times(self) -> dict[str, dt.datetime]:
        """symbol -> most recent invalidation-triggered stop-out time (S4, Phase 5),
        derived from `intraday/monitor.py`'s own incident log (message prefixed
        ``"stopout:{symbol}"`` -- there is no dedicated table for this, so the incident
        log doubles as the record). Feeds `ValidationContext.last_stopout_at`'s
        post-stop-out cooldown check (Phase 1) -- the first real caller to populate that
        field; every caller before Phase 5 was a test supplying it directly.
        """
        out: dict[str, dt.datetime] = {}
        for row in self.incidents():
            if row["message"].startswith("stopout:"):
                symbol = row["message"].split(":", 1)[1]
                out[symbol] = dt.datetime.fromisoformat(row["created_at"])
        return out

    # -- monitor checks (S4, cadence telemetry, found 2026-09-23) ------------------------

    def record_monitor_check(
        self, as_of: pd.Timestamp, cycle: str, checked_at: dt.datetime
    ) -> float | None:
        """Records that `check_invalidation_and_time_stops` actually ran, and returns the
        gap in seconds since this same trading day's previous recorded check (`None` for
        the day's first) -- found the hard way, 2026-09-23: GitHub's own `schedule` event
        delivery for this repo is far less reliable than the configured cron intervals
        suggest (most `monitor`/`cycle` triggers never fire at all -- confirmed against
        both this workflow and `paper_trading.yml`'s own, independent schedule, so it
        isn't this project's own concurrency-group design), and `FakeBroker` never
        auto-triggers a stop the way a real resting order would (see its own module
        docstring) -- so during shadow mode, an invalidated position's actual protection
        is only as prompt as this gap. Scoped to `as_of` rather than the single global
        previous row, so the expected multi-hour overnight gap between yesterday's last
        check and today's first is never counted as a same-day cadence failure.
        """
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=dt.UTC)
        previous = self._conn.execute(
            "SELECT checked_at FROM monitor_checks WHERE as_of = ? ORDER BY id DESC LIMIT 1",
            (str(pd.Timestamp(as_of)),),
        ).fetchone()
        gap_seconds: float | None = None
        if previous is not None:
            previous_at = dt.datetime.fromisoformat(previous["checked_at"])
            gap_seconds = (checked_at - previous_at).total_seconds()
        self._conn.execute(
            "INSERT INTO monitor_checks (as_of, cycle, checked_at, gap_seconds) "
            "VALUES (?, ?, ?, ?)",
            (str(pd.Timestamp(as_of)), cycle, checked_at.isoformat(), gap_seconds),
        )
        self._conn.commit()
        return gap_seconds

    def monitor_checks_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM monitor_checks WHERE as_of = ? ORDER BY id",
            (str(pd.Timestamp(as_of)),),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- shadow broker state (S4's placebo agent, Phase 7) -------------------------------

    def save_shadow_broker_state(self, state: dict[str, Any]) -> None:
        """Upserts the single persisted `FakeBroker.to_state()` snapshot -- one row,
        always id=1. Each GitHub Actions run is a fresh process with no real broker to
        query, so this is the placebo's only way to remember what it holds between runs.
        """
        self._conn.execute(
            """
            INSERT INTO shadow_broker_state (id, state_json, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json,
                                           updated_at = excluded.updated_at
            """,
            (_dumps(state), _now()),
        )
        self._conn.commit()

    def load_shadow_broker_state(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT state_json FROM shadow_broker_state WHERE id = 1"
        ).fetchone()
        return json.loads(row["state_json"]) if row is not None else None

    def total_realized_cash_flow(self) -> float:
        """Signed sum of every fill this ledger has ever recorded -- a buy is a cash
        outflow, a sell is an inflow. `intraday/placebo.py`'s mark-to-market uses this
        (plus a starting equity and a fresh mark of whatever's still held) to derive shadow
        cash/equity fresh every call, since nothing else updates a `FakeBroker`'s own
        `.cash`/`.equity` automatically the way a real broker would.

        A full `fills JOIN orders` scan, fine at S4's cadence (on the order of one placebo
        entry per day) -- a resting stop leg's own order never produces a `fills` row
        (`FakeBroker.submit_order`'s own rule: `type in ("stop", "stop_limit")` always
        rests at `filled_qty=0`), so this never needs to filter by `leg`.
        """
        rows = self._conn.execute(
            """
            SELECT o.side AS side, f.qty AS qty, f.price AS price
            FROM fills f JOIN orders o ON f.client_order_id = o.client_order_id
            """
        ).fetchall()
        total = 0.0
        for row in rows:
            notional = row["qty"] * row["price"]
            total += -notional if row["side"] == "buy" else notional
        return total

    # -- playbook (S4, Phase 10) -----------------------------------------------------------

    def record_playbook_decision(
        self,
        status: str,
        text: str,
        source_proposal_path: str,
        adopted_at: pd.Timestamp | None = None,
        review_date: pd.Timestamp | None = None,
        resolution_reason: str | None = None,
    ) -> int:
        """One row per accept/reject decision on a proposal file (`intraday/playbook.py`).
        A rejection (`status="rejected"`) sets `resolution_reason` and leaves
        `adopted_at`/`review_date` null; an acceptance (`status="active"`) sets the
        reverse. Retiring an already-accepted entry later goes through
        :meth:`update_playbook_entry_status` instead of a second insert -- one row per
        proposal's whole lifecycle, not one row per state transition."""
        cursor = self._conn.execute(
            """
            INSERT INTO playbook_entries
                (status, text, source_proposal_path, adopted_at, review_date,
                 resolved_at, resolution_reason, created_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                status,
                text,
                source_proposal_path,
                str(pd.Timestamp(adopted_at)) if adopted_at is not None else None,
                str(pd.Timestamp(review_date)) if review_date is not None else None,
                resolution_reason,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def get_playbook_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM playbook_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def update_playbook_entry_status(
        self, entry_id: int, status: str, resolved_at: pd.Timestamp, resolution_reason: str
    ) -> None:
        """Retires a currently-active entry in place (`intraday/playbook.py::
        retire_entry`) -- same upsert-by-id shape `update_reflection` already uses."""
        self._conn.execute(
            """
            UPDATE playbook_entries
            SET status = ?, resolved_at = ?, resolution_reason = ?
            WHERE id = ?
            """,
            (status, str(pd.Timestamp(resolved_at)), resolution_reason, entry_id),
        )
        self._conn.commit()

    def active_playbook_entries(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM playbook_entries WHERE status = 'active' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def all_playbook_entries(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM playbook_entries ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def record_playbook_version(self, text: str, change_kind: str, entry_id: int) -> int:
        """One row per change to `PLAYBOOK.md` as a whole (`intraday/playbook.py`) -- `id`
        doubles as the version number a trade proposal stamps itself with
        (`current_playbook_version`). `text` is a full snapshot, the same "snapshot per
        change" idiom `snapshots` already uses for account equity, so a past version's
        exact wording is always recoverable without replaying every entry's history."""
        cursor = self._conn.execute(
            """
            INSERT INTO playbook_versions (text, change_kind, entry_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (text, change_kind, entry_id, _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def current_playbook_version(self) -> int | None:
        row = self._conn.execute("SELECT MAX(id) AS v FROM playbook_versions").fetchone()
        return int(row["v"]) if row is not None and row["v"] is not None else None

    def distinct_trading_days(self) -> list[str]:
        """Every distinct calendar date (`YYYY-MM-DD`, normalized -- `as_of` is stored
        with a `00:00:00` time component, `str(pd.Timestamp(...))`'s own default) S4 has
        recorded any activity for, across `proposals`/`reflections`/`briefs` -- how many
        days the loop has actually run, with no broker/market-calendar dependency
        (`review.py::weekly_summary` wants this window without pulling in a `Broker`
        just to count days)."""
        rows = self._conn.execute(
            """
            SELECT as_of FROM proposals
            UNION SELECT as_of FROM reflections
            UNION SELECT as_of FROM briefs
            """
        ).fetchall()
        return sorted({str(pd.Timestamp(row["as_of"]).date()) for row in rows})
