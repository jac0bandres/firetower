"""In-app team dispatch: keyword choice, storage, and the board route.

Dispatch only records teams on a ticket. It does not call 911 or any department.
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

import board  # noqa: E402
import db  # noqa: E402
import dispatch  # noqa: E402


@pytest.fixture
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "tickets.db"))
    monkeypatch.setattr(board, "MOCK_MODE", False)
    db.init_db()


def sample(**fields):
    ticket = {
        "ticket_code": "FT-0001",
        "node_id": "!node1",
        "packet_id": "100",
        "received_at": "2026-10-24T18:00:00+00:00",
        "raw_text": "checking in, all quiet",
        "urgency": "ROUTINE",
        "location": "Camp",
        "injured_count": 0,
        "raised_by_keyword": False,
        "priority_floor_applied": False,
    }
    ticket.update(fields)
    return ticket


def test_fire_report_selects_fire_only():
    teams = dispatch.suggest_teams(sample(
        raw_text="fire in the shelter kitchen",
        what_happened="Kitchen fire spreading to the supply room",
        urgency="EMERGENCY",
        injured_count=0,
    ))
    assert teams == ["fire"]


def test_police_and_injury_select_police_and_ems():
    teams = dispatch.suggest_teams(sample(
        raw_text="man with a gun, one person bleeding",
        urgency="EMERGENCY",
        injured_count=1,
    ))
    assert teams == ["police", "ems"]


def test_trapped_people_select_rescue_and_ems():
    teams = dispatch.suggest_teams(sample(
        raw_text="bridge washed out, car in the water, 2 people trapped",
        urgency="EMERGENCY",
        injured_count=2,
    ))
    assert teams == ["ems", "rescue"]


def test_unspecified_emergency_defaults_to_ems():
    assert dispatch.suggest_teams(sample(
        raw_text="need help at the trailhead",
        urgency="EMERGENCY",
        injured_count=None,
    )) == ["ems"]


def test_routine_check_in_selects_no_team():
    assert dispatch.suggest_teams(sample()) == []


def test_nobody_hurt_does_not_add_ems_to_a_fire():
    assert dispatch.suggest_teams(sample(
        raw_text="kitchen fire, nobody hurt",
        what_happened="Kitchen fire",
        urgency="EMERGENCY",
        injured_count=0,
    )) == ["fire"]


def test_firetower_does_not_match_fire():
    assert dispatch.suggest_teams(sample(raw_text="firetower relay check")) == []


def test_normalize_rejects_unknown_teams():
    with pytest.raises(ValueError):
        dispatch.normalize_teams(["sheriff"])
    with pytest.raises(ValueError):
        dispatch.normalize_teams([])


def test_dispatch_records_teams_and_assigns(temp_db):
    db.insert_ticket(sample(
        raw_text="fire in the kitchen",
        urgency="EMERGENCY",
        injured_count=0,
    ))

    result = board.dispatch_ticket("FT-0001")

    assert result["dispatched_teams"] == ["fire"]
    assert result["status"] == "assigned"
    assert result["suggested_teams"] == ["fire"]
    assert result["dispatched_at"]
    stored = db.list_tickets()[0]
    assert stored["dispatched_teams"] == "fire"
    assert stored["status"] == "assigned"
    assert stored["assigned_at"] == stored["dispatched_at"]


def test_dispatch_missing_ticket_is_404(temp_db):
    with pytest.raises(HTTPException) as caught:
        board.dispatch_ticket("FT-9999")
    assert caught.value.status_code == 404


def test_dispatch_without_a_team_is_400(temp_db):
    db.insert_ticket(sample())
    with pytest.raises(HTTPException) as caught:
        board.dispatch_ticket("FT-0001")
    assert caught.value.status_code == 400
    assert db.list_tickets()[0]["status"] == "open"
    assert db.list_tickets()[0]["dispatched_teams"] is None


def test_reopen_clears_dispatch_and_close_keeps_it(temp_db):
    db.insert_ticket(sample(raw_text="shots fired near the gate", urgency="EMERGENCY"))
    db.set_ticket_dispatch("FT-0001", ["police"])
    db.set_ticket_status("FT-0001", "closed")
    closed = db.list_tickets()[0]
    assert closed["status"] == "closed"
    assert closed["dispatched_teams"] == "police"

    db.set_ticket_status("FT-0001", "open")
    reopened = db.list_tickets()[0]
    assert reopened["status"] == "open"
    assert reopened["dispatched_teams"] is None
    assert reopened["dispatched_at"] is None


def test_migration_adds_dispatch_columns(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy.db"))
    legacy = db.SCHEMA.replace("    dispatched_teams TEXT,\n", "").replace(
        "    dispatched_at TEXT,\n", ""
    )
    with sqlite3.connect(db.DB_PATH) as connection:
        connection.executescript(legacy)
    db.init_db()
    with db.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(tickets)")}
    assert {"dispatched_teams", "dispatched_at"} <= columns


def test_ticket_api_includes_suggestions(temp_db):
    db.insert_ticket(sample(raw_text="need insulin soon", urgency="PRIORITY", injured_count=0))
    listed = board.tickets()
    assert listed[0]["suggested_teams"] == ["ems"]
    assert listed[0]["dispatched_teams"] == []


def test_sender_notice_names_teams_without_promises():
    import dispatch as _dispatch
    text = _dispatch.sender_notice(["rescue", "ems", "fire"])
    assert "Fire, EMS and Rescue" in text
    assert text.isascii()
    for banned in ("911", "on the way", "minutes", "arrive"):
        assert banned not in text.lower()


def test_dispatch_queues_notice_and_gateway_sends_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import board
    import db as _db
    import gateway as _gateway
    from fastapi.testclient import TestClient

    monkeypatch.setattr(_db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(board, "MOCK_MODE", False)
    monkeypatch.setattr(_gateway.time, "sleep", lambda seconds: None)
    _db.init_db()
    _db.insert_ticket({
        "ticket_code": "FT-0001", "node_id": "!7a29cc82", "packet_id": "1",
        "received_at": "2026-10-24T19:00:00+00:00", "raw_text": "house on fire, 2 hurt",
        "urgency": "EMERGENCY", "location": "88 elm ave", "what_happened": "house fire",
        "injured_count": 2, "raised_by_keyword": 0, "priority_floor_applied": 0,
        "meshtastic_packet_json": "{}",
    })

    response = TestClient(board.app).post("/api/tickets/FT-0001/dispatch")
    assert response.status_code == 200
    queued = _db.list_unsent_outbox()
    assert len(queued) == 1 and queued[0]["node_id"] == "!7a29cc82"

    class Radio:
        sent = []

        def sendText(self, text, **kwargs):
            self.sent.append((text, kwargs))
            return SimpleNamespace(id=1)

    radio = Radio()
    _gateway.send_outbox(radio)
    _gateway.send_outbox(radio)  # already sent: nothing goes out twice
    assert len(radio.sent) == 1
    text, kwargs = radio.sent[0]
    assert "FT-0001" in text and "Fire and EMS" in text
    assert kwargs["destinationId"] == "!7a29cc82" and kwargs["wantAck"]
    assert _db.list_unsent_outbox() == []
