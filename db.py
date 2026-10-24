"""SQLite storage for Firetower tickets, priority nodes, and packet dedupe."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

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
    latitude REAL,
    longitude REAL,
    position_received_at TEXT,
    position_packet_json TEXT,
    meshtastic_packet_json TEXT,
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
CREATE TABLE IF NOT EXISTS node_positions (
    node_id TEXT PRIMARY KEY,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    received_at TEXT NOT NULL,
    meshtastic_packet_json TEXT NOT NULL
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
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        for name, column_type in (
            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("position_received_at", "TEXT"),
            ("position_packet_json", "TEXT"),
            ("meshtastic_packet_json", "TEXT"),
        ):
            if name not in existing:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {column_type}")
        conn.commit()


def is_duplicate_packet(packet_id: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_packets WHERE packet_id = ? AND first_seen_at >= ?",
            (packet_id, cutoff),
        ).fetchone()
    return row is not None


def mark_packet_seen(packet_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO seen_packets(packet_id, first_seen_at) VALUES (?, ?) "
            "ON CONFLICT(packet_id) DO UPDATE SET first_seen_at = excluded.first_seen_at",
            (packet_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def next_ticket_code() -> str:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM tickets").fetchone()
    return f"FT-{row['next_id']:04d}"


def insert_ticket(ticket: dict) -> str:
    code = ticket["ticket_code"]
    fields = (
        "ticket_code", "node_id", "packet_id", "received_at", "raw_text",
        "urgency", "location", "what_happened", "injured_count",
        "raised_by_keyword", "priority_floor_applied", "hop_count",
        "latitude", "longitude", "position_received_at", "position_packet_json",
        "meshtastic_packet_json",
    )
    with connect() as conn:
        conn.execute(
            f"INSERT INTO tickets ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            tuple(ticket.get(field) for field in fields),
        )
        conn.commit()
    return code


def list_tickets(status: str | None = None) -> list[dict]:
    query = "SELECT * FROM tickets"
    params = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY CASE urgency WHEN 'EMERGENCY' THEN 0 WHEN 'PRIORITY' THEN 1 WHEN 'WELFARE' THEN 2 ELSE 3 END, received_at DESC"
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
    tickets = []
    for row in rows:
        ticket = dict(row)
        ticket.pop("id")
        for flag in ("raised_by_keyword", "priority_floor_applied", "synced_to_snowflake"):
            ticket[flag] = bool(ticket[flag])
        tickets.append(ticket)
    return tickets


def set_ticket_status(ticket_code: str, status: str) -> None:
    if status not in {"open", "assigned", "closed"}:
        raise ValueError(f"Invalid ticket status: {status}")
    assigned_at = datetime.now(timezone.utc).isoformat() if status == "assigned" else None
    with connect() as conn:
        conn.execute(
            "UPDATE tickets SET status = ?, assigned_at = ? WHERE ticket_code = ?",
            (status, assigned_at, ticket_code),
        )
        conn.commit()


def list_priority_nodes() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT node_id, role FROM priority_nodes").fetchall()
    return {row["node_id"]: row["role"] for row in rows}


def set_priority_node(node_id: str, role: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO priority_nodes(node_id, role) VALUES (?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET role = excluded.role",
            (node_id, role),
        )
        conn.commit()


def remove_priority_node(node_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM priority_nodes WHERE node_id = ?", (node_id,))
        conn.commit()


def save_node_position(node_id: str, latitude: float, longitude: float,
                       received_at: str, packet_json: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO node_positions(node_id, latitude, longitude, received_at, meshtastic_packet_json) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
            "latitude = excluded.latitude, longitude = excluded.longitude, "
            "received_at = excluded.received_at, meshtastic_packet_json = excluded.meshtastic_packet_json "
            "WHERE excluded.received_at >= node_positions.received_at",
            (node_id, latitude, longitude, received_at, packet_json),
        )
        conn.commit()


def get_node_position(node_id: str, max_age_seconds: int = 1800) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM node_positions WHERE node_id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["received_at"])
    return dict(row) if timedelta(0) <= age <= timedelta(seconds=max_age_seconds) else None


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
