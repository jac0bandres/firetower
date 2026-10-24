"""Delivery receipts, radio-safe text, and late or approximate GPS."""

import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
        return SimpleNamespace(id=0xABCDEF01)


def iso(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class DbCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "t.db"))
        path_patch.start()
        self.addCleanup(path_patch.stop)
        db.init_db()
        sleep_patch = patch.object(gateway.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.interface = FakeInterface()
        self.sender = "!7a29cc82"

    def text_packet(self, text, packet_id=1):
        return {
            "from": 0x7A29CC82, "fromId": self.sender, "id": packet_id, "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text},
        }

    def position_packet(self, lat, lon):
        return {
            "from": 0x7A29CC82, "fromId": self.sender, "id": 99, "channel": 0,
            "decoded": {"portnum": "POSITION_APP",
                        "position": {"latitude": lat, "longitude": lon}},
        }

    def report(self, text="house on fire by the square", **fields):
        result = TriageResult(urgency="EMERGENCY", location="by the square", **fields)
        with patch.object(triage, "triage", return_value=result), \
                patch.object(gateway, "_ai_clarification_question",
                             side_effect=lambda missing, raw: gateway._clarification_question(missing)), \
                patch.object(gateway, "_ai_gps_detail_question",
                             side_effect=lambda step, raw, known=None: gateway._gps_detail_question(step)):
            gateway.on_receive(self.text_packet(text), self.interface)
        return db.list_tickets()[0]


class DeliveryTests(DbCase):
    def test_every_send_requests_an_ack_and_logs_the_receipt(self):
        with self.assertLogs("gateway", level="INFO") as logs:
            gateway.send_ack(self.sender, "FT-0001", self.interface)
            _, kwargs = self.interface.sent[0]
            self.assertTrue(kwargs["wantAck"])
            callback = kwargs["onResponse"]
            # Meshtastic only passes plain ACKs to a callback named onAckNak.
            self.assertEqual(callback.__name__, "onAckNak")
            callback({"fromId": self.sender, "decoded": {"routing": {"errorReason": "NONE"}}})
            callback({"fromId": "!11111111", "decoded": {"routing": {"errorReason": "NONE"}}})
            callback({"fromId": "!12345678",
                      "decoded": {"routing": {"errorReason": "MAX_RETRANSMIT"}}})
        text = "\n".join(logs.output)
        self.assertIn("Sent ack FT-0001 to !7a29cc82 packet=abcdef01", text)
        self.assertIn("Delivered ack FT-0001 to !7a29cc82", text)
        self.assertIn("Relayed ack FT-0001 toward !7a29cc82 (ack from !11111111", text)
        self.assertIn("Delivery FAILED for ack FT-0001 to !7a29cc82: MAX_RETRANSMIT", text)

    def test_model_punctuation_is_sent_as_ascii(self):
        gateway.send_ai_reply(self.sender, "FT‑0001 is “open” — 2 hurt…",
                              self.interface)
        text = self.interface.sent[0][0]
        self.assertTrue(text.isascii())
        self.assertIn('FT-0001 is "open" - 2 hurt...', text)


class GpsTests(DbCase):
    def test_gps_after_the_text_attaches_to_that_ticket(self):
        ticket = self.report()
        self.assertIsNone(ticket["latitude"])
        self.assertEqual(db.get_pending_clarification(self.sender)["steps"][-1], "address")

        gateway.on_position(self.position_packet(34.52436, -84.01715), self.interface)

        ticket = db.list_tickets()[0]
        self.assertAlmostEqual(ticket["latitude"], 34.52436)
        self.assertAlmostEqual(ticket["longitude"], -84.01715)
        # The fix counts as real GPS: the next answer ends the location drill.
        with patch.object(triage, "extract_with_model",
                          return_value=TriageResult(urgency="EMERGENCY", injured_count=2)):
            gateway.on_receive(self.text_packet("2 hurt", packet_id=2), self.interface)
        self.assertIsNone(db.get_pending_clarification(self.sender))

    def test_late_gps_only_reaches_the_newest_recent_ticket(self):
        self.report()
        with db.connect() as conn:
            conn.execute("UPDATE tickets SET received_at = ?", (iso(-gateway.LATE_POSITION_WINDOW_SECONDS - 60),))
            conn.commit()
        gateway.on_position(self.position_packet(34.5, -84.0), self.interface)
        self.assertIsNone(db.list_tickets()[0]["latitude"])

    def test_old_fix_is_used_as_an_approximation(self):
        db.save_node_position(self.sender, 34.6, -84.1, iso(-3 * 3600), "{}")
        ticket = self.report()
        self.assertAlmostEqual(ticket["latitude"], 34.6)
        self.assertFalse(gateway._has_fresh_gps(ticket))
        # An approximation does not stop the location questions.
        self.assertIn("landmark", db.get_pending_clarification(self.sender)["steps"])

        gateway.on_position(self.position_packet(34.52436, -84.01715), self.interface)
        ticket = db.list_tickets()[0]
        self.assertAlmostEqual(ticket["latitude"], 34.52436)
        self.assertTrue(gateway._has_fresh_gps(ticket))

    def test_fresh_fix_is_not_replaced(self):
        db.save_node_position(self.sender, 34.6, -84.1, iso(-60), "{}")
        self.report()
        gateway.on_position(self.position_packet(35.0, -85.0), self.interface)
        self.assertAlmostEqual(db.list_tickets()[0]["latitude"], 34.6)


if __name__ == "__main__":
    unittest.main()
