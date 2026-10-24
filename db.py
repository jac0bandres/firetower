"""SQLite storage for Firetower tickets, priority nodes, and packet dedupe."""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("FIRETOWER_DB_PATH", "data/firetower.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_code TEXT UNIQUE NOT NULL,
    node_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    urgency TEXT NOT NULL,
    location TEXT,
    what_happened TEXT,
    injured_count INTEGER,
    raised_by_keyword INTEGER NOT NULL DEFAULT 0,
    priority_floor_applied INTEGER NOT NULL DEFAULT 0,
    hop_count INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    assigned_at TEXT,
    synced_to_snowflake INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS priority_nodes (
    node_id TEXT PRIMARY KEY,
    role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_packets (
    packet_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def is_duplicate_packet(packet_id: str) -> bool:
    raise NotImplementedError


def mark_packet_seen(packet_id: str) -> None:
    raise NotImplementedError


def next_ticket_code() -> str:
    raise NotImplementedError


def insert_ticket(ticket: dict) -> str:
    raise NotImplementedError


def list_tickets(status: str | None = None) -> list[dict]:
    raise NotImplementedError


def set_ticket_status(ticket_code: str, status: str) -> None:
    raise NotImplementedError


def list_priority_nodes() -> dict[str, str]:
    raise NotImplementedError


def set_priority_node(node_id: str, role: str) -> None:
    raise NotImplementedError


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
