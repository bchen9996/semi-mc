"""The paper-trading ledger: one SQLite file holding every decision, order, fill, daily
snapshot, manual adjustment, reconciliation run, and incident a live job (a later phase)
ever produces.

Stdlib ``sqlite3``, not DuckDB -- nothing else in this project touches a database, and the
row counts here (one row per decision/order/fill/snapshot) never approach where that
tradeoff would matter. One :class:`Ledger` per SQLite file; every public method is exactly
one INSERT or one SELECT, so a bug in how a row is shaped can only exist in one place --
the same "one choke point" reasoning as ``MarketData.training_window``. Nothing above this
layer is allowed to write SQL directly.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from ..execution.broker import Order, OrderRequest, Position
from ..strategy.decide import Decision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    sleeve TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL,
    data_vintage TEXT,
    git_commit TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (sleeve, as_of, snapshot_hash)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    sleeve TEXT NOT NULL,
    decision_snapshot_hash TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    notional REAL,
    limit_price REAL,
    status TEXT NOT NULL,
    broker_order_id TEXT,
    submitted_at TEXT,
    updated_at TEXT NOT NULL
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
    sleeve TEXT NOT NULL,
    as_of TEXT NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (sleeve, as_of)
);

CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve TEXT NOT NULL,
    as_of TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount REAL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    discrepancies_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sleeve TEXT,
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


class Ledger:
    """A SQLite-backed record of everything a live paper-trading job does.

    Every insert method is idempotent where the brief's own "freeze decisions" and
    "duplicate client_order_id is the same order" rules require it (decisions, orders,
    snapshots); fills, adjustments, reconciliation runs, and incidents are plain appends,
    since each one is a genuinely new event even when it repeats a prior value.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- decisions --------------------------------------------------------------------

    def record_decision(
        self,
        decision: Decision,
        data_vintage: str | None = None,
        git_commit: str | None = None,
    ) -> int:
        """Insert one frozen decision.

        Idempotent by ``(sleeve, as_of, snapshot_hash)`` -- recording the identical
        decision twice returns the same row rather than a duplicate. A *different*
        decision for the same ``(sleeve, as_of)`` is a genuinely new row; deciding whether
        that should be allowed is a later phase's live-job policy, not this ledger's.
        """
        as_of = str(pd.Timestamp(decision.as_of))
        self._conn.execute(
            """
            INSERT INTO decisions
                (as_of, sleeve, snapshot_hash, weights_json, diagnostics_json,
                 data_vintage, git_commit, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sleeve, as_of, snapshot_hash) DO NOTHING
            """,
            (
                as_of,
                decision.sleeve,
                decision.snapshot_hash,
                _dumps(decision.weights.to_dict()),
                _dumps(decision.diagnostics),
                data_vintage,
                git_commit,
                _now(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM decisions WHERE sleeve = ? AND as_of = ? AND snapshot_hash = ?",
            (decision.sleeve, as_of, decision.snapshot_hash),
        ).fetchone()
        return int(row["id"])

    @staticmethod
    def _decode_decision_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["weights"] = json.loads(data.pop("weights_json"))
        data["diagnostics"] = json.loads(data.pop("diagnostics_json"))
        return data

    def get_decision(self, sleeve: str, as_of: pd.Timestamp) -> dict[str, Any] | None:
        """The most recently recorded decision for ``(sleeve, as_of)``, or ``None``."""
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE sleeve = ? AND as_of = ? ORDER BY id DESC LIMIT 1",
            (sleeve, str(pd.Timestamp(as_of))),
        ).fetchone()
        return None if row is None else self._decode_decision_row(row)

    def last_decision(self, sleeve: str) -> dict[str, Any] | None:
        """The most recently recorded decision for this sleeve, regardless of date --
        ``live/decide.py``'s rebalance-cadence check uses this to tell whether ``as_of``
        falls in a period already decided."""
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE sleeve = ? ORDER BY as_of DESC, id DESC LIMIT 1",
            (sleeve,),
        ).fetchone()
        return None if row is None else self._decode_decision_row(row)

    def decisions_for(self, sleeve: str) -> list[dict[str, Any]]:
        """Every recorded decision for this sleeve, oldest first -- ``analytics.
        paper_evaluation.replay_shadow_equity_curve`` (Phase 6) uses this to replay a
        shadow sleeve's whole decision history against realized prices, since a
        shadow-only sleeve never has a snapshot to read equity from directly."""
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE sleeve = ? ORDER BY as_of, id", (sleeve,)
        ).fetchall()
        return [self._decode_decision_row(r) for r in rows]

    # -- orders -------------------------------------------------------------------------

    def record_order(
        self, sleeve: str, decision_snapshot_hash: str, request: OrderRequest, order: Order
    ) -> int:
        """Insert or update one order by its ``client_order_id`` -- a resubmission of the
        same id (idempotent at the broker) updates status in place rather than creating a
        second row for what is, by construction, the same order."""
        self._conn.execute(
            """
            INSERT INTO orders
                (client_order_id, sleeve, decision_snapshot_hash, symbol, side, qty,
                 notional, limit_price, status, broker_order_id, submitted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_order_id) DO UPDATE SET
                status = excluded.status,
                broker_order_id = excluded.broker_order_id,
                updated_at = excluded.updated_at
            """,
            (
                request.client_order_id,
                sleeve,
                decision_snapshot_hash,
                request.symbol,
                request.side,
                request.qty,
                request.notional,
                request.limit_price,
                order.status,
                order.id,
                order.submitted_at.isoformat() if order.submitted_at else None,
                _now(),
            ),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM orders WHERE client_order_id = ?", (request.client_order_id,)
        ).fetchone()
        return int(row["id"])

    def update_order_status(self, client_order_id: str, order: Order) -> None:
        """Refresh just an order's status/broker id -- ``live/mark.py`` polls the broker by
        ``client_order_id`` and never has the original :class:`OrderRequest` on hand the
        way :meth:`record_order` needs, only a fresh :class:`Order`."""
        self._conn.execute(
            "UPDATE orders SET status = ?, broker_order_id = ?, updated_at = ? "
            "WHERE client_order_id = ?",
            (order.status, order.id, _now(), client_order_id),
        )
        self._conn.commit()

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def recent_orders(self, sleeve: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM orders WHERE sleeve = ? ORDER BY id DESC LIMIT ?", (sleeve, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def open_orders(self, sleeve: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" * len(_OPEN_ORDER_STATUSES))
        rows = self._conn.execute(
            f"SELECT * FROM orders WHERE sleeve = ? AND status IN ({placeholders})",
            (sleeve, *_OPEN_ORDER_STATUSES),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- fills ----------------------------------------------------------------------

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

    def recent_fills(self, sleeve: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT f.* FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id
            WHERE o.sleeve = ? ORDER BY f.id DESC LIMIT ?
            """,
            (sleeve, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def fills_with_order_context(self, sleeve: str) -> list[dict[str, Any]]:
        """Every fill for this sleeve, alongside the order fields ``analytics.
        execution_cost.execution_cost_report`` (Phase 6) needs to look up an independent
        arrival price -- symbol, side, and the order's own ``submitted_at`` -- without
        that module ever writing SQL itself, the same reasoning every other
        ``Ledger`` read method exists for."""
        rows = self._conn.execute(
            """
            SELECT f.client_order_id AS client_order_id, f.qty AS qty, f.price AS price,
                   f.filled_at AS filled_at, o.symbol AS symbol, o.side AS side,
                   o.submitted_at AS submitted_at
            FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id
            WHERE o.sleeve = ? ORDER BY f.id
            """,
            (sleeve,),
        ).fetchall()
        return [dict(r) for r in rows]

    def net_filled_qty(self, sleeve: str) -> dict[str, float]:
        """This sleeve's own view of what it should be holding, per symbol -- every
        recorded fill, signed by the order's side (buy positive, sell negative), summed
        across all orders ever placed."""
        rows = self._conn.execute(
            """
            SELECT o.symbol AS symbol, o.side AS side, f.qty AS qty
            FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id
            WHERE o.sleeve = ?
            """,
            (sleeve,),
        ).fetchall()
        return self._net_from_rows(rows)

    def net_filled_qty_all_sleeves(self) -> dict[str, float]:
        """The combined view across every sleeve this ledger has ever recorded a fill for.

        ``live/mark.py``'s reconciliation uses this, not the single-sleeve ``net_filled_qty``
        -- s0 and s3 share one real Alpaca paper account (``docs/go_live_checklist.md`` item
        6), so ``broker.list_positions()`` reflects whichever of them actually traded, not
        just the sleeve this particular ``mark`` run happens to be for. Comparing a single
        sleeve's own fills (always empty for a sleeve left disabled) against the shared
        account's real, other-sleeve-driven holdings produced a guaranteed, permanent
        reconciliation "discrepancy" for the disabled sleeve the moment the other one held
        its first real position -- found 2026-09-26, s3's first month-end rebalance. Every
        sleeve recorded in this ledger's ``orders``/``fills`` tables shares the same
        ``paper.registered_accounts`` broker (a shadow-only sleeve like ``vol_twin_shadow``
        never calls ``execute``, so it never appears here), so summing across all of them is
        exactly the account-wide total the broker itself would report."""
        rows = self._conn.execute(
            """
            SELECT o.symbol AS symbol, o.side AS side, f.qty AS qty
            FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id
            """
        ).fetchall()
        return self._net_from_rows(rows)

    @staticmethod
    def _net_from_rows(rows: list[sqlite3.Row]) -> dict[str, float]:
        net: dict[str, float] = {}
        for row in rows:
            signed = row["qty"] if row["side"] == "buy" else -row["qty"]
            net[row["symbol"]] = net.get(row["symbol"], 0.0) + signed
        return net

    # -- snapshots ------------------------------------------------------------------

    def record_snapshot(
        self,
        sleeve: str,
        as_of: pd.Timestamp,
        equity: float,
        cash: float,
        positions: dict[str, Position],
    ) -> int:
        """One sleeve's equity/cash/positions as of one date. Idempotent by
        ``(sleeve, as_of)`` -- re-marking the same day overwrites rather than duplicates."""
        as_of_str = str(pd.Timestamp(as_of))
        positions_payload = {
            symbol: {"qty": position.qty, "market_value": position.market_value}
            for symbol, position in positions.items()
        }
        self._conn.execute(
            """
            INSERT INTO snapshots (sleeve, as_of, equity, cash, positions_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sleeve, as_of) DO UPDATE SET
                equity = excluded.equity,
                cash = excluded.cash,
                positions_json = excluded.positions_json,
                created_at = excluded.created_at
            """,
            (sleeve, as_of_str, equity, cash, _dumps(positions_payload), _now()),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM snapshots WHERE sleeve = ? AND as_of = ?", (sleeve, as_of_str)
        ).fetchone()
        return int(row["id"])

    def snapshots_for(self, sleeve: str) -> list[dict[str, Any]]:
        """Every recorded snapshot for this sleeve, oldest first -- ``live/report.py``'s
        equity history."""
        rows = self._conn.execute(
            "SELECT * FROM snapshots WHERE sleeve = ? ORDER BY as_of", (sleeve,)
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_snapshot(self, sleeve: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE sleeve = ? ORDER BY as_of DESC LIMIT 1", (sleeve,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["positions"] = json.loads(data.pop("positions_json"))
        return data

    # -- adjustments, reconciliation, incidents --------------------------------------

    def record_adjustment(
        self, sleeve: str, as_of: pd.Timestamp, kind: str, note: str, amount: float | None = None
    ) -> int:
        """A manual/corrective ledger entry -- a corporate action, a fee, a hand-fixed
        reconciliation gap. Always a plain append: a correction is a new event, not an
        edit to history."""
        cursor = self._conn.execute(
            """
            INSERT INTO adjustments (sleeve, as_of, kind, amount, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sleeve, str(pd.Timestamp(as_of)), kind, amount, note, _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def record_reconciliation(
        self, sleeve: str, as_of: pd.Timestamp, status: str, discrepancies: list[dict] | dict
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO reconciliations (sleeve, as_of, status, discrepancies_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sleeve, str(pd.Timestamp(as_of)), status, _dumps(discrepancies), _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def latest_reconciliation(self, sleeve: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM reconciliations WHERE sleeve = ? ORDER BY id DESC LIMIT 1", (sleeve,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["discrepancies"] = json.loads(data.pop("discrepancies_json"))
        return data

    def record_incident(self, severity: str, message: str, sleeve: str | None = None) -> int:
        cursor = self._conn.execute(
            "INSERT INTO incidents (sleeve, severity, message, created_at) VALUES (?, ?, ?, ?)",
            (sleeve, severity, message, _now()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def incidents(self, sleeve: str | None = None) -> list[dict[str, Any]]:
        if sleeve is None:
            rows = self._conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM incidents WHERE sleeve = ? ORDER BY id", (sleeve,)
            ).fetchall()
        return [dict(r) for r in rows]
