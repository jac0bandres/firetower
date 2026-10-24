"""Exercise the packet shapes emitted by the Meshtastic Python client."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import db
import gateway
import meshtastic_packets as packets
import triage
from triage import TriageResult


class FakeInterface:
    myInfo = SimpleNamespace(my_node_num=0x12345678)

    def __init__(self):
        self.sent = []

    def sendText(self, text, **kwargs):
        self.sent.append((text, kwargs))


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = str(Path(self.temp.name) / "tickets.db")
        self.path_patch = patch.object(db, "DB_PATH", self.db_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        db.init_db()
        self.interface = FakeInterface()
        self.sender = "!a1b2c3d4"
        self.position_packet = {
            "from": 0xA1B2C3D4, "fromId": self.sender, "id": 41, "channel": 0,
            "decoded": {"portnum": "POSITION_APP", "payload": b"position bytes",
                        "position": {"latitudeI": 356000000, "longitudeI": -825000000,
                                     "raw": object()}},
            "raw": object(),
        }
        self.text_packet = {
            "from": 0xA1B2C3D4, "fromId": self.sender, "id": 42, "channel": 0,
            "hopStart": 3, "hopLimit": 1,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"fire at camp",
                        "text": "fire at camp"},
            "raw": object(),
        }

    def test_position_then_text_snapshots_location_and_packet(self):
        gateway.on_position(self.position_packet, self.interface)
        with patch.object(gateway.triage, "triage", return_value=TriageResult(
            urgency="EMERGENCY", what_happened="Camp fire")):
            gateway.on_receive(self.text_packet, self.interface)
            gateway.on_receive(self.text_packet, self.interface)
        rows = db.list_tickets()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["latitude"], rows[0]["longitude"]), (35.6, -82.5))
        self.assertEqual(rows[0]["hop_count"], 2)
        snapshot = json.loads(rows[0]["meshtastic_packet_json"])
        self.assertEqual(snapshot["decoded"]["payload"]["encoding"], "base64")
        self.assertNotIn("raw", snapshot)
        self.assertEqual(self.interface.sent, [
            ("FT FT-0001 received", {"destinationId": self.sender, "channelIndex": 0})
        ])

    def test_missing_or_stale_position_does_not_pin_ticket(self):
        db.save_node_position(self.sender, 35.6, -82.5,
                              "2020-01-01T00:00:00+00:00", "{}")
        with patch.object(gateway.triage, "triage", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.text_packet, self.interface)
        self.assertIsNone(db.list_tickets()[0]["latitude"])

    def test_non_text_and_own_packets_are_ignored(self):
        with patch.object(gateway.triage, "triage") as triage:
            gateway.on_receive(self.position_packet, self.interface)
            own = dict(self.text_packet, **{"from": self.interface.myInfo.my_node_num})
            gateway.on_receive(own, self.interface)
            triage.assert_not_called()
        self.assertEqual(db.list_tickets(), [])

    def test_position_parser_requires_both_valid_coordinates(self):
        self.assertEqual(packets.position(self.position_packet), (35.6, -82.5))
        self.assertIsNone(packets.position({"decoded": {"position": {"latitudeI": 10}}}))

    def test_mock_tickets_match_database_and_packet_identity(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "mock_tickets.json"
        tickets = json.loads(fixture.read_text())
        with db.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        for ticket in tickets:
            self.assertEqual(set(ticket), columns - {"id"})
            snapshot = json.loads(ticket["meshtastic_packet_json"])
            self.assertEqual(snapshot["fromId"], ticket["node_id"])
            self.assertEqual(str(snapshot["id"]), ticket["packet_id"])
            self.assertEqual(snapshot["decoded"]["text"], ticket["raw_text"])
            if ticket["latitude"] is not None:
                position_packet = json.loads(ticket["position_packet_json"])
                self.assertEqual(position_packet["fromId"], ticket["node_id"])
                self.assertEqual(packets.position(position_packet),
                                 (ticket["latitude"], ticket["longitude"]))

    def test_triage_keeps_keyword_floor_when_local_model_is_unavailable(self):
        with patch.object(triage._client, "chat", side_effect=ConnectionError("offline")):
            result = triage.triage("fire at camp", self.sender)
        self.assertEqual(result.urgency, "EMERGENCY")
        self.assertTrue(result.raised_by_keyword)


if __name__ == "__main__":
    unittest.main()
