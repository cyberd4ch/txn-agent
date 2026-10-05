"""Per-tenant durable state (stdlib sqlite3 only).

One `SQLiteStore` per process backs any number of tenants: every row is namespaced by
tenant name, so restarts (or a second app instance over the same file) resume with
intents, offers, carts, runs, approvals, and audit history intact. Money columns are
TEXT (Decimal strings) — never floats.

Deployment notes: WAL mode is enabled; a single connection + lock keeps threads honest.
For multi-node deployments swap this class for a real database behind the same methods.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from .audit import AuditLog
from .models import Cart, Intent, Offer, cart_from_dict, intent_from_dict, offer_from_dict, to_dict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT NOT NULL, tenant TEXT NOT NULL, vertical TEXT NOT NULL,
    query TEXT NOT NULL, max_total TEXT NOT NULL, quantity INTEGER NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (intent_id, tenant));
CREATE TABLE IF NOT EXISTS offers (
    intent_id TEXT NOT NULL, tenant TEXT NOT NULL, offer_id TEXT NOT NULL,
    payload TEXT NOT NULL, PRIMARY KEY (intent_id, offer_id, tenant));
CREATE TABLE IF NOT EXISTS carts (
    cart_id TEXT NOT NULL, tenant TEXT NOT NULL, payload TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (cart_id, tenant));
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT NOT NULL, tenant TEXT NOT NULL, status TEXT NOT NULL,
    outcome TEXT, updated_at TEXT NOT NULL, owner_pid INTEGER,
    PRIMARY KEY (run_id, tenant));
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT NOT NULL, tenant TEXT NOT NULL, payload TEXT NOT NULL,
    decision TEXT, decided_at TEXT, PRIMARY KEY (approval_id, tenant));
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL, ts TEXT NOT NULL,
    intent_id TEXT, event TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_tenant_idx ON audit (tenant, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    """Durable per-tenant state. Methods are JSON-codec boundaries: callers pass and
    receive domain objects (Intent/Offer/Cart) or plain dicts (runs/approvals/audit)."""

    def __init__(self, path: str | Path):
        path = Path(path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "owner_pid" not in cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN owner_pid INTEGER")

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query_one(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...] | None:
        with self._lock:
            return cast("tuple[Any, ...] | None", self._conn.execute(sql, params).fetchone())

    # ------------------------------------------------------------------ intents

    def save_intent(self, tenant: str, intent: Intent) -> None:
        self._exec("INSERT OR REPLACE INTO intents VALUES (?,?,?,?,?,?,?)",
                   (intent.intent_id, tenant, intent.vertical.value, intent.query,
                    str(intent.max_total), intent.quantity, _now()))

    def load_intent(self, tenant: str, intent_id: str) -> Intent | None:
        row = self._query_one("SELECT vertical, query, max_total, quantity, intent_id "
                              "FROM intents WHERE tenant=? AND intent_id=?", (tenant, intent_id))
        if row is None:
            return None
        return intent_from_dict({"vertical": row[0], "query": row[1], "max_total": row[2],
                                 "quantity": row[3], "intent_id": row[4]})

    # ------------------------------------------------------------------ offers

    def save_offers(self, tenant: str, intent_id: str, offers: list[Offer]) -> None:
        with self._lock:
            for o in offers:
                self._conn.execute("INSERT OR REPLACE INTO offers VALUES (?,?,?,?)",
                                   (intent_id, tenant, o.offer_id,
                                    json.dumps(to_dict(o), default=str)))
            self._conn.commit()

    def load_offer(self, tenant: str, intent_id: str, offer_id: str) -> Offer | None:
        row = self._query_one("SELECT payload FROM offers "
                              "WHERE tenant=? AND intent_id=? AND offer_id=?",
                              (tenant, intent_id, offer_id))
        return offer_from_dict(json.loads(row[0])) if row else None

    # ------------------------------------------------------------------ carts

    def save_cart(self, tenant: str, cart: Cart) -> None:
        payload = {"cart_id": cart.cart_id,
                   "max_total": str(cart.max_total) if cart.max_total is not None else None,
                   "lines": [{"offer": to_dict(i.offer), "quantity": i.quantity}
                             for i in cart.items]}
        self._exec("INSERT OR REPLACE INTO carts VALUES (?,?,?,?)",
                   (cart.cart_id, tenant, json.dumps(payload, default=str), _now()))

    def load_cart(self, tenant: str, cart_id: str) -> Cart | None:
        row = self._query_one("SELECT payload FROM carts WHERE tenant=? AND cart_id=?",
                              (tenant, cart_id))
        return cart_from_dict(json.loads(row[0])) if row else None

    # ------------------------------------------------------------------ runs

    def save_run(self, tenant: str, run_id: str, status: str,
                 outcome: dict[str, Any] | None = None,
                 owner_pid: int | None = None) -> None:
        self._exec("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                   (run_id, tenant, status,
                    json.dumps(outcome, default=str) if outcome is not None else None,
                    _now(), owner_pid))

    def load_run(self, tenant: str, run_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT status, outcome, owner_pid FROM runs "
                              "WHERE tenant=? AND run_id=?", (tenant, run_id))
        if row is None:
            return None
        return {"status": row[0], "outcome": json.loads(row[1]) if row[1] else None,
                "owner_pid": row[2]}

    # ------------------------------------------------------------------ approvals

    def record_approval(self, tenant: str, approval_id: str, payload: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?)",
                   (approval_id, tenant, json.dumps(payload, default=str), None, None))

    def resolve_approval(self, tenant: str, approval_id: str, approved: bool) -> bool:
        cur = self._exec("UPDATE approvals SET decision=?, decided_at=? "
                         "WHERE tenant=? AND approval_id=? AND decision IS NULL",
                         ("approved" if approved else "denied", _now(), tenant, approval_id))
        return bool(cur.rowcount)

    def pending_approvals(self, tenant: str) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT approval_id, payload FROM approvals "
                "WHERE tenant=? AND decision IS NULL", (tenant,)).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def load_approval(self, tenant: str, approval_id: str) -> dict[str, Any] | None:
        row = self._query_one("SELECT payload, decision FROM approvals "
                              "WHERE tenant=? AND approval_id=?", (tenant, approval_id))
        if row is None:
            return None
        return {"payload": json.loads(row[0]), "decision": row[1]}

    # ------------------------------------------------------------------ audit

    def append_audit(self, tenant: str, entry: dict[str, Any]) -> None:
        self._exec("INSERT INTO audit (tenant, ts, intent_id, event, payload) VALUES (?,?,?,?,?)",
                   (tenant, str(entry.get("ts", _now())), str(entry.get("intent_id", "")),
                    str(entry.get("event", "")), json.dumps(entry, default=str)))

    def audit_events(self, tenant: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM audit WHERE tenant=? ORDER BY id DESC LIMIT ?",
                (tenant, limit)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]


class StoreAuditLog(AuditLog):
    """AuditLog that mirrors every entry into the store (and restores the in-memory
    view on startup), so /v1/audit survives restarts."""

    def __init__(self, store: SQLiteStore, tenant: str, restore_limit: int = 500):
        AuditLog.__init__(self, None)  # set up the in-memory event list
        self._store = store
        self._tenant = tenant
        for entry in store.audit_events(tenant, limit=restore_limit):
            self.events.append(entry)

    def record(self, intent_id: str, event: str, **data: Any) -> None:
        super().record(intent_id, event, **data)
        self._store.append_audit(self._tenant, self.events[-1])
