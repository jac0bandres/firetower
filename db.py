"""SQLite storage for Firetower tickets, priority nodes, and packet dedupe."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import dispatch

DB_PATH = os.environ.get("FIRETOWER_DB_PATH", "data/firetower.db")

# tickets.location_source values other than real GPS ("gps"). Real GPS
# replaces any of these; none of these ever replaces real GPS.
ESTIMATED_SOURCES = ("gps_old", "map", "radio_area")

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
    clarification_question TEXT,
    clarification_response TEXT,
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
    dispatched_teams TEXT,
    dispatched_at TEXT,
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

CREATE TABLE IF NOT EXISTS ai_threads (
    thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES ai_threads(thread_id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_clarifications (
    node_id TEXT PRIMARY KEY,
    ticket_code TEXT NOT NULL,
    missing_fields TEXT NOT NULL,
    question_steps TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    node_id TEXT PRIMARY KEY,
    display_name TEXT,
    last_known_address TEXT,
    updated_at TEXT NOT NULL
);

-- Radio DMs queued by the board (which cannot reach the radio) for the
-- gateway to send, e.g. the dispatch notice to the reporter.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    ticket_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    error TEXT
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
            ("clarification_question", "TEXT"),
            ("clarification_response", "TEXT"),
            ("dispatched_teams", "TEXT"),
            ("dispatched_at", "TEXT"),
            # Where latitude/longitude came from: gps | gps_old | map | radio_area
            ("location_source", "TEXT"),
            ("location_radius_m", "INTEGER"),
            ("location_note", "TEXT"),
        ):
            if name not in existing:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {column_type}")
        pending_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pending_clarifications)")
        }
        if "question_steps" not in pending_columns:
            conn.execute("ALTER TABLE pending_clarifications ADD COLUMN question_steps TEXT")
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
        "clarification_question", "clarification_response",
        "location_source", "location_radius_m", "location_note",
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


def newest_active_ticket(node_id: str, max_age_seconds: int) -> dict | None:
    """The sender's newest open or assigned ticket, if it is recent enough."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE node_id = ? AND status IN ('open', 'assigned') "
            "AND received_at >= ? ORDER BY received_at DESC, id DESC LIMIT 1",
            (node_id, cutoff),
        ).fetchone()
    return dict(row) if row else None


def set_ticket_status(ticket_code: str, status: str) -> None:
    if status not in {"open", "assigned", "closed"}:
        raise ValueError(f"Invalid ticket status: {status}")
    assigned_at = datetime.now(timezone.utc).isoformat() if status == "assigned" else None
    with connect() as conn:
        if status == "open":
            conn.execute(
                "UPDATE tickets SET status = ?, assigned_at = ?, "
                "dispatched_teams = NULL, dispatched_at = NULL WHERE ticket_code = ?",
                (status, assigned_at, ticket_code),
            )
        else:
            conn.execute(
                "UPDATE tickets SET status = ?, assigned_at = ? WHERE ticket_code = ?",
                (status, assigned_at, ticket_code),
            )
        conn.commit()


def set_ticket_dispatch(ticket_code: str, teams: list[str]) -> None:
    """Record an in-app dispatch and mark the ticket assigned.

    `teams` is a list of ids from dispatch.TEAMS. Nothing is sent off the laptop.
    """
    ordered = dispatch.normalize_teams(teams)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM tickets WHERE ticket_code = ?", (ticket_code,)
        ).fetchone()
        if row is None:
            raise LookupError(ticket_code)
        conn.execute(
            "UPDATE tickets SET dispatched_teams = ?, dispatched_at = ?, "
            "status = 'assigned', assigned_at = ? WHERE ticket_code = ?",
            (",".join(ordered), now, now, ticket_code),
        )
        conn.commit()


