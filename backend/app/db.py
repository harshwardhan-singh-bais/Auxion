"""SQLite persistence layer. Orders, sessions, campaigns, events survive restarts.

Thread-safe via a lock + short-lived connections. Small enough for a demo, real
enough to prove durability (#reproducible, settlement survives reload).
"""
from __future__ import annotations

import json
import sqlite3
import time
from threading import Lock
from typing import Any, Optional

from .config import settings

_lock = Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(settings.db_path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                tier TEXT,
                merchant TEXT,
                created_at REAL,
                meta TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                session_id TEXT,
                merchant TEXT,
                amount REAL,
                currency TEXT,
                status TEXT,
                items TEXT,
                payment_ref TEXT,
                receipt TEXT,
                created_at REAL,
                updated_at REAL,
                meta TEXT
            );
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                kind TEXT,
                status TEXT,
                payload TEXT,
                created_at REAL,
                fired_at REAL
            );
            CREATE TABLE IF NOT EXISTS metrics (
                key TEXT PRIMARY KEY,
                value REAL
            );
            """
        )


def upsert_session(session_id: str, tier: str, merchant: str, created_at: float, meta: dict) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO sessions(session_id,tier,merchant,created_at,meta) VALUES(?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET tier=excluded.tier, meta=excluded.meta",
            (session_id, tier, merchant, created_at, json.dumps(meta)),
        )


def save_order(order: dict) -> None:
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO orders(order_id,session_id,merchant,amount,currency,status,items,
                                  payment_ref,receipt,created_at,updated_at,meta)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
                 status=excluded.status, payment_ref=excluded.payment_ref,
                 receipt=excluded.receipt, updated_at=excluded.updated_at, meta=excluded.meta""",
            (
                order["order_id"], order.get("session_id"), order.get("merchant"),
                order.get("amount"), order.get("currency"), order.get("status"),
                json.dumps(order.get("items", [])), order.get("payment_ref"),
                json.dumps(order.get("receipt")) if order.get("receipt") else None,
                order.get("created_at", time.time()), time.time(),
                json.dumps(order.get("meta", {})),
            ),
        )


def get_order(order_id: str) -> Optional[dict]:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    return _order_row(row) if row else None


def all_orders(limit: int = 500) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [_order_row(r) for r in rows]


def _order_row(r: sqlite3.Row) -> dict:
    return {
        "order_id": r["order_id"], "session_id": r["session_id"], "merchant": r["merchant"],
        "amount": r["amount"], "currency": r["currency"], "status": r["status"],
        "items": json.loads(r["items"] or "[]"), "payment_ref": r["payment_ref"],
        "receipt": json.loads(r["receipt"]) if r["receipt"] else None,
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "meta": json.loads(r["meta"] or "{}"),
    }


def save_campaign(camp: dict) -> None:
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO campaigns(id,session_id,kind,status,payload,created_at,fired_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, fired_at=excluded.fired_at,
                 payload=excluded.payload""",
            (camp["id"], camp.get("session_id"), camp.get("kind"), camp.get("status"),
             json.dumps(camp.get("payload", {})), camp.get("created_at", time.time()),
             camp.get("fired_at")),
        )


def all_campaigns(limit: int = 200) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [
        {"id": r["id"], "session_id": r["session_id"], "kind": r["kind"], "status": r["status"],
         "payload": json.loads(r["payload"] or "{}"), "created_at": r["created_at"],
         "fired_at": r["fired_at"]}
        for r in rows
    ]


def set_metric(key: str, value: float) -> None:
    with _lock, _conn() as c:
        c.execute("INSERT INTO metrics(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def get_metric(key: str, default: float = 0.0) -> float:
    with _lock, _conn() as c:
        row = c.execute("SELECT value FROM metrics WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def reset_all() -> None:
    """For seeded/reproducible demo runs."""
    with _lock, _conn() as c:
        c.executescript("DELETE FROM orders; DELETE FROM campaigns; DELETE FROM sessions; DELETE FROM metrics;")


# Initialize the schema at import time so any import order is safe (the
# orchestrator singleton reads metrics before main.py would otherwise init).
init_db()
