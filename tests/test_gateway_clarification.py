"""Unit tests for the clarification follow-up flow in gateway.py: a new
EMERGENCY/PRIORITY ticket missing location or injured_count triggers a
follow-up DM, and the sender's next plain-text reply updates that same
ticket instead of creating a new one."""

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


class ClarificationTests(unittest.TestCase):
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
        # _ai_clarification_question calls the real local Ollama model by
        # default — too slow/non-deterministic for these tests (and would
        # contend with a live gateway process during real-hardware testing).
        # Delegate to the static template so existing assertions on question
        # wording still hold; _ai_clarification_question's own AI/fallback
        # behavior is covered separately below.
        self.ai_question_patch = patch.object(
            gateway, "_ai_clarification_question",
            side_effect=lambda missing, text: gateway._clarification_question(missing),
        )
        self.ai_question_patch.start()
        self.addCleanup(self.ai_question_patch.stop)

    def make_packet(self, text, packet_id=100):
        return {
            "from": 0xA1B2C3D4, "fromId": self.sender, "id": packet_id, "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text},
            "raw": object(),
        }

    def test_emergency_missing_both_fields_asks_both(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire"), self.interface)

        self.assertEqual(len(self.interface.sent), 2)
        ack_text, _ = self.interface.sent[0]
        question_text, kwargs = self.interface.sent[1]
        self.assertTrue(ack_text.startswith(f"{gateway.GATEWAY_NAME} FT-0001 received"))
        self.assertIn("location", question_text.lower())
        self.assertIn("hurt", question_text.lower())
        self.assertEqual(kwargs["destinationId"], self.sender)
        # A delay separates the ack from the question — sending two RELIABLE
        # messages back-to-back risked a collision (confirmed live).
        gateway.time.sleep.assert_any_call(gateway.CLARIFICATION_SEND_DELAY_SECONDS)
        pending = db.get_pending_clarification(self.sender)
        self.assertEqual(pending["ticket_code"], "FT-0001")
        self.assertEqual(set(pending["missing_fields"]), {"location", "injured_count"})

    def test_emergency_with_all_fields_asks_nothing(self):
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="Hwy 52", injured_count=2)):
            gateway.on_receive(self.make_packet("help fire"), self.interface)

        self.assertEqual(len(self.interface.sent), 1)
        self.assertIsNone(db.get_pending_clarification(self.sender))

    def test_routine_missing_fields_does_not_ask(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("when's the supply drop"), self.interface)

        self.assertIsNone(db.get_pending_clarification(self.sender))

    def test_reply_updates_same_ticket_not_a_new_one(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="Behind the old mill", injured_count=3)):
            gateway.on_receive(self.make_packet("behind the old mill, 3 hurt", packet_id=2), self.interface)

        tickets = db.list_tickets()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["location"], "Behind the old mill")
        self.assertEqual(tickets[0]["injured_count"], 3)
        self.assertIsNone(db.get_pending_clarification(self.sender))
        # ack, question, confirmation
        self.assertEqual(len(self.interface.sent), 3)
        self.assertIn("updated", self.interface.sent[2][0].lower())

    def test_reply_with_no_new_info_still_clears_pending(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("not sure, hard to tell", packet_id=2), self.interface)

        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertIn("noted", self.interface.sent[-1][0].lower())

    def test_ai_command_takes_precedence_over_pending_clarification(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        import ai_assistant
        with patch.object(ai_assistant, "answer", return_value="ok") as answer:
            gateway.on_receive(self.make_packet("!ai status", packet_id=2), self.interface)

        answer.assert_called_once()
        # The pending clarification is untouched by an !ai command.
        self.assertIsNotNone(db.get_pending_clarification(self.sender))


class AiClarificationQuestionTests(unittest.TestCase):
    """_ai_clarification_question is mocked to the static template in the
    tests above (for speed/determinism); this tests its own AI/fallback
    behavior directly, same pattern as ai_assistant's model fallback tests."""

    def make_response(self, text):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.message.content = text
        return response

    def test_uses_ai_generated_question_when_available(self):
        with patch.object(triage._client, "chat", return_value=self.make_response(
                "How bad is the gas smell?")):
            question = gateway._ai_clarification_question(["injured_count"], "gas leak in house")
        self.assertEqual(question, "How bad is the gas smell?")

    def test_falls_back_to_template_when_model_fails(self):
        with patch.object(triage._client, "chat", side_effect=RuntimeError("down")):
            question = gateway._ai_clarification_question(["location"], "fire!")
        self.assertEqual(question, gateway._clarification_question(["location"]))

    def test_falls_back_to_template_on_empty_response(self):
        with patch.object(triage._client, "chat", return_value=self.make_response("   ")):
            question = gateway._ai_clarification_question(["injured_count"], "help")
        self.assertEqual(question, gateway._clarification_question(["injured_count"]))

    def test_strips_leaked_thinking_trace(self):
        with patch.object(triage._client, "chat", return_value=self.make_response(
                "Reasoning about this...</think>\n\nHow many are hurt?")):
            question = gateway._ai_clarification_question(["injured_count"], "help")
        self.assertEqual(question, "How many are hurt?")


if __name__ == "__main__":
    unittest.main()
