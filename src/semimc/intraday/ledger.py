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
    created_at TEXT NOT NULL
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
    leg TEXT NOT NULL DEFAULT 'entry'
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
    created_at TEXT NOT NULL,
    UNIQUE (source, headline_hash)
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_OPEN_ORDER_STATUSES = ("new", "accepted", "partially_filled")


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
    ) -> int:
        """Every proposal, accepted or rejected -- a plain append, per the brief's own
        "blotter: every proposal including rejected ones." ``status`` carries the
        accept/reject outcome; ``reason`` is populated on a rejection."""
        cursor = self._conn.execute(
            """
            INSERT INTO proposals (as_of, cycle, symbol, action, status, reason,
                                    payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def proposals_for(self, as_of: pd.Timestamp) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM proposals WHERE as_of = ? ORDER BY id", (str(pd.Timestamp(as_of)),)
        ).fetchall()
        out = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json"))
            out.append(data)
        return out

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
    ) -> int:
        """Insert or update one order by its ``client_order_id`` -- a resubmission of the
        same id (idempotent at the broker) updates status in place, same reasoning as
        ``live/ledger.py``'s ``record_order``.

        ``leg`` distinguishes an entry order's row from its resting stop-loss leg's own
        row (S4, Phase 4) -- ``"entry"`` (the default, every order before Phase 4) or
        ``"stop"``. :func:`~semimc.intraday.orders.submit_order_with_fault_isolation`
        records a successful ``"oto"`` submission's leg this way automatically.
        """
        self._conn.execute(
            """
            INSERT INTO orders
                (client_order_id, proposal_id, symbol, side, qty, notional, limit_price,
                 status, broker_order_id, submitted_at, updated_at, leg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def open_stop_leg_for(self, symbol: str) -> dict[str, Any] | None:
        """The most recently recorded still-open stop-loss leg for ``symbol``, or
        ``None`` (S4, Phase 4) -- what `intraday/orders.py::find_and_cancel_resting_stop`
        looks up before a close/trim. Singular, not a list: nothing before Phase 5's
        tranche-splitting can produce more than one open entry (and therefore more than
        one resting stop) for the same symbol at once -- revisit this once that changes.
        """
        placeholders = ",".join("?" * len(_OPEN_ORDER_STATUSES))
        row = self._conn.execute(
            f"""
            SELECT * FROM orders
            WHERE symbol = ? AND leg = 'stop' AND status IN ({placeholders})
            ORDER BY id DESC LIMIT 1
            """,
            (symbol, *_OPEN_ORDER_STATUSES),
        ).fetchone()
        return dict(row) if row is not None else None

    def record_fill(
        self, client_order_id: str, qty: float, price: float, filled_at: dt.datetime
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO fills (client_order_id, qty, price, filled_at) VALUES (?, ?, ?, ?)",
            (client_order_id, qty, price, filled_at.isoformat()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

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

    def record_headline_seen(self, as_of: pd.Timestamp, source: str, headline_hash: str) -> None:
        """``ON CONFLICT ... DO NOTHING`` -- the same headline from the same source is only
        ever logged once, regardless of how many cycles re-fetch it."""
        self._conn.execute(
            """
            INSERT INTO headlines_seen (as_of, source, headline_hash, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source, headline_hash) DO NOTHING
            """,
            (str(pd.Timestamp(as_of)), source, headline_hash, _now()),
        )
        self._conn.commit()

    def headline_already_seen(self, source: str, headline_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM headlines_seen WHERE source = ? AND headline_hash = ?",
            (source, headline_hash),
        ).fetchone()
        return row is not None

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
