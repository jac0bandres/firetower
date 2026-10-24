"""Unit tests for the snowflake_rag.py scaffold: inert when unconfigured
(the common case today — no credentials in .env), explicit about being
unimplemented once configured, rather than silently doing nothing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import snowflake_rag  # noqa: E402


class TestIsConfigured:
    def test_false_when_env_blank(self, monkeypatch):
        for var in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD",
                    "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA"):
            monkeypatch.setattr(snowflake_rag, var, "")
        assert snowflake_rag.is_configured() is False

    def test_true_when_all_set(self, monkeypatch):
        for var in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD",
                    "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA"):
            monkeypatch.setattr(snowflake_rag, var, "x")
        assert snowflake_rag.is_configured() is True

    def test_false_when_partially_set(self, monkeypatch):
        monkeypatch.setattr(snowflake_rag, "SNOWFLAKE_ACCOUNT", "x")
        monkeypatch.setattr(snowflake_rag, "SNOWFLAKE_USER", "")
        assert snowflake_rag.is_configured() is False


class TestQueryContext:
    def test_returns_none_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(snowflake_rag, "SNOWFLAKE_ACCOUNT", "")
        assert snowflake_rag.query_context("what happened") is None

    def test_raises_not_implemented_when_configured(self, monkeypatch):
        for var in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD",
                    "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA"):
            monkeypatch.setattr(snowflake_rag, var, "x")
        with pytest.raises(NotImplementedError):
            snowflake_rag.query_context("what happened")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
