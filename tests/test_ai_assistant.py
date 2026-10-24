"""Unit tests for ai_assistant.py — channel-context building, per-sender
threads, and the DigitalOcean reply path. db is exercised against a real
temp SQLite file."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import router  # noqa: E402
import ai_assistant  # noqa: E402


def make_openai_response(text: str):
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=text))]
    return response


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "test.db")
        monkeypatch.setattr(db, "DB_PATH", db_path)
        db.init_db()
        yield


def insert_sample_ticket(**overrides):
    ticket = {
        "ticket_code": "FT-0001",
        "node_id": "!sender",
        "packet_id": "1",
        "received_at": "2026-10-24T14:00:00Z",
        "raw_text": "fire at camp",
        "urgency": "EMERGENCY",
        "location": "Camp Road",
        "what_happened": "Campfire spreading",
        "injured_count": 0,
        "raised_by_keyword": False,
        "priority_floor_applied": False,
        "hop_count": 1,
        "latitude": None,
        "longitude": None,
        "position_received_at": None,
        "position_packet_json": None,
        "meshtastic_packet_json": "{}",
    }
    ticket.update(overrides)
    db.insert_ticket(ticket)


class TestBuildChannelContext:
    def test_empty_channel_says_so(self):
        assert "No messages" in ai_assistant.build_channel_context()

    def test_includes_ticket_details(self):
        insert_sample_ticket()
        context = ai_assistant.build_channel_context()
        assert "FT-0001" in context
        assert "EMERGENCY" in context
        assert "!sender" in context
        assert "Camp Road" in context

    def test_caps_to_context_limit(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "CONTEXT_TICKET_LIMIT", 2)
        for i in range(5):
            insert_sample_ticket(
                ticket_code=f"FT-{i:04d}", packet_id=str(i),
                received_at=f"2026-10-24T14:0{i}:00Z",
            )
        context = ai_assistant.build_channel_context()
        assert context.count("FT-") == 2
        # Most recent tickets are kept, not the earliest.
        assert "FT-0004" in context
        assert "FT-0000" not in context

    def test_context_preserves_zero_unknown_status_and_original_report(self):
        insert_sample_ticket()
        insert_sample_ticket(ticket_code="FT-0002", packet_id="2", injured_count=None)
        db.set_ticket_status("FT-0001", "closed")

        context = ai_assistant.build_channel_context()

        assert "injured_count=0" in context
        assert "injured_count=unknown" in context
        assert "status=closed" in context
        assert "received=2026-10-24T14:00:00Z" in context
        assert "report=fire at camp" in context
        assert "summary=Campfire spreading" in context

    def test_totals_include_tickets_outside_recent_context(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "CONTEXT_TICKET_LIMIT", 1)
        insert_sample_ticket()
        insert_sample_ticket(ticket_code="FT-0002", packet_id="2", urgency="PRIORITY")

        context = ai_assistant.build_channel_context()

        assert "all 2 tickets" in context
        assert "open: EMERGENCY=1, PRIORITY=1" in context
        assert context.count("FT-") == 1


def use_cloud(monkeypatch, replies):
    monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
    monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
    monkeypatch.setattr(router, "is_online", lambda force=False: True)
    pending = list(replies)
    calls = []

    def fake_cloud(messages):
        calls.append(messages)
        reply = pending.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ai_assistant, "_call_cloud", fake_cloud)
    return calls


class TestAnswer:
    def test_uses_cloud_and_persists_thread(self, monkeypatch):
        calls = use_cloud(monkeypatch, ["Two open emergencies."])

        reply = ai_assistant.answer("!asker", "how many open emergencies?")

        assert reply == "Two open emergencies."
        thread_id = db.get_or_create_ai_thread("!asker")
        history = db.list_ai_thread_messages(thread_id)
        assert [h["role"] for h in history] == ["user", "assistant"]
        assert history[0]["content"] == "how many open emergencies?"
        assert history[1]["content"] == "Two open emergencies."
        assert "The asking radio node is !asker" in calls[0][0]["content"]

    def test_second_question_continues_same_thread(self, monkeypatch):
        calls = use_cloud(monkeypatch, ["First answer.", "Second answer."])

        ai_assistant.answer("!asker", "question one")
        ai_assistant.answer("!asker", "question two")

        thread_id = db.get_or_create_ai_thread("!asker")
        history = db.list_ai_thread_messages(thread_id)
        assert len(history) == 4
        contents = [message["content"] for message in calls[1]]
        assert "question one" in contents
        assert "First answer." in contents
        assert calls[1][-1]["role"] == "user"
        assert "Question: question two" in calls[1][-1]["content"]

    def test_never_sends_system_messages(self, monkeypatch):
        # DigitalOcean agent endpoints answer HTTP 400 to any system message.
        calls = use_cloud(monkeypatch, ["First answer.", "Second answer."])

        ai_assistant.answer("!asker", "question one")
        ai_assistant.answer("!asker", "question two")

        for messages in calls:
            assert {message["role"] for message in messages} <= {"user", "assistant"}
            assert "Channel history:" in messages[-1]["content"]

    def test_strips_leaked_thinking_trace(self, monkeypatch):
        use_cloud(monkeypatch, [
            "Let me reason about this step by step...\nSo the answer is clear.</think>\n\nTwo open tickets."
        ])

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "Two open tickets."
        assert "reason about this" not in reply

    def test_does_not_call_a_local_model(self, monkeypatch):
        use_cloud(monkeypatch, [RuntimeError("cloud down")])

        reply = ai_assistant.answer("!asker", "question")

        assert reply == ai_assistant.UNAVAILABLE_REPLY

    def test_cloud_thinking_tags_are_stripped(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: True)
        monkeypatch.setattr(ai_assistant, "_call_cloud", lambda messages: "<THINK>internal</THINK>FT-0001 is open.")

        assert ai_assistant.answer("!asker", "status?") == "FT-0001 is open."

    def test_degrades_when_cloud_fails(self, monkeypatch):
        use_cloud(monkeypatch, [RuntimeError("down")])

        reply = ai_assistant.answer("!asker", "question")

        assert reply == ai_assistant.UNAVAILABLE_REPLY
        thread_id = db.get_or_create_ai_thread("!asker")
        history = db.list_ai_thread_messages(thread_id)
        assert history[-1]["content"] == ai_assistant.UNAVAILABLE_REPLY

    def test_different_senders_get_different_threads(self, monkeypatch):
        use_cloud(monkeypatch, ["ok", "ok"])

        ai_assistant.answer("!alice", "hi")
        ai_assistant.answer("!bob", "hi")

        assert db.get_or_create_ai_thread("!alice") != db.get_or_create_ai_thread("!bob")


class TestCloudTier:
    def test_unconfigured_cloud_does_not_use_a_local_model(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "")
        cloud_call = MagicMock(side_effect=AssertionError("cloud should not be called"))
        monkeypatch.setattr(ai_assistant, "_call_cloud", cloud_call)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == ai_assistant.UNAVAILABLE_REPLY
        cloud_call.assert_not_called()

    def test_configured_and_online_tries_cloud_first(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: True)
        monkeypatch.setattr(ai_assistant, "_call_cloud", lambda messages: "cloud answer")

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "cloud answer"

    def test_configured_but_offline_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: False)
        cloud_call = MagicMock(side_effect=AssertionError("cloud should not be called while offline"))
        monkeypatch.setattr(ai_assistant, "_call_cloud", cloud_call)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == ai_assistant.UNAVAILABLE_REPLY
        cloud_call.assert_not_called()

    def test_cloud_failure_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: True)
        monkeypatch.setattr(
            ai_assistant, "_call_cloud",
            MagicMock(side_effect=RuntimeError("cloud unreachable")),
        )

        reply = ai_assistant.answer("!asker", "question")

        assert reply == ai_assistant.UNAVAILABLE_REPLY

    def test_call_cloud_uses_openai_compatible_client(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = make_openai_response("hi from the cloud")
        monkeypatch.setattr(ai_assistant, "_cloud_client", lambda: mock_client)

        result = ai_assistant._call_cloud([{"role": "user", "content": "hi"}])

        assert result == "hi from the cloud"
        mock_client.chat.completions.create.assert_called_once()
        assert mock_client.chat.completions.create.call_args.kwargs["model"] == ai_assistant.DIGITALOCEAN_MODEL
        assert mock_client.chat.completions.create.call_args.kwargs["temperature"] == 0.2

    def test_complete_raw_uses_the_cloud_model(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: True)
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = make_openai_response('{"question":"Where?"}')
        monkeypatch.setattr(ai_assistant, "_cloud_client", lambda: mock_client)

        result = ai_assistant.complete_raw([{"role": "user", "content": "hi"}], temperature=0)

        assert result == '{"question":"Where?"}'
        assert mock_client.chat.completions.create.call_args.kwargs["model"] == ai_assistant.DIGITALOCEAN_MODEL
        assert mock_client.chat.completions.create.call_args.kwargs["temperature"] == 0

    def test_complete_raw_skips_the_call_when_offline_or_unconfigured(self, monkeypatch):
        cloud_call = MagicMock(side_effect=AssertionError("cloud should not be called"))
        monkeypatch.setattr(ai_assistant, "_call_cloud", cloud_call)
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "")

        with pytest.raises(RuntimeError):
            ai_assistant.complete_raw([{"role": "user", "content": "hi"}])

        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: False)

        with pytest.raises(RuntimeError):
            ai_assistant.complete_raw([{"role": "user", "content": "hi"}])
        cloud_call.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
