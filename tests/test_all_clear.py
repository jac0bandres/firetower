"""Tickets close only when the sender says the situation itself is over."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import gateway  # noqa: E402
import triage  # noqa: E402
from triage import TriageResult  # noqa: E402

SENDER = "!a1b2c3d7"


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        monkeypatch.setattr(db, "DB_PATH", str(Path(directory) / "t.db"))
        db.init_db()
        monkeypatch.setattr(gateway.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(gateway, "_ai_clarification_question",
                            lambda missing, raw: gateway._clarification_question(missing))
        db.save_node_position(SENDER, 34.69, -84.48,
                              __import__("datetime").datetime.now(
                                  __import__("datetime").timezone.utc).isoformat(), "{}")
        yield


class Radio:
    myInfo = SimpleNamespace(my_node_num=0x12345678)

    def __init__(self):
        self.sent = []

    def sendText(self, text, **kwargs):
        self.sent.append(text)


def packet(text, packet_id):
    return {"from": 0xA1B2C3D7, "fromId": SENDER, "id": packet_id, "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text}}


def report_fire(radio):
    with patch.object(triage, "triage", return_value=TriageResult(
            urgency="EMERGENCY", location="Shelter", what_happened="kitchen fire spreading")):
        gateway.on_receive(packet("fire in the shelter kitchen, spreading to supply room", 1), radio)


@pytest.mark.parametrize("text", [
    "all clear, fire's out", "false alarm sorry", "never mind we're ok now",
    "the fire is out, everyone's ok", "we don't need help anymore",
])
def test_all_clear_wording_is_recognised(text):
    assert gateway._is_all_clear(text, TriageResult(urgency="ROUTINE"))


@pytest.mark.parametrize("text", [
    "Nobody hurt, everyone is outside now",
    "fire spreading, we're ok now but the roof is going",
])
def test_no_injuries_or_urgent_wording_does_not_close(text):
    urgency = "ROUTINE" if text.startswith("Nobody") else "EMERGENCY"
    assert not gateway._is_all_clear(text, TriageResult(urgency=urgency))


def test_no_injuries_reply_keeps_the_fire_ticket_open():
    radio = Radio()
    report_fire(radio)
    with patch.object(triage, "extract_with_model",
                      return_value=TriageResult(urgency="PRIORITY", injured_count=0)):
        gateway.on_receive(packet("Nobody hurt, everyone is outside now", 2), radio)
    ticket = db.list_tickets()[0]
    assert ticket["status"] == "open"
    assert ticket["injured_count"] == 0


def test_all_clear_reply_to_a_follow_up_closes_the_ticket():
    radio = Radio()
    report_fire(radio)
    with patch.object(triage, "extract_with_model",
                      return_value=TriageResult(urgency="ROUTINE", injured_count=0)):
        gateway.on_receive(packet("all clear, fire's out, nobody hurt", 2), radio)
    ticket = db.list_tickets()[0]
    assert ticket["status"] == "closed"
    assert ticket["injured_count"] == 0
    assert "all clear" in ticket["clarification_response"]
    assert db.get_pending_clarification(SENDER) is None
    assert "Closed: you reported all clear" in radio.sent[-1]
    assert len(db.list_tickets()) == 1


def test_later_all_clear_closes_newest_ticket_instead_of_opening_one():
    radio = Radio()
    report_fire(radio)
    db.clear_pending_clarification(SENDER)
    with patch.object(triage, "triage", return_value=TriageResult(urgency="ROUTINE")):
        gateway.on_receive(packet("update: the fire is out, we're fine now", 3), radio)
    tickets = db.list_tickets()
    assert len(tickets) == 1
    assert tickets[0]["status"] == "closed"


def test_all_clear_with_no_active_ticket_is_stored_as_a_ticket():
    radio = Radio()
    with patch.object(triage, "triage", return_value=TriageResult(urgency="ROUTINE")):
        gateway.on_receive(packet("all clear here", 4), radio)
    assert len(db.list_tickets()) == 1
