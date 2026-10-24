"""Unit tests for the clarification follow-up flow in gateway.py: a new
EMERGENCY/PRIORITY ticket missing location or injured_count triggers a
follow-up DM, and the sender's next plain-text reply updates that same
ticket instead of creating a new one."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_assistant  # noqa: E402
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
        # _ai_clarification_question calls DigitalOcean by default — too
        # slow/non-deterministic for these tests (and would contend with a
        # live gateway process during real-hardware testing). Delegate to
        # the static template so existing assertions on question wording
        # still hold; _ai_clarification_question's own AI/fallback behavior
        # is covered separately below.
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

    def seed_gps(self):
        db.save_node_position(
            self.sender, 35.6, -82.5, datetime.now(timezone.utc).isoformat(), "{}",
        )

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
        self.assertEqual(pending["steps"], ["fields", "landmark", "address"])
        self.assertEqual(db.list_tickets()[0]["clarification_question"],
                         gateway._clarification_question(["location", "injured_count"]))
        self.assertIsNone(db.list_tickets()[0]["clarification_response"])

    def test_emergency_with_all_fields_asks_nothing(self):
        self.seed_gps()
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
        self.seed_gps()
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
        self.seed_gps()
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("not sure, hard to tell", packet_id=2), self.interface)

        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertIn("Thanks, your reply is on the ticket", self.interface.sent[-1][0])
        ticket = db.list_tickets()[0]
        self.assertEqual(ticket["clarification_response"], "not sure, hard to tell")
        self.assertIsNone(ticket["location"])
        self.assertIsNone(ticket["injured_count"])
        self.assertTrue(ticket["clarification_question"])

    def test_missing_person_report_is_not_asked_for_injuries(self):
        self.seed_gps()
        text = "my son is missing near lake park since the storm red jacket pls look"
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="lake park",
                what_happened="my son is missing since the storm")):
            gateway.on_receive(self.make_packet(text), self.interface)

        self.assertEqual(len(self.interface.sent), 1)
        self.assertIsNone(db.get_pending_clarification(self.sender))

    def test_undescribed_missing_person_gets_last_seen_question(self):
        self.seed_gps()
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="lake park")):
            gateway.on_receive(self.make_packet("my daughter is missing near lake park"),
                               self.interface)

        self.assertEqual(self.interface.sent[1][0].split(": ", 1)[1],
                         gateway._gps_detail_question("last_seen"))
        with patch.object(triage, "extract_with_model", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("this morning, blue raincoat", packet_id=2),
                               self.interface)
        ticket = db.list_tickets()[0]
        self.assertEqual(ticket["clarification_response"], "this morning, blue raincoat")
        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertEqual(len(db.list_tickets()), 1)

    def test_unrelated_urgent_report_opens_new_ticket(self):
        # FT-0062: a house-fire ticket waiting on an injury count swallowed a
        # missing-person report from the same radio as its "reply".
        self.seed_gps()
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="88 elm ave", what_happened="house on fire")):
            gateway.on_receive(self.make_packet("HOUSE ON FIRE 88 elm ave", packet_id=1),
                               self.interface)

        report = "my son is missing near lake park since the storm red jacket pls look"
        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="lake park")), \
                patch.object(triage, "triage", return_value=TriageResult(
                    urgency="EMERGENCY", location="lake park",
                    what_happened="son missing")):
            gateway.on_receive(self.make_packet(report, packet_id=2), self.interface)

        tickets = {ticket["ticket_code"]: ticket for ticket in db.list_tickets()}
        self.assertEqual(len(tickets), 2)
        self.assertIsNone(tickets["FT-0001"]["clarification_response"])
        self.assertEqual(tickets["FT-0002"]["raw_text"], report)
        self.assertEqual(tickets["FT-0002"]["location"], "lake park")
        self.assertIn("FT-0002 received", self.interface.sent[-1][0])

    def test_urgent_reply_about_same_place_stays_on_ticket(self):
        self.seed_gps()
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="88 elm ave")):
            gateway.on_receive(self.make_packet("HOUSE ON FIRE 88 elm ave", packet_id=1),
                               self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="88 Elm Ave")):
            gateway.on_receive(self.make_packet("still burning at 88 Elm Ave", packet_id=2),
                               self.interface)

        tickets = db.list_tickets()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["clarification_response"], "still burning at 88 Elm Ave")

    def test_landmark_answer_never_splits(self):
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="the old mill", injured_count=1)):
            gateway.on_receive(self.make_packet("fire at the mill", packet_id=1), self.interface)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="water tower")):
            gateway.on_receive(self.make_packet("by the water tower, fire spreading",
                                                packet_id=2), self.interface)

        self.assertEqual(len(db.list_tickets()), 1)
        self.assertEqual(db.list_tickets()[0]["clarification_response"],
                         "by the water tower, fire spreading")

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
        self.seed_gps()
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
        self.seed_gps()
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help", packet_id=1), self.interface)
        self.interface.sent.clear()

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="Long road " * 120, injured_count=2)):
            gateway.on_receive(self.make_packet("details", packet_id=2), self.interface)

        self.assertLessEqual(len(self.interface.sent), gateway.AI_MAX_CHUNKS)
        for text, kwargs in self.interface.sent:
            self.assertLessEqual(len(text.encode("utf-8")), gateway.MAX_CHUNK_BYTES)
            self.assertEqual(kwargs["destinationId"], self.sender)
            self.assertEqual(kwargs["channelIndex"], 0)
            self.assertTrue(kwargs["wantAck"])

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
        pending = db.get_pending_clarification(self.sender)
        self.assertEqual(pending["steps"], ["fields", "landmark", "address"])
        self.assertEqual(len(db.list_tickets()), 1)

    def test_gps_present_asks_one_question_and_stops(self):
        self.seed_gps()
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        pending = db.get_pending_clarification(self.sender)
        self.assertEqual(pending["steps"], ["fields"])
        self.assertEqual(len(self.interface.sent), 2)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="Hwy 52", injured_count=1)):
            gateway.on_receive(self.make_packet("hwy 52, 1 hurt", packet_id=2), self.interface)

        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertEqual(len(db.list_tickets()), 1)
        sent_text = " ".join(text for text, _ in self.interface.sent).lower()
        self.assertNotIn("landmark", sent_text)
        self.assertNotIn("street or address", sent_text)

    def test_missing_gps_replies_stay_on_the_same_ticket(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)

        self.assertEqual(
            db.get_pending_clarification(self.sender)["steps"],
            ["fields", "landmark", "address"],
        )

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="the old mill")):
            gateway.on_receive(self.make_packet("the old mill", packet_id=2), self.interface)

        self.assertEqual(len(db.list_tickets()), 1)
        self.assertEqual(db.list_tickets()[0]["location"], "the old mill")
        self.assertIn("landmark", self.interface.sent[-1][0].lower())
        self.assertEqual(
            db.get_pending_clarification(self.sender)["steps"],
            ["landmark", "address", "injured_count"],
        )

        with patch.object(triage, "extract_with_model", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("water tower", packet_id=3), self.interface)

        self.assertEqual(len(db.list_tickets()), 1)
        self.assertIn("street or address", self.interface.sent[-1][0].lower())
        self.assertEqual(db.list_tickets()[0]["location"], "the old mill")

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="412 Oak St", injured_count=2)):
            gateway.on_receive(self.make_packet("412 Oak St, 2 hurt", packet_id=4), self.interface)

        tickets = db.list_tickets()
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["location"], "412 Oak St")
        self.assertEqual(tickets[0]["injured_count"], 2)
        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertEqual(tickets[0]["clarification_question"], "\n".join([
            gateway._clarification_question(["location", "injured_count"]),
            gateway._gps_detail_question("landmark"),
            gateway._gps_detail_question("address"),
        ]))
        self.assertEqual(
            tickets[0]["clarification_response"],
            "the old mill\nwater tower\n412 Oak St, 2 hurt",
        )
        self.assertIn("updated", self.interface.sent[-1][0].lower())
        delay_calls = [
            call for call in gateway.time.sleep.call_args_list
            if call.args == (gateway.CLARIFICATION_SEND_DELAY_SECONDS,)
        ]
        self.assertEqual(len(delay_calls), 3)
        for text, kwargs in self.interface.sent:
            self.assertLessEqual(len(text.encode("utf-8")), gateway.MAX_CHUNK_BYTES)
            self.assertEqual(kwargs["destinationId"], self.sender)
            self.assertLessEqual(text.count("?"), 1)
            self.assertNotIn("🚒", text)

    def test_missing_gps_vague_place_asks_landmark_then_address(self):
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="PRIORITY", location="the old mill", injured_count=2)):
            gateway.on_receive(self.make_packet("fire at the mill", packet_id=1), self.interface)

        self.assertEqual(db.get_pending_clarification(self.sender)["steps"], ["landmark", "address"])
        self.assertIn("landmark", self.interface.sent[1][0].lower())
        self.assertEqual(len(self.interface.sent), 2)

        with patch.object(triage, "extract_with_model", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("water tower", packet_id=2), self.interface)

        self.assertEqual(len(db.list_tickets()), 1)
        self.assertEqual(db.get_pending_clarification(self.sender)["steps"], ["address"])
        self.assertEqual(db.list_tickets()[0]["clarification_question"], "\n".join([
            gateway._gps_detail_question("landmark"),
            gateway._gps_detail_question("address"),
        ]))
        self.assertEqual(db.list_tickets()[0]["clarification_response"], "water tower")

    def test_missing_gps_street_address_asks_only_a_landmark(self):
        with patch.object(triage, "triage", return_value=TriageResult(
                urgency="EMERGENCY", location="412 Oak St", injured_count=1)):
            gateway.on_receive(self.make_packet("fire at home", packet_id=1), self.interface)

        self.assertEqual(db.get_pending_clarification(self.sender)["steps"], ["landmark"])
        self.assertIn("landmark", self.interface.sent[1][0].lower())

        with patch.object(triage, "extract_with_model", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("the water tower", packet_id=2), self.interface)

        ticket = db.list_tickets()[0]
        self.assertEqual(len(db.list_tickets()), 1)
        self.assertEqual(ticket["location"], "412 Oak St")
        self.assertEqual(ticket["clarification_response"], "the water tower")
        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertIn("your reply is on the ticket", self.interface.sent[-1][0].lower())

    def test_late_gps_fix_stops_location_questions(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)
        db.attach_ticket_position(
            self.sender, "1", 35.6, -82.5, datetime.now(timezone.utc).isoformat(), "{}",
        )

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="the mill", injured_count=2)):
            gateway.on_receive(self.make_packet("the mill, 2 hurt", packet_id=2), self.interface)

        self.assertIsNone(db.get_pending_clarification(self.sender))
        self.assertEqual(len(db.list_tickets()), 1)
        self.assertNotIn("landmark", self.interface.sent[-1][0].lower())
        self.assertIn("updated", self.interface.sent[-1][0].lower())

    def test_expired_clarification_opens_a_new_ticket(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)
        expired = (
            datetime.now(timezone.utc) - timedelta(seconds=gateway.CLARIFICATION_TTL_SECONDS + 5)
        ).isoformat()
        with db.connect() as connection:
            connection.execute(
                "UPDATE pending_clarifications SET created_at = ? WHERE node_id = ?",
                (expired, self.sender),
            )
            connection.commit()

        with patch.object(triage, "triage", return_value=TriageResult(urgency="ROUTINE")):
            gateway.on_receive(self.make_packet("checking in later", packet_id=2), self.interface)

        tickets = db.list_tickets()
        self.assertEqual(len(tickets), 2)
        original = next(ticket for ticket in tickets if ticket["ticket_code"] == "FT-0001")
        self.assertIsNone(original["clarification_response"])

    def test_follow_up_does_not_extend_clarification_ttl(self):
        with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")):
            gateway.on_receive(self.make_packet("help fire", packet_id=1), self.interface)
        with db.connect() as connection:
            created = connection.execute(
                "SELECT created_at FROM pending_clarifications"
            ).fetchone()["created_at"]

        with patch.object(triage, "extract_with_model", return_value=TriageResult(
                urgency="EMERGENCY", location="the mill")):
            gateway.on_receive(self.make_packet("the mill", packet_id=2), self.interface)

        with db.connect() as connection:
            again = connection.execute(
                "SELECT created_at FROM pending_clarifications"
            ).fetchone()["created_at"]
        self.assertEqual(again, created)
        self.assertIsNotNone(db.get_pending_clarification(self.sender))


class ClarificationPlanTests(unittest.TestCase):
    def plan(self, has_gps, location, injured, urgency="EMERGENCY"):
        result = TriageResult(urgency=urgency, location=location, injured_count=injured)
        return gateway._clarification_steps(result, has_gps)

    def test_gps_present_stops_after_one_question(self):
        self.assertEqual(self.plan(True, None, None), ["fields"])
        self.assertEqual(self.plan(True, "Hwy 52", 2), [])

    def test_missing_gps_asks_for_place_details(self):
        self.assertEqual(self.plan(False, None, None), ["fields", "landmark", "address"])
        self.assertEqual(self.plan(False, "the old mill", 2, "PRIORITY"), ["landmark", "address"])
        self.assertEqual(self.plan(False, "412 Oak St", 1), ["landmark"])
        self.assertEqual(self.plan(False, "Hwy 52", 2), ["landmark"])

    def test_missing_person_skips_injury_count(self):
        def plan(text, has_gps, location="lake park"):
            result = TriageResult(urgency="EMERGENCY", location=location)
            return gateway._clarification_steps(result, has_gps, text)

        described = "my son is missing near lake park since the storm red jacket pls look"
        self.assertEqual(plan(described, True), [])
        self.assertEqual(plan(described, False), ["landmark", "address"])
        undescribed = "my daughter is missing near lake park"
        self.assertEqual(plan(undescribed, True), ["last_seen"])
        self.assertEqual(plan(undescribed, False), ["last_seen", "landmark", "address"])
        self.assertEqual(plan(undescribed, True, location=None), ["fields"])
        self.assertEqual(
            gateway._missing_fields(TriageResult(urgency="EMERGENCY"), undescribed),
            ["location"],
        )
        # Ordinary emergencies still need an injury count.
        self.assertEqual(plan("house fire, family inside", True), ["fields"])

    def test_routine_and_welfare_are_not_questioned(self):
        self.assertEqual(self.plan(False, None, None, "ROUTINE"), [])
        self.assertEqual(self.plan(False, None, None, "WELFARE"), [])

    def test_static_gps_questions_fit_one_radio_question(self):
        for step in ("landmark", "address", "where", "injured_count", "last_seen"):
            question = gateway._gps_detail_question(step)
            self.assertLess(len(question), 80)
            self.assertTrue(question.endswith("?"))
            self.assertEqual(question.count("?"), 1)
            self.assertTrue(question.isascii())


class AiClarificationQuestionTests(unittest.TestCase):
    """_ai_clarification_question is mocked to the static template in the
    tests above (for speed/determinism); this tests its own AI/fallback
    behavior directly, same pattern as ai_assistant's model fallback tests."""

    def model_content(self, text, raw=False):
        return text if raw else json.dumps({"question": text})

    def test_uses_ai_generated_question_when_available(self):
        with patch.object(
            ai_assistant, "complete_raw",
            return_value=self.model_content("How many people are injured near the gas leak?"),
        ) as complete:
            question = gateway._ai_clarification_question(["injured_count"], "gas leak in house")
        self.assertEqual(question, "How many people are injured near the gas leak?")
        self.assertEqual(complete.call_args.kwargs["temperature"], 0)
        self.assertIn("gas leak in house", complete.call_args.args[0][0]["content"])

    def test_rejects_question_that_does_not_ask_for_missing_count(self):
        with patch.object(
            ai_assistant, "complete_raw",
            return_value=self.model_content("How bad is the gas smell?"),
        ):
            question = gateway._ai_clarification_question(["injured_count"], "gas leak in house")
        self.assertEqual(question, gateway._clarification_question(["injured_count"]))

    def test_accepts_yes_no_injury_question(self):
        for text in ["Is your son hurt?", "Is anyone hurt?", "Are any people injured?"]:
            with self.subTest(text=text), patch.object(
                    ai_assistant, "complete_raw", return_value=self.model_content(text)):
                self.assertEqual(gateway._ai_clarification_question(["injured_count"], "help"), text)

    def test_requires_every_missing_field(self):
        for text in ["Where are you?", "How many are hurt?", "How many supplies at your location?"]:
            with self.subTest(text=text), patch.object(
                    ai_assistant, "complete_raw", return_value=self.model_content(text)):
                question = gateway._ai_clarification_question(["location", "injured_count"], "help")
                self.assertEqual(question, gateway._clarification_question(["location", "injured_count"]))

    def test_accepts_specific_location_and_injury_questions(self):
        for text in ["Where are you and how many people are hurt?",
                     "What address and number of injured people?",
                     "Which landmark or intersection, and how many are injured?"]:
            with self.subTest(text=text), patch.object(
                    ai_assistant, "complete_raw", return_value=self.model_content(text)):
                self.assertEqual(gateway._ai_clarification_question(
                    ["location", "injured_count"], "help"), text)

    def test_rejects_oversized_or_invalid_questions(self):
        for text in ["Where " + "exactly " * 20 + "are you?", "Where are you", None,
                     "Where are you? What happened?", "Where\nare you?", "Where are you? 🚒"]:
            with self.subTest(text=text), patch.object(
                    ai_assistant, "complete_raw", return_value=self.model_content(text)):
                self.assertEqual(gateway._ai_clarification_question(["location"], "help"),
                                 gateway._clarification_question(["location"]))

    def test_falls_back_to_template_when_model_fails(self):
        with patch.object(ai_assistant, "complete_raw", side_effect=RuntimeError("down")):
            question = gateway._ai_clarification_question(["location"], "fire!")
        self.assertEqual(question, gateway._clarification_question(["location"]))

    def test_falls_back_to_template_on_empty_response(self):
        with patch.object(ai_assistant, "complete_raw", return_value=self.model_content("   ")):
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
                    ai_assistant, "complete_raw", return_value=self.model_content(content, raw=True)):
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
                    ai_assistant, "complete_raw", return_value=self.model_content(content, raw=True)):
                self.assertEqual(gateway._ai_clarification_question(["injured_count"], "help"),
                                 gateway._clarification_question(["injured_count"]))

    def test_uses_ai_landmark_and_address_questions(self):
        with patch.object(
            ai_assistant, "complete_raw",
            return_value=self.model_content("What landmark is nearest to the fire?"),
        ) as complete:
            question = gateway._ai_gps_detail_question("landmark", "fire downtown", "the mill")
        self.assertEqual(question, "What landmark is nearest to the fire?")
        self.assertEqual(complete.call_args.kwargs["temperature"], 0)
        self.assertIn("fire downtown", complete.call_args.args[0][0]["content"])
        self.assertIn("the mill", complete.call_args.args[0][0]["content"])

        with patch.object(
            ai_assistant, "complete_raw",
            return_value=self.model_content("What street or address are you near?"),
        ):
            question = gateway._ai_gps_detail_question("address", "fire downtown")
        self.assertEqual(question, "What street or address are you near?")

    def test_gps_detail_question_falls_back_when_model_fails(self):
        with patch.object(ai_assistant, "complete_raw", side_effect=RuntimeError("down")):
            question = gateway._ai_gps_detail_question("address", "help")
        self.assertEqual(question, gateway._gps_detail_question("address"))

        for text in ["How many people are hurt?", "Where are you? What street?",
                     "What street are you on? 🚒", None]:
            with self.subTest(text=text), patch.object(
                    ai_assistant, "complete_raw", return_value=self.model_content(text)):
                self.assertEqual(
                    gateway._ai_gps_detail_question("landmark", "help"),
                    gateway._gps_detail_question("landmark"),
                )


if __name__ == "__main__":
    unittest.main()
