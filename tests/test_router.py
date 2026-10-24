"""Unit tests for router.py — the online/offline probe and SITREP model
tier selection. requests.get is mocked; no network access is needed."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

import router  # noqa: E402


@pytest.fixture(autouse=True)
def reset_router_cache():
    router._last_check = 0.0
    router._last_result = False
    yield


class TestIsOnline:
    def test_online_when_healthcheck_succeeds(self, monkeypatch):
        mock_get = MagicMock(return_value=MagicMock(status_code=200))
        monkeypatch.setattr(requests, "get", mock_get)

        assert router.is_online(force=True) is True

    def test_offline_when_healthcheck_raises(self, monkeypatch):
        monkeypatch.setattr(requests, "get", MagicMock(side_effect=requests.ConnectionError("no route")))

        assert router.is_online(force=True) is False

    def test_offline_on_server_error_status(self, monkeypatch):
        monkeypatch.setattr(requests, "get", MagicMock(return_value=MagicMock(status_code=503)))

        assert router.is_online(force=True) is False

    def test_result_is_cached_within_interval(self, monkeypatch):
        mock_get = MagicMock(return_value=MagicMock(status_code=200))
        monkeypatch.setattr(requests, "get", mock_get)

        router.is_online(force=True)
        router.is_online()
        router.is_online()

        assert mock_get.call_count == 1

    def test_force_bypasses_cache(self, monkeypatch):
        mock_get = MagicMock(return_value=MagicMock(status_code=200))
        monkeypatch.setattr(requests, "get", mock_get)

        router.is_online(force=True)
        router.is_online(force=True)

        assert mock_get.call_count == 2


class TestSitrepModel:
    def test_returns_cloud_model_when_online(self, monkeypatch):
        monkeypatch.setattr(router, "is_online", lambda force=False: True)
        assert router.sitrep_model() == router.DIGITALOCEAN_MODEL

    def test_returns_local_model_when_offline(self, monkeypatch):
        monkeypatch.setattr(router, "is_online", lambda force=False: False)
        assert router.sitrep_model() == router.SITREP_MODEL_OFFLINE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
