"""Location estimates for reports without GPS: map lookup and radio area."""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import gateway  # noqa: E402
import locate  # noqa: E402
import router  # noqa: E402
import triage  # noqa: E402
from triage import TriageResult  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        monkeypatch.setattr(db, "DB_PATH", str(Path(directory) / "t.db"))
        db.init_db()
        monkeypatch.setattr(gateway.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(locate, "_cache", {})
        monkeypatch.setattr(locate, "_last_request", 0.0)
        yield


def nominatim(lat, lon, name="River Street, Dahlonega"):
    response = MagicMock()
    response.json.return_value = [{
        "lat": str(lat), "lon": str(lon), "display_name": name,
        "boundingbox": [str(lat - 0.001), str(lat + 0.001), str(lon - 0.001), str(lon + 0.001)],
    }]
    return response


class FakeRadio:
    myInfo = SimpleNamespace(my_node_num=0x12345678)

    def __init__(self):
        self.sent = []

    def sendText(self, text, **kwargs):
        self.sent.append(text)


def text_packet(text, sender="!7a29cc82", packet_id=1, **extra):
    return {"from": 0x7A29CC82, "fromId": sender, "id": packet_id, "channel": 0,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text},
            **extra}


def test_candidates_strip_relative_words_and_try_the_street():
    assert locate._candidates("the old house on river st") == ["old house on river st", "river st"]
    assert locate._candidates("by the square") == ["Public Square North|Northside Square"]
    assert locate._candidates("here") == []


@pytest.mark.real_locate
def test_map_lookup_is_bounded_to_the_area_and_cached(monkeypatch):
    monkeypatch.setattr(router, "is_online", lambda force=False: True)
    with patch.object(locate.requests, "get", return_value=nominatim(34.531, -83.985)) as get:
        first = locate.from_text("the old house on river st")
        second = locate.from_text("the old house on river st")
    assert (first["latitude"], first["longitude"]) == (34.531, -83.985)
    assert "map lookup" in first["note"]
    assert second == first
    assert get.call_count == 1
    params = get.call_args.kwargs["params"]
    assert params["bounded"] == 1 and "viewbox" in params
    assert "User-Agent" in get.call_args.kwargs["headers"]


@pytest.mark.real_locate
def test_map_lookup_rejects_results_outside_the_area_and_skips_offline(monkeypatch):
    monkeypatch.setattr(router, "is_online", lambda force=False: True)
    with patch.object(locate.requests, "get", return_value=nominatim(40.0, -75.0)):
        assert locate.from_text("river st") is None
    monkeypatch.setattr(router, "is_online", lambda force=False: False)
    with patch.object(locate.requests, "get") as get:
        assert locate.from_text("main st") is None
    get.assert_not_called()


@pytest.mark.real_locate
def test_map_lookup_prefers_the_match_nearest_the_sender(monkeypatch):
    monkeypatch.setattr(router, "is_online", lambda force=False: True)
    response = MagicMock()
    response.json.return_value = [
        {"lat": "34.5327", "lon": "-83.9850", "display_name": "Public Square, Dahlonega"},
        {"lat": "34.6948", "lon": "-84.4822", "display_name": "Public Square, Ellijay"},
    ]
    with patch.object(locate.requests, "get", return_value=response):
        hit = locate.from_text("by the square", near=(34.70, -84.48))
    assert "Ellijay" in hit["label"]


def test_area_follows_the_mesh_radios():
    db.save_node_position("!a", 34.69, -84.48, "2026-10-24T19:00:00+00:00", "{}")
    south, west, north, east = locate._area()
    assert south < 34.69 < north and west < -84.48 < east


def test_radio_area_uses_gateway_when_heard_directly(monkeypatch):
    monkeypatch.setattr(locate, "_gateway_position", (34.53, -83.98))
    area = locate.radio_area({"hopsAway": 0}, "!7a29cc82")
    assert (area["latitude"], area["longitude"]) == (34.53, -83.98)
    assert area["radius_m"] == locate.RADIO_RANGE_M
    assert "directly" in area["note"]


