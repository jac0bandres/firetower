"""Keep the suite offline: no test reaches the real map service."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import locate  # noqa: E402


@pytest.fixture(autouse=True)
def no_map_lookups(request, monkeypatch):
    if request.node.get_closest_marker("real_locate"):
        return
    monkeypatch.setattr(locate, "from_text", lambda place, near=None: None)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_locate: use locate.from_text with a mocked HTTP layer")
