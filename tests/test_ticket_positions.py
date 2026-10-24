"""Late GPS attachment is limited to an explicitly linked unlocated ticket."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "tickets.db"))
    db.init_db()


def insert_ticket(code="FT-0001", node_id="!node1", packet_id="100", **fields):
    ticket = {
        "ticket_code": code,
        "node_id": node_id,
        "packet_id": packet_id,
        "received_at": "2026-10-24T18:00:00+00:00",
        "raw_text": "Fire at the shelter",
        "urgency": "EMERGENCY",
        "location": "Shelter",
        "raised_by_keyword": False,
        "priority_floor_applied": False,
        "meshtastic_packet_json": '{"id":100,"rxTime":1792862400}',
        **fields,
    }
    db.insert_ticket(ticket)


def attach_position(node_id="!node1", packet_id="100"):
    db.attach_ticket_position(
        node_id, packet_id, 34.7, -84.4, "2026-10-24T18:00:05+00:00",
        '{"decoded":{"replyId":100},"rxTime":1792862405}',
    )


def test_attachment_changes_only_position_snapshot_on_exact_ticket():
    insert_ticket()
    original = db.list_tickets()[0]

    attach_position()

    assert db.list_tickets() == [{
        **original,
        "latitude": 34.7,
        "longitude": -84.4,
        "position_received_at": "2026-10-24T18:00:05+00:00",
        "position_packet_json": '{"decoded":{"replyId":100},"rxTime":1792862405}',
        "location_source": "gps",
    }]


def test_attachment_does_not_cross_senders_or_backfill_other_packets():
    insert_ticket()
    insert_ticket("FT-0002", node_id="!other", packet_id="100")
    insert_ticket("FT-0003", packet_id="101")

    attach_position()

    tickets = {ticket["ticket_code"]: ticket for ticket in db.list_tickets()}
    assert tickets["FT-0001"]["latitude"] == 34.7
    for code in ("FT-0002", "FT-0003"):
        assert tickets[code]["latitude"] is None
        assert tickets[code]["longitude"] is None
        assert tickets[code]["position_received_at"] is None
        assert tickets[code]["position_packet_json"] is None


@pytest.mark.parametrize("node_id,packet_id", [("!unknown", "100"), ("!node1", "999")])
def test_unmatched_attachment_leaves_tickets_unchanged(node_id, packet_id):
    insert_ticket()
    original = db.list_tickets()

    attach_position(node_id, packet_id)

    assert db.list_tickets() == original


@pytest.mark.parametrize("latitude,longitude", [(35.0, -85.0), (35.0, None), (None, -85.0)])
def test_existing_or_partial_coordinates_are_never_overwritten(latitude, longitude):
    insert_ticket(
        latitude=latitude, longitude=longitude,
        position_received_at="2026-10-24T17:59:00+00:00",
        position_packet_json='{"id":90}',
    )
    original = db.list_tickets()

    attach_position()

    assert db.list_tickets() == original


def test_duplicate_attachment_preserves_first_snapshot():
    insert_ticket()
    attach_position()
    original = db.list_tickets()

    db.attach_ticket_position("!node1", "100", 35.0, -85.0, "2026-10-24T18:01:00+00:00", "{}")

    assert db.list_tickets() == original
