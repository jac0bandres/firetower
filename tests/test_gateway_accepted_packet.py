"""Regression test for a real bug found via live hardware testing:
Meshtastic's protobuf-to-dict conversion omits the `channel` field when it
equals its default value (0, our primary channel) — so a real packet on
channel 0 has no "channel" key at all, not a "channel": 0 key. Every
existing test fixture included an explicit "channel": 0, which is why this
was never caught until a real radio message on the default channel was
silently dropped."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import gateway  # noqa: E402


class FakeInterface:
    myInfo = SimpleNamespace(my_node_num=0x12345678)


def test_missing_channel_key_is_treated_as_channel_zero():
    packet = {"from": 0xAAAAAAA1, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}}
    assert gateway._accepted_packet(packet, FakeInterface()) is True


def test_explicit_channel_zero_still_works():
    packet = {"from": 0xAAAAAAA1, "channel": 0, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}}
    assert gateway._accepted_packet(packet, FakeInterface()) is True


def test_different_channel_is_rejected():
    packet = {"from": 0xAAAAAAA1, "channel": 1, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}}
    assert gateway._accepted_packet(packet, FakeInterface()) is False


def test_own_node_is_rejected_even_on_missing_channel():
    packet = {"from": FakeInterface.myInfo.my_node_num, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hi"}}
    assert gateway._accepted_packet(packet, FakeInterface()) is False


def test_non_dict_packet_is_rejected():
    assert gateway._accepted_packet(None, FakeInterface()) is False


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
