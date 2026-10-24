"""Clarification storage and additive migration tests using temporary databases."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "tickets.db"))


@pytest.fixture
def ticket():
    return {
        "ticket_code": "FT-0001",
        "node_id": "!node1",
        "packet_id": "100",
        "received_at": "2026-10-24T14:02:11Z",
        "raw_text": "Fire at the shelter",
        "urgency": "EMERGENCY",
        "location": "Shelter",
        "injured_count": None,
        "raised_by_keyword": False,
        "priority_floor_applied": False,
    }


def test_new_tickets_default_to_null_clarifications(ticket):
    db.init_db()
    db.insert_ticket(ticket)

    stored = db.list_tickets()[0]
    assert stored["clarification_question"] is None
    assert stored["clarification_response"] is None


def test_insert_preserves_original_question_and_raw_response(ticket):
    db.init_db()
    ticket.update(
        clarification_question="How many people are hurt at the shelter?",
        clarification_response="  Nobody hurt — we're outside now\n",
    )
    assert db.insert_ticket(ticket) == "FT-0001"

    stored = db.list_tickets(status="open")[0]
    assert stored["clarification_question"] == ticket["clarification_question"]
    assert stored["clarification_response"] == ticket["clarification_response"]


def test_updates_preserve_exchange_and_protect_original_ticket(ticket):
    db.init_db()
    db.insert_ticket(ticket)
    question = "How many people are hurt at the shelter?"
    response = "Nobody hurt, everyone is outside now"
    db.update_ticket_fields("FT-0001", clarification_question=question)
    db.update_ticket_fields(
        "FT-0001", clarification_response=response, injured_count=0,
        raw_text="replacement", urgency="ROUTINE", status="closed",
    )

    stored = db.list_tickets()[0]
    assert stored["clarification_question"] == question
    assert stored["clarification_response"] == response
    assert stored["injured_count"] == 0
    assert stored["raw_text"] == ticket["raw_text"]
    assert stored["urgency"] == "EMERGENCY"
    assert stored["status"] == "open"


@pytest.mark.parametrize("existing_field", [None, "clarification_question", "clarification_response"])
def test_migration_preserves_existing_tickets_and_is_repeatable(ticket, existing_field):
    legacy_schema = db.SCHEMA.replace("    clarification_question TEXT,\n", "").replace(
        "    clarification_response TEXT,\n", ""
    )
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.executescript(legacy_schema)
        fields = tuple(ticket)
        connection.execute(
            f"INSERT INTO tickets ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
            tuple(ticket.values()),
        )
        if existing_field:
            connection.execute(f"ALTER TABLE tickets ADD COLUMN {existing_field} TEXT")
            connection.execute(f"UPDATE tickets SET {existing_field} = ?", ("Already stored",))

    db.init_db()
    db.init_db()

    stored_tickets = db.list_tickets()
    assert len(stored_tickets) == 1
    stored = stored_tickets[0]
    for field, value in ticket.items():
        assert stored[field] == value
    for field in ("clarification_question", "clarification_response"):
        assert stored[field] == ("Already stored" if field == existing_field else None)
    with db.connect() as connection:
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(tickets)")}
        for field in ("clarification_question", "clarification_response"):
            assert columns[field]["type"] == "TEXT"
            assert columns[field]["notnull"] == 0


def test_mock_tickets_include_clarification_fields():
    fixture_path = Path(__file__).parent.parent / "fixtures" / "mock_tickets.json"
    tickets = json.loads(fixture_path.read_text(encoding="utf-8"))

    for ticket in tickets:
        assert "clarification_question" in ticket
        assert "clarification_response" in ticket
    assert any(ticket["clarification_question"] and ticket["clarification_response"] for ticket in tickets)


def test_append_clarification_keeps_each_turn(ticket):
    db.init_db()
    db.insert_ticket(ticket)
    db.append_clarification("FT-0001", question="Where are you?")
    db.append_clarification("FT-0001", response="the mill")
    db.append_clarification("FT-0001", question="What landmark is nearest to you?")
    db.append_clarification("FT-0001", response="water tower")

    stored = db.list_tickets()[0]
    assert stored["clarification_question"] == "Where are you?\nWhat landmark is nearest to you?"
    assert stored["clarification_response"] == "the mill\nwater tower"
    assert stored["raw_text"] == ticket["raw_text"]


def test_pending_steps_round_trip_and_keep_the_original_ttl():
    db.init_db()
    db.set_pending_clarification("!node", "FT-0001", ["location", "injured_count"], ["fields", "landmark", "address"])
    pending = db.get_pending_clarification("!node")
    assert pending["steps"] == ["fields", "landmark", "address"]
    assert pending["missing_fields"] == ["location", "injured_count"]

    db.set_pending_clarification(
        "!node", "FT-0001", ["injured_count"], ["landmark", "address"], keep_created_at=True,
    )
    again = db.get_pending_clarification("!node")
    assert again["steps"] == ["landmark", "address"]
    assert again["missing_fields"] == ["injured_count"]
    assert again["created_at"] == pending["created_at"]


def test_pending_steps_column_migrates_onto_an_older_database():
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.execute(
            "CREATE TABLE pending_clarifications ("
            "node_id TEXT PRIMARY KEY, ticket_code TEXT NOT NULL, "
            "missing_fields TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO pending_clarifications(node_id, ticket_code, missing_fields, created_at) "
            "VALUES ('!node', 'FT-0001', 'location', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )

    db.init_db()

    with db.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(pending_clarifications)")}
    assert "question_steps" in columns
    pending = db.get_pending_clarification("!node")
    assert pending["missing_fields"] == ["location"]
    assert pending["steps"] == []
