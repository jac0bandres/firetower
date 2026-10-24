"""Clarification storage and additive migration tests using temporary databases."""

import json
import sqlite3
import sys
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
