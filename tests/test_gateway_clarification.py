"""Unit tests for the clarification follow-up flow in gateway.py: a new
EMERGENCY/PRIORITY ticket missing location or injured_count triggers a
follow-up DM, and the sender's next plain-text reply updates that same
ticket instead of creating a new one."""

import json
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
        self.assertIn("EMERGENCY; queued for coordinator review", ack_text)
        self.assertNotIn("dispatch", ack_text.lower())
        self.assertIn("location", question_text.lower())
        self.assertIn("hurt", question_text.lower())
        self.assertEqual(kwargs["destinationId"], self.sender)
        # A delay separates the ack from the question — sending two RELIABLE
        # messages back-to-back risked a collision (confirmed live).
        gateway.time.sleep.assert_any_call(gateway.CLARIFICATION_SEND_DELAY_SECONDS)
        pending = db.get_pending_clarification(self.sender)
        self.assertEqual(pending["ticket_code"], "FT-0001")
        self.assertEqual(set(pending["missing_fields"]), {"location", "injured_count"})
        self.assertEqual(db.list_tickets()[0]["clarification_question"],
                         gateway._clarification_question(["location", "injured_count"]))
        self.assertIsNone(db.list_tickets()[0]["clarification_response"])

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
        self.assertIn("location: Behind the old mill; injured: 3", self.interface.sent[2][0])
        self.assertIn("EMERGENCY; queued for coordinator review", self.interface.sent[2][0])
        self.assertEqual(tickets[0]["clarification_response"], "behind the old mill, 3 hurt")
        self.assertEqual(tickets[0]["clarification_question"],
                         gateway._clarification_question(["location", "injured_count"]))

    def test_reply_with_no_new_info_still_clears_pending(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("not sure, hard to tell", packet_id=2), self.interface)

        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertIn("Reply saved; location/injury fields unchanged", self.interface.sent[-1][0])
        ticket = db.list_tickets()[0]
        self.assertEqual(ticket["clarification_response"], "not sure, hard to tell")
        self.assertIsNone(ticket["location"])
        self.assertIsNone(ticket["injured_count"])
        self.assertTrue(ticket["clarification_question"])

    def test_question_is_saved_before_failed_send(self):
        def fail_question(node_id, ticket_code, note, interface, channel):
            self.assertEqual(db.list_tickets()[0]["clarification_question"], note)
            raise RuntimeError("radio unavailable")

        with patch.object(triage, "triage", return_value=TriageResult(urgency="PRIORITY")), \
                patch.object(gateway, "send_ticket_note", side_effect=fail_question):
            gateway.on_receive(self.make_packet("need help"), self.interface)

        self.assertTrue(db.list_tickets()[0]["clarification_question"])
        self.assertIsNotNone(db.get_pending_clarification(self.sender))

    def test_reply_only_updates_missing_fields_and_confirms_zero_injured(self):
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="PRIORITY", location="School")):
            gateway.on_receive(self.make_packet("need help", packet_id=1), self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="ROUTINE", location="Elsewhere", injured_count=0)):
            gateway.on_receive(self.make_packet("nobody hurt", packet_id=2), self.interface)

        ticket = db.list_tickets()[0]
        self.assertEqual(ticket["location"], "School")
        self.assertEqual(ticket["injured_count"], 0)
        self.assertIn("Updated: injured: 0. PRIORITY", self.interface.sent[-1][0])
        self.assertNotIn("Elsewhere", self.interface.sent[-1][0])

    def test_long_confirmation_stays_within_dm_byte_and_chunk_limits(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help", packet_id=1), self.interface)
        self.interface.sent.clear()

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="Long road " * 120, injured_count=2)):
            gateway.on_receive(self.make_packet("details", packet_id=2), self.interface)

        self.assertLessEqual(len(self.interface.sent), gateway.AI_MAX_CHUNKS)
        for text, kwargs in self.interface.sent:
            self.assertLessEqual(len(text.encode("utf-8")), gateway.MAX_CHUNK_BYTES)
            self.assertEqual(kwargs, {"destinationId": self.sender, "channelIndex": 0})

    def test_direct_ack_without_result_still_queues_for_review(self):
        gateway.send_ack(self.sender, "FT-0001", self.interface)
        self.assertIn("queued for coordinator review", self.interface.sent[0][0])

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

    def make_response(self, text, raw=False):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.message.content = text if raw else json.dumps({"question": text})
        return response

    def test_uses_ai_generated_question_when_available(self):
        with patch.object(triage._client, "chat", return_value=self.make_response(
                "How many people are injured near the gas leak?")) as chat:
            question = gateway._ai_clarification_question(["injured_count"], "gas leak in house")
        self.assertEqual(question, "How many people are injured near the gas leak?")
        self.assertEqual(chat.call_args.kwargs["options"], {"temperature": 0})
        self.assertIs(chat.call_args.kwargs["think"], False)
        self.assertEqual(chat.call_args.kwargs["format"], {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        })

    def test_rejects_question_that_does_not_ask_for_missing_count(self):
        with patch.object(triage._client, "chat", return_value=self.make_response(
                "How bad is the gas smell?")):
            question = gateway._ai_clarification_question(["injured_count"], "gas leak in house")
        self.assertEqual(question, gateway._clarification_question(["injured_count"]))

    def test_requires_every_missing_field(self):
        for text in ["Where are you?", "How many are hurt?", "How many supplies at your location?"]:
            with self.subTest(text=text), patch.object(
                    triage._client, "chat", return_value=self.make_response(text)):
                question = gateway._ai_clarification_question(["location", "injured_count"], "help")
                self.assertEqual(question, gateway._clarification_question(["location", "injured_count"]))

    def test_accepts_specific_location_and_injury_questions(self):
        for text in ["Where are you and how many people are hurt?",
                     "What address and number of injured people?",
                     "Which landmark or intersection, and how many are injured?"]:
            with self.subTest(text=text), patch.object(
                    triage._client, "chat", return_value=self.make_response(text)):
                self.assertEqual(gateway._ai_clarification_question(
                    ["location", "injured_count"], "help"), text)

    def test_rejects_oversized_or_invalid_questions(self):
        for text in ["Where " + "exactly " * 20 + "are you?", "Where are you", None,
                     "Where are you? What happened?", "Where\nare you?", "Where are you? 🚒"]:
            with self.subTest(text=text), patch.object(
                    triage._client, "chat", return_value=self.make_response(text)):
                self.assertEqual(gateway._ai_clarification_question(["location"], "help"),
                                 gateway._clarification_question(["location"]))

    def test_falls_back_to_template_when_model_fails(self):
        with patch.object(triage._client, "chat", side_effect=RuntimeError("down")):
            question = gateway._ai_clarification_question(["location"], "fire!")
        self.assertEqual(question, gateway._clarification_question(["location"]))

    def test_falls_back_to_template_on_empty_response(self):
        with patch.object(triage._client, "chat", return_value=self.make_response("   ")):
            question = gateway._ai_clarification_question(["injured_count"], "help")
        self.assertEqual(question, gateway._clarification_question(["injured_count"]))

    def test_rejects_prose_and_malformed_json(self):
        valid = json.dumps({"question": "How many people are hurt near the fire?"})
        for content in ["How many are hurt?", "Reasoning first " + valid,
                        valid + " Extra prose", "```json\n" + valid + "\n```",
                        '{"question": "How many are hurt?"', "[]", "null",
                        '{"question": false}', '{"question": "How many are hurt?", "reasoning": "x"}',
                        '{}', None]:
            with self.subTest(content=content), patch.object(
                    triage._client, "chat", return_value=self.make_response(content, raw=True)):
                self.assertEqual(gateway._ai_clarification_question(["injured_count"], "help"),
                                 gateway._clarification_question(["injured_count"]))

    def test_rejects_completed_and_unfinished_reasoning_traces(self):
        valid = json.dumps({"question": "How many people are hurt near the fire?"})
        for content in ["<think>Considering the report " + valid,
                        "<think>Reasoning</think>" + valid,
                        "Reasoning about this...</think>" + valid,
                        "<THINK>How many people are hurt?",
                        json.dumps({"question": "<think>How many people are hurt?"}),
                        json.dumps({"question": "How many are hurt?</think>"})]:
            with self.subTest(content=content), patch.object(
                    triage._client, "chat", return_value=self.make_response(content, raw=True)):
                self.assertEqual(gateway._ai_clarification_question(["injured_count"], "help"),
                                 gateway._clarification_question(["injured_count"]))


if __name__ == "__main__":
    unittest.main()
