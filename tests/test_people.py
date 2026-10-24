"""Unit tests for db.py's people table: get_person/upsert_person accumulate
known info about a sender over time without blanking out existing fields."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(db, "DB_PATH", str(Path(tmp) / "test.db"))
        db.init_db()
        yield


def test_unknown_person_returns_none():
    assert db.get_person("!unknown") is None


def test_upsert_then_get():
    db.upsert_person("!node1", display_name="Bubba Snodgrass", last_known_address="412 Oak St")
    person = db.get_person("!node1")
    assert person["display_name"] == "Bubba Snodgrass"
    assert person["last_known_address"] == "412 Oak St"


def test_second_upsert_with_only_address_keeps_existing_name():
    db.upsert_person("!node1", display_name="Bubba Snodgrass")
    db.upsert_person("!node1", last_known_address="412 Oak St")
    person = db.get_person("!node1")
    assert person["display_name"] == "Bubba Snodgrass"
    assert person["last_known_address"] == "412 Oak St"


def test_upsert_overwrites_with_new_non_empty_value():
    db.upsert_person("!node1", last_known_address="Old address")
    db.upsert_person("!node1", last_known_address="New address")
    assert db.get_person("!node1")["last_known_address"] == "New address"


def test_different_nodes_are_independent():
    db.upsert_person("!alice", display_name="Alice")
    db.upsert_person("!bob", display_name="Bob")
    assert db.get_person("!alice")["display_name"] == "Alice"
    assert db.get_person("!bob")["display_name"] == "Bob"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
