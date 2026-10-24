"""Unit tests for triage.py — keyword net, priority floor, model extraction,
and the merged triage() pipeline. The DigitalOcean client and db are mocked;
no live server or database is needed to run these."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import ai_assistant  # noqa: E402
import db  # noqa: E402
import triage  # noqa: E402


def cloud_json(payload: dict) -> str:
    return json.dumps(payload)


class TestKeywordFloor:
    def test_emergency_keyword_fires(self):
        assert triage.keyword_floor("there's a fire in the kitchen") == "EMERGENCY"

    def test_priority_keyword_fires(self):
        assert triage.keyword_floor("I need insulin soon") == "PRIORITY"

    def test_emergency_outranks_priority_when_both_present(self):
        assert triage.keyword_floor("fire, and I'm injured") == "EMERGENCY"

    def test_no_keyword_returns_none(self):
        assert triage.keyword_floor("when is the next supply drop") is None

    def test_case_insensitive(self):
        assert triage.keyword_floor("TRAPPED under debris") == "EMERGENCY"


class TestApplyPriorityFloor:
    def test_raises_routine_to_priority_for_priority_node(self, monkeypatch):
        monkeypatch.setattr(db, "list_priority_nodes", lambda: {"Shelter": "shelter lead"})
        result = triage.TriageResult(urgency="ROUTINE")
        out = triage.apply_priority_floor(result, "Shelter")
        assert out.urgency == "PRIORITY"
        assert out.priority_floor_applied is True

    def test_never_lowers_emergency(self, monkeypatch):
        monkeypatch.setattr(db, "list_priority_nodes", lambda: {"Shelter": "shelter lead"})
        result = triage.TriageResult(urgency="EMERGENCY")
        out = triage.apply_priority_floor(result, "Shelter")
        assert out.urgency == "EMERGENCY"
        assert out.priority_floor_applied is False

    def test_non_priority_node_untouched(self, monkeypatch):
        monkeypatch.setattr(db, "list_priority_nodes", lambda: {"Shelter": "shelter lead"})
        result = triage.TriageResult(urgency="ROUTINE")
        out = triage.apply_priority_floor(result, "Trailhead")
        assert out.urgency == "ROUTINE"
        assert out.priority_floor_applied is False


class TestExtractWithModel:
    def test_does_not_import_a_local_model_client(self):
        assert "ollama" not in sys.modules
        assert not hasattr(triage, "_client")

    def test_uses_cloud_model_result(self, monkeypatch):
        calls = MagicMock(return_value=cloud_json(
            {"urgency": "EMERGENCY", "location": "Hwy 52", "what_happened": "bridge out", "injured_count": 2}
        ))
        monkeypatch.setattr(ai_assistant, "complete_raw", calls)

        result = triage.extract_with_model("bridge out on 52, two hurt")

        assert result.urgency == "EMERGENCY"
        assert result.location == "Hwy 52"
        assert result.injured_count == 2
        calls.assert_called_once()
        assert calls.call_args.kwargs["temperature"] == 0
        assert "bridge out on 52, two hurt" in calls.call_args.args[0][0]["content"]

    def test_does_not_retry_a_second_model_when_cloud_fails(self, monkeypatch):
        calls = MagicMock(side_effect=RuntimeError("cloud unreachable"))
        monkeypatch.setattr(ai_assistant, "complete_raw", calls)

        result = triage.extract_with_model("checking in, we're ok")

        assert calls.call_count == 1
        assert result.urgency == "ROUTINE"
        assert result.raised_by_keyword is False

    def test_degrades_to_keyword_only_when_cloud_fails(self, monkeypatch):
        monkeypatch.setattr(
            ai_assistant, "complete_raw", MagicMock(side_effect=RuntimeError("no model available"))
        )

        result = triage.extract_with_model("fire in the shelter kitchen")

        assert result.urgency == "EMERGENCY"
        assert result.raised_by_keyword is True

    def test_degrades_to_routine_when_no_keyword_and_cloud_fails(self, monkeypatch):
        monkeypatch.setattr(
            ai_assistant, "complete_raw", MagicMock(side_effect=RuntimeError("no model available"))
        )

        result = triage.extract_with_model("what's for dinner tonight")

        assert result.urgency == "ROUTINE"
        assert result.raised_by_keyword is False


class TestTriagePipeline:
    def test_keyword_net_raises_model_urgency(self, monkeypatch):
        monkeypatch.setattr(db, "list_priority_nodes", lambda: {})
        monkeypatch.setattr(
            triage,
            "extract_with_model",
            lambda text: triage.TriageResult(urgency="WELFARE", what_happened="asking about insulin"),
        )

        result = triage.triage("just wondering about insulin supply", "Ridge Camp")

        assert result.urgency == "PRIORITY"
        assert result.raised_by_keyword is True

    def test_keyword_net_never_lowers_model_urgency(self, monkeypatch):
        monkeypatch.setattr(db, "list_priority_nodes", lambda: {})
        monkeypatch.setattr(
            triage, "extract_with_model", lambda text: triage.TriageResult(urgency="EMERGENCY")
        )

        result = triage.triage("routine message with no keywords", "Trailhead")

        assert result.urgency == "EMERGENCY"
        assert result.raised_by_keyword is False

    def test_priority_node_floor_applies_after_model_and_keywords(self, monkeypatch):
        monkeypatch.setattr(db, "list_priority_nodes", lambda: {"Main St": "registered medical need"})
        monkeypatch.setattr(
            triage, "extract_with_model", lambda text: triage.TriageResult(urgency="ROUTINE")
        )

        result = triage.triage("just saying hi", "Main St")

        assert result.urgency == "PRIORITY"
        assert result.priority_floor_applied is True

    def test_full_precedence_emergency_keyword_beats_priority_node(self, monkeypatch):
        monkeypatch.setattr(db, "list_priority_nodes", lambda: {"Main St": "registered medical need"})
        monkeypatch.setattr(
            triage, "extract_with_model", lambda text: triage.TriageResult(urgency="ROUTINE")
        )

        result = triage.triage("fire!", "Main St")

        assert result.urgency == "EMERGENCY"
        assert result.raised_by_keyword is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
