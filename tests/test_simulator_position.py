"""Unit tests for meshtastic_packets.simulator_position()/simulator_name() —
the team's virtual-node test harness sends GPS/name as JSON over PRIVATE_APP
rather than a standard Meshtastic POSITION_APP packet."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import meshtastic_packets as packets  # noqa: E402


def make_packet(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return {"decoded": {"portnum": "PRIVATE_APP", "payload": payload}}


class TestSimulatorPosition:
    def test_parses_valid_json(self):
        packet = make_packet('{"id":"!virt0003","name":"Bertha Wigglesworth","lat":34.51867,"lon":-83.98231}')
        assert packets.simulator_position(packet) == (34.51867, -83.98231)

    def test_accepts_str_payload_not_just_bytes(self):
        packet = make_packet('{"lat":34.5,"lon":-83.9}')
        packet["decoded"]["payload"] = '{"lat":34.5,"lon":-83.9}'
        assert packets.simulator_position(packet) == (34.5, -83.9)

    def test_non_private_app_portnum_returns_none(self):
        packet = {"decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b'{"lat":1,"lon":1}'}}
        assert packets.simulator_position(packet) is None

    def test_malformed_json_returns_none(self):
        assert packets.simulator_position(make_packet(b"not json")) is None

    def test_missing_lat_lon_returns_none(self):
        assert packets.simulator_position(make_packet('{"id":"!virt0001"}')) is None

    def test_out_of_range_coordinates_rejected(self):
        assert packets.simulator_position(make_packet('{"lat":999,"lon":-83.9}')) is None

    def test_zero_zero_rejected(self):
        assert packets.simulator_position(make_packet('{"lat":0,"lon":0}')) is None

    def test_undecodable_bytes_returns_none(self):
        assert packets.simulator_position(make_packet(b"\xff\xfe\x00bad")) is None


class TestSimulatorName:
    def test_extracts_name(self):
        packet = make_packet('{"id":"!virt0003","name":"Bertha Wigglesworth","lat":1,"lon":1}')
        assert packets.simulator_name(packet) == "Bertha Wigglesworth"

    def test_missing_name_returns_none(self):
        assert packets.simulator_name(make_packet('{"lat":1,"lon":1}')) is None

    def test_blank_name_returns_none(self):
        assert packets.simulator_name(make_packet('{"name":"   ","lat":1,"lon":1}')) is None

    def test_non_private_app_returns_none(self):
        packet = {"decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b'{"name":"x"}'}}
        assert packets.simulator_name(packet) is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