def queue_outbox(node_id: str, ticket_code: str, kind: str, text: str) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO outbox(node_id, ticket_code, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (node_id, ticket_code, kind, text, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.lastrowid


def list_unsent_outbox(limit: int = 5) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND error IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def finish_outbox(message_id: int, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE outbox SET sent_at = ?, error = ? WHERE id = ?",
            (None if error else datetime.now(timezone.utc).isoformat(), error, message_id),
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


def attach_ticket_position(node_id: str, packet_id: str, latitude: float,
                           longitude: float, received_at: str, packet_json: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE tickets SET latitude = ?, longitude = ?, "
            "position_received_at = ?, position_packet_json = ?, location_source = 'gps', "
            "location_radius_m = NULL, location_note = NULL "
            "WHERE node_id = ? AND packet_id = ? AND ("
            "(latitude IS NULL AND longitude IS NULL) "
            f"OR location_source IN ({', '.join(repr(s) for s in ESTIMATED_SOURCES)}))",
            (latitude, longitude, received_at, packet_json, node_id, packet_id),
        )
        conn.commit()


def set_location_estimate(ticket_code: str, latitude: float, longitude: float, source: str,
                          radius_m: int | None, note: str | None) -> bool:
    """Pin a ticket from an estimate. Never overwrites real GPS."""
    if source not in ESTIMATED_SOURCES:
        raise ValueError(f"Not an estimated location source: {source}")
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE tickets SET latitude = ?, longitude = ?, location_source = ?, "
            "location_radius_m = ?, location_note = ? "
            "WHERE ticket_code = ? AND COALESCE(location_source, '') != 'gps'",
            (latitude, longitude, source, radius_m, note, ticket_code),
        )
        conn.commit()
    return cursor.rowcount > 0


def list_node_positions() -> list[dict]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM node_positions")]


def get_node_position(node_id: str, max_age_seconds: int | None = 1800) -> dict | None:
    """The sender's last fix. max_age_seconds=None returns it at any age."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM node_positions WHERE node_id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    if max_age_seconds is None:
        return dict(row)
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["received_at"])
    return dict(row) if timedelta(0) <= age <= timedelta(seconds=max_age_seconds) else None


def attach_late_position(node_id: str, latitude: float, longitude: float, received_at: str,
                         packet_json: str, window_seconds: int, stale_seconds: int) -> str | None:
    """GPS that arrives just after a text belongs to that report.

    Only the sender's newest ticket is considered, only if it was received
    within window_seconds before this fix, and only if it has no position or
    an approximate one (a fix more than stale_seconds away from the report).
    Returns the updated ticket code, or None.
    """
    fix_time = datetime.fromisoformat(received_at)
    with connect() as conn:
        row = conn.execute(
            "SELECT ticket_code, received_at, latitude, position_received_at, location_source "
            "FROM tickets WHERE node_id = ? ORDER BY received_at DESC, id DESC LIMIT 1",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        report_time = datetime.fromisoformat(row["received_at"])
        if not timedelta(0) <= fix_time - report_time <= timedelta(seconds=window_seconds):
            return None
        if row["location_source"] == "gps":
            return None
        if (row["location_source"] is None and row["latitude"] is not None
                and row["position_received_at"]):
            gap = abs(report_time - datetime.fromisoformat(row["position_received_at"]))
            if gap <= timedelta(seconds=stale_seconds):
                return None
        conn.execute(
            "UPDATE tickets SET latitude = ?, longitude = ?, position_received_at = ?, "
            "position_packet_json = ?, location_source = 'gps', location_radius_m = NULL, "
            "location_note = NULL WHERE ticket_code = ?",
            (latitude, longitude, received_at, packet_json, row["ticket_code"]),
        )
        conn.commit()
    return row["ticket_code"]


def get_or_create_ai_thread(node_id: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT thread_id FROM ai_threads WHERE node_id = ?", (node_id,)).fetchone()
        if row is not None:
            return row["thread_id"]
        cursor = conn.execute(
            "INSERT INTO ai_threads(node_id, created_at) VALUES (?, ?)",
            (node_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cursor.lastrowid


def add_ai_message(thread_id: int, role: str, content: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO ai_messages(thread_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, role, content, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def list_ai_thread_messages(thread_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM ai_messages WHERE thread_id = ? ORDER BY id",
            (thread_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_pending_clarification(node_id: str, ticket_code: str, missing_fields: list[str],
                              steps: list[str] | None = None, *, keep_created_at: bool = False) -> None:
    """Store the open follow-up for a sender.

    `steps` is the remaining question sequence. `keep_created_at` continues an
    existing sequence without extending the clarification TTL.
    """
    missing_value = ",".join(missing_fields)
    steps_value = None if steps is None else ",".join(steps)
    with connect() as conn:
        if keep_created_at:
            conn.execute(
                "UPDATE pending_clarifications SET ticket_code = ?, missing_fields = ?, "
                "question_steps = ? WHERE node_id = ?",
                (ticket_code, missing_value, steps_value, node_id),
            )
        else:
            conn.execute(
                "INSERT INTO pending_clarifications"
                "(node_id, ticket_code, missing_fields, question_steps, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
                "ticket_code = excluded.ticket_code, missing_fields = excluded.missing_fields, "
                "question_steps = excluded.question_steps, created_at = excluded.created_at",
                (
                    node_id, ticket_code, missing_value, steps_value,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.commit()


def get_pending_clarification(node_id: str, max_age_seconds: int = 600) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_clarifications WHERE node_id = ?", (node_id,)
        ).fetchone()
    if row is None:
        return None
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])
    if not (timedelta(0) <= age <= timedelta(seconds=max_age_seconds)):
        return None
    result = dict(row)
    result["missing_fields"] = [field for field in result["missing_fields"].split(",") if field]
    raw_steps = result.get("question_steps") or ""
    result["steps"] = [step for step in raw_steps.split(",") if step]
    return result


def clear_pending_clarification(node_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pending_clarifications WHERE node_id = ?", (node_id,))
        conn.commit()


def get_person(node_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM people WHERE node_id = ?", (node_id,)).fetchone()
    return dict(row) if row is not None else None


def upsert_person(node_id: str, display_name: str | None = None,
                   last_known_address: str | None = None) -> None:
    """Accumulates known info about a sender — only overwrites a field when a
    new non-empty value is given, so one source (e.g. a later message with no
    name) never blanks out what a prior source already taught us."""
    existing = get_person(node_id)
    display_name = display_name or (existing["display_name"] if existing else None)
    last_known_address = last_known_address or (existing["last_known_address"] if existing else None)
    with connect() as conn:
        conn.execute(
            "INSERT INTO people(node_id, display_name, last_known_address, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(node_id) DO UPDATE SET "
            "display_name = excluded.display_name, last_known_address = excluded.last_known_address, "
            "updated_at = excluded.updated_at",
            (node_id, display_name, last_known_address, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def append_clarification(ticket_code: str, *, question: str | None = None,
                         response: str | None = None) -> None:
    """Append one follow-up question or raw sender reply, keeping earlier turns."""
    additions = {}
    if question is not None:
        additions["clarification_question"] = question
    if response is not None:
        additions["clarification_response"] = response
    if not additions:
        return
    with connect() as conn:
        row = conn.execute(
            "SELECT clarification_question, clarification_response FROM tickets WHERE ticket_code = ?",
            (ticket_code,),
        ).fetchone()
        if row is None:
            return
        assignments = []
        values = []
        for column, text in additions.items():
            current = row[column]
            assignments.append(f"{column} = ?")
            values.append(text if not current else f"{current}\n{text}")
        conn.execute(
            f"UPDATE tickets SET {', '.join(assignments)} WHERE ticket_code = ?",
            (*values, ticket_code),
        )
        conn.commit()


def update_ticket_fields(ticket_code: str, **fields) -> None:
    allowed = {
        "location", "injured_count", "what_happened",
        "clarification_question", "clarification_response",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    with connect() as conn:
        conn.execute(
            f"UPDATE tickets SET {set_clause} WHERE ticket_code = ?",
            (*updates.values(), ticket_code),
        )
        conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
