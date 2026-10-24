"""Unit tests for ai_assistant.py — channel-context building, per-sender
threads, and model fallback. Ollama and db are exercised against a real
temp SQLite file (ai_threads/ai_messages are simple enough it's cheaper
than mocking db), with the Ollama client mocked."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import router  # noqa: E402
import ai_assistant  # noqa: E402


def make_ollama_response(text: str):
    response = MagicMock()
    response.message.content = text
    return response


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


class TestAnswer:
    def test_uses_primary_model_and_persists_thread(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.return_value = make_ollama_response("Two open emergencies.")
        monkeypatch.setattr(ai_assistant, "_client", mock_client)

        reply = ai_assistant.answer("!asker", "how many open emergencies?")

        assert reply == "Two open emergencies."
        thread_id = db.get_or_create_ai_thread("!asker")
        history = db.list_ai_thread_messages(thread_id)
        assert [h["role"] for h in history] == ["user", "assistant"]
        assert history[0]["content"] == "how many open emergencies?"
        assert history[1]["content"] == "Two open emergencies."

    def test_second_question_continues_same_thread(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.side_effect = [
            make_ollama_response("First answer."),
            make_ollama_response("Second answer."),
        ]
        monkeypatch.setattr(ai_assistant, "_client", mock_client)

        ai_assistant.answer("!asker", "question one")
        ai_assistant.answer("!asker", "question two")

        thread_id = db.get_or_create_ai_thread("!asker")
        history = db.list_ai_thread_messages(thread_id)
        assert len(history) == 4
        # The second call's prompt included the first exchange as context.
        second_call_messages = mock_client.chat.call_args_list[1].kwargs["messages"]
        contents = [m["content"] for m in second_call_messages]
        assert "question one" in contents
        assert "First answer." in contents

    def test_strips_leaked_thinking_trace(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.return_value = make_ollama_response(
            "Let me reason about this step by step...\nSo the answer is clear.</think>\n\nTwo open tickets."
        )
        monkeypatch.setattr(ai_assistant, "_client", mock_client)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "Two open tickets."
        assert "reason about this" not in reply

    def test_falls_back_to_backup_model(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.side_effect = [RuntimeError("primary down"), make_ollama_response("Backup answer.")]
        monkeypatch.setattr(ai_assistant, "_client", mock_client)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "Backup answer."
        assert mock_client.chat.call_count == 2

    def test_degrades_when_both_models_fail(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.side_effect = RuntimeError("down")
        monkeypatch.setattr(ai_assistant, "_client", mock_client)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == ai_assistant.UNAVAILABLE_REPLY
        thread_id = db.get_or_create_ai_thread("!asker")
        history = db.list_ai_thread_messages(thread_id)
        assert history[-1]["content"] == ai_assistant.UNAVAILABLE_REPLY

    def test_different_senders_get_different_threads(self, monkeypatch):
        mock_client = MagicMock()
        mock_client.chat.return_value = make_ollama_response("ok")
        monkeypatch.setattr(ai_assistant, "_client", mock_client)

        ai_assistant.answer("!alice", "hi")
        ai_assistant.answer("!bob", "hi")

        assert db.get_or_create_ai_thread("!alice") != db.get_or_create_ai_thread("!bob")


class TestCloudTier:
    def test_unconfigured_cloud_skips_straight_to_local(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "")
        mock_local = MagicMock()
        mock_local.chat.return_value = make_ollama_response("local answer")
        monkeypatch.setattr(ai_assistant, "_client", mock_local)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "local answer"

    def test_configured_and_online_tries_cloud_first(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: True)
        monkeypatch.setattr(ai_assistant, "_call_cloud", lambda messages: "cloud answer")
        mock_local = MagicMock()
        monkeypatch.setattr(ai_assistant, "_client", mock_local)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "cloud answer"
        mock_local.chat.assert_not_called()

    def test_configured_but_offline_skips_cloud(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: False)
        cloud_call = MagicMock(side_effect=AssertionError("cloud should not be called while offline"))
        monkeypatch.setattr(ai_assistant, "_call_cloud", cloud_call)
        mock_local = MagicMock()
        mock_local.chat.return_value = make_ollama_response("local answer")
        monkeypatch.setattr(ai_assistant, "_client", mock_local)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "local answer"
        cloud_call.assert_not_called()

    def test_cloud_failure_falls_back_to_local(self, monkeypatch):
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_API_KEY", "fake-key")
        monkeypatch.setattr(ai_assistant, "DIGITALOCEAN_BASE_URL", "https://fake.example/v1")
        monkeypatch.setattr(router, "is_online", lambda force=False: True)
        monkeypatch.setattr(
            ai_assistant, "_call_cloud",
            MagicMock(side_effect=RuntimeError("cloud unreachable")),
        )
        mock_local = MagicMock()
        mock_local.chat.return_value = make_ollama_response("local answer")
        monkeypatch.setattr(ai_assistant, "_client", mock_local)

        reply = ai_assistant.answer("!asker", "question")

        assert reply == "local answer"

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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
