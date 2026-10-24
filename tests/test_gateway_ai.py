"""Unit tests for the !ai command routing in gateway.py: never becomes a
ticket, always replies by DM (never broadcast), and respects the chunk
limit/byte budget on the radio link."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_assistant  # noqa: E402
import db  # noqa: E402
import gateway  # noqa: E402


class FakeInterface:
    myInfo = SimpleNamespace(my_node_num=0x12345678)

    def __init__(self):
        self.sent = []

    def sendText(self, text, **kwargs):
        self.sent.append((text, kwargs))


class AiCommandTests(unittest.TestCase):
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
        self.sleep_patch = patch.object(gateway.time, "sleep")
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def make_packet(self, text, packet_id=100):
        return {
            "from": 0xA1B2C3D4, "fromId": self.sender, "id": packet_id, "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text},
            "raw": object(),
        }

    def test_ai_command_replies_by_dm_and_creates_no_ticket(self):
        with patch.object(ai_assistant, "answer", return_value="Two emergencies are open.") as answer:
            gateway.on_receive(self.make_packet("!ai how many open emergencies?"), self.interface)

        answer.assert_called_once_with(self.sender, "how many open emergencies?")
        self.assertEqual(db.list_tickets(), [])
        self.assertEqual(len(self.interface.sent), 1)
        text, kwargs = self.interface.sent[0]
        self.assertTrue(text.startswith("AI "))
        self.assertEqual(kwargs["destinationId"], self.sender)
        self.assertNotIn("broadcast", str(kwargs).lower())

    def test_ai_command_is_case_insensitive_and_strips_question(self):
        with patch.object(ai_assistant, "answer", return_value="ok") as answer:
            gateway.on_receive(self.make_packet("!AI   what happened at the bridge"), self.interface)
        answer.assert_called_once_with(self.sender, "what happened at the bridge")

    def test_bare_ai_command_without_question_does_not_call_model(self):
        with patch.object(ai_assistant, "answer") as answer:
            gateway.on_receive(self.make_packet("!ai"), self.interface)
        answer.assert_not_called()
        self.assertEqual(len(self.interface.sent), 1)
        self.assertIn("Ask a question", self.interface.sent[0][0])

    def test_long_reply_is_chunked_within_byte_budget(self):
        long_reply = "word " * 200
        with patch.object(ai_assistant, "answer", return_value=long_reply):
            gateway.on_receive(self.make_packet("!ai tell me everything"), self.interface)

        self.assertLessEqual(len(self.interface.sent), gateway.AI_MAX_CHUNKS)
        for text, _ in self.interface.sent:
            self.assertLessEqual(len(text.encode("utf-8")), gateway.MAX_CHUNK_BYTES)
            self.assertTrue(text.startswith("AI "))

    def test_ai_command_does_not_duplicate_on_retry(self):
        with patch.object(ai_assistant, "answer", return_value="ok") as answer:
            packet = self.make_packet("!ai question", packet_id=200)
            gateway.on_receive(packet, self.interface)
            gateway.on_receive(packet, self.interface)
        answer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