def test_radio_area_uses_known_relay_position(monkeypatch):
    monkeypatch.setattr(locate, "_gateway_position", (34.53, -83.98))
    db.save_node_position("!a1b2c3d4", 34.60, -84.05, "2026-10-24T19:00:00+00:00", "{}")
    area = locate.radio_area({"hopsAway": 1, "relayNode": 0xD4}, "!7a29cc82")
    assert (area["latitude"], area["longitude"]) == (34.60, -84.05)
    assert "!a1b2c3d4" in area["note"]


def test_no_gps_report_is_pinned_by_map_lookup(monkeypatch):
    monkeypatch.setattr(locate, "from_text", lambda place, near=None: {
        "latitude": 34.531, "longitude": -83.985, "radius_m": 120,
        "note": 'map lookup of "river st": River Street'} if place else None)
    with patch.object(triage, "triage", return_value=TriageResult(
            urgency="ROUTINE", location="the old house on river st")):
        gateway.on_receive(text_packet("roof caved in on river st"), FakeRadio())
    ticket = db.list_tickets()[0]
    assert (ticket["latitude"], ticket["longitude"]) == (34.531, -83.985)
    assert ticket["location_source"] == "map"
    assert ticket["location_radius_m"] == 120
    assert not gateway._has_fresh_gps(ticket)


def test_no_place_falls_back_to_radio_area(monkeypatch):
    monkeypatch.setattr(locate, "_gateway_position", (34.53, -83.98))
    with patch.object(triage, "triage", return_value=TriageResult(urgency="ROUTINE")):
        gateway.on_receive(text_packet("help", hopsAway=0), FakeRadio())
    ticket = db.list_tickets()[0]
    assert ticket["location_source"] == "radio_area"
    assert ticket["location_radius_m"] == locate.RADIO_RANGE_M


def test_follow_up_answer_pins_ticket_and_real_gps_replaces_it(monkeypatch):
    monkeypatch.setattr(locate, "_gateway_position", (34.53, -83.98))
    lookups = {"412 Oak St": {"latitude": 34.52, "longitude": -83.99, "radius_m": 60,
                              "note": 'map lookup of "412 Oak St"'}}
    monkeypatch.setattr(locate, "from_text", lambda place, near=None: lookups.get(place))
    radio = FakeRadio()
    with patch.object(triage, "triage", return_value=TriageResult(urgency="EMERGENCY")), \
            patch.object(gateway, "_ai_clarification_question",
                         side_effect=lambda missing, raw: gateway._clarification_question(missing)):
        gateway.on_receive(text_packet("house fire help", hopsAway=0), radio)
    assert db.list_tickets()[0]["location_source"] == "radio_area"

    with patch.object(triage, "extract_with_model", return_value=TriageResult(
            urgency="EMERGENCY", location="412 Oak St", injured_count=1)), \
            patch.object(gateway, "_ai_gps_detail_question",
                         side_effect=lambda step, raw, known=None: gateway._gps_detail_question(step)):
        gateway.on_receive(text_packet("412 Oak St, 1 hurt", packet_id=2), radio)
    ticket = db.list_tickets()[0]
    assert ticket["location_source"] == "map"
    assert (ticket["latitude"], ticket["longitude"]) == (34.52, -83.99)

    gateway.on_position({"from": 0x7A29CC82, "fromId": "!7a29cc82", "id": 9, "channel": 0,
                         "decoded": {"portnum": "POSITION_APP",
                                     "position": {"latitude": 34.5244, "longitude": -84.0172}}},
                        radio)
    ticket = db.list_tickets()[0]
    assert ticket["location_source"] == "gps"
    assert ticket["latitude"] == pytest.approx(34.5244)
    assert ticket["location_note"] is None
    # An estimate never overwrites real GPS.
    assert not db.set_location_estimate(ticket["ticket_code"], 1, 1, "map", 10, "x")
