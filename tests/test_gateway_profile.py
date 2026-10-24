"""Integration tests for the person-profile autofill flow: a known sender's
last-known address fills in a new ticket's missing location, and the
simulator's PRIVATE_APP JSON (position + name) updates the profile."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import gateway  # noqa: E402
import triage  # noqa: E402
from triage import TriageResult  # noqa: E402


class FakeInterface:
    myInfo = SimpleNamespace(my_node_num=0x12345678)

    def __init__(self):
        self.sent = []

    def sendText(self, text, **kwargs):
        self.sent.append((text, kwargs))


class ProfileAutofillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "tickets.db"))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        db.init_db()
        self.interface = FakeInterface()
        self.sender = "!a1b2c3d4"
        self.sleep_patch = patch.object(gateway.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)
        self.ai_question_patch = patch.object(
            gateway, "_ai_clarification_question",
            side_effect=lambda missing, text: gateway._clarification_question(missing),
        )
        self.ai_question_patch.start()
        self.addCleanup(self.ai_question_patch.stop)
        self.gps_question_patch = patch.object(
            gateway, "_ai_gps_detail_question",
            side_effect=lambda step, text, known=None: gateway._gps_detail_question(step),
        )
        self.gps_question_patch.start()
        self.addCleanup(self.gps_question_patch.stop)

    def make_packet(self, text, packet_id=100):
        return {
            "from": 0xA1B2C3D4, "fromId": self.sender, "id": packet_id, "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text},
        }

    def make_simulator_packet(self, payload_json, packet_id=200):
        return {
            "from": 0xA1B2C3D4, "fromId": self.sender, "id": packet_id, "channel": 0,
            "decoded": {"portnum": "PRIVATE_APP", "payload": payload_json.encode()},
        }

    def test_known_address_autofills_missing_location(self):
        db.upsert_person(self.sender, last_known_address="412 Oak St")
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", injured_count=1)):
            gateway.on_receive(self.make_packet("help"), self.interface)

        ticket = db.list_tickets()[0]
        self.assertEqual(ticket["location"], "412 Oak St")
        # The saved address fills location, and the injury count is known.
        # There is still no GPS fix, so the sender is asked for a landmark.
        self.assertEqual(len(self.interface.sent), 2)
        self.assertIn("landmark", self.interface.sent[1][0].lower())
        self.assertEqual(db.get_pending_clarification(self.sender)["steps"], ["landmark"])

    def test_unknown_sender_gets_no_autofill(self):
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", injured_count=1)):
            gateway.on_receive(self.make_packet("help"), self.interface)

        ticket = db.list_tickets()[0]
        self.assertIsNone(ticket["location"])

    def test_new_location_updates_profile_for_next_time(self):
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="Behind the mill", injured_count=0)):
            gateway.on_receive(self.make_packet("help"), self.interface)

        self.assertEqual(db.get_person(self.sender)["last_known_address"], "Behind the mill")

    def test_clarification_reply_location_also_updates_profile(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="412 Oak St", injured_count=1)):
            gateway.on_receive(self.make_packet("412 Oak St, 1 hurt", packet_id=2), self.interface)

        self.assertEqual(db.get_person(self.sender)["last_known_address"], "412 Oak St")

    def test_simulator_packet_saves_position_and_name(self):
        gateway.on_simulator_position(
            self.make_simulator_packet(
                '{"id":"!virt0003","name":"Bertha Wigglesworth","lat":34.51867,"lon":-83.98231}'
            ),
            self.interface,
        )
        position = db.get_node_position(self.sender)
        self.assertEqual((position["latitude"], position["longitude"]), (34.51867, -83.98231))
        self.assertEqual(db.get_person(self.sender)["display_name"], "Bertha Wigglesworth")

    def test_simulator_packet_from_own_node_is_ignored(self):
        packet = self.make_simulator_packet('{"lat":1,"lon":1}')
        packet["from"] = self.interface.myInfo.my_node_num
        packet["fromId"] = "!12345678"
        gateway.on_simulator_position(packet, self.interface)
        self.assertIsNone(db.get_node_position("!12345678"))


if __name__ == "__main__":
    unittest.main()
