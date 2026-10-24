"""Estimate where a report is when the sender's radio sent no GPS fix.

Two estimates, both stored as approximations (db.ESTIMATED_SOURCES):

- map: look up the place the sender named ("412 Oak St", "river st",
  "Hancock Park") on OpenStreetMap's Nominatim, limited to the operating
  area. Needs internet, so it only runs when router.is_online().
- radio_area: where the radio link says the sender must be. A message heard
  directly (0 hops) came from within radio range of the gateway; a relayed
  message came from near the relay, when we know where that relay is.

Real GPS always wins over both, and neither ever replaces it.
"""

import logging
import os
import re
import threading
import time

import requests

import db
import router

logger = logging.getLogger(__name__)

GEOCODER_URL = os.environ.get(
    "FIRETOWER_GEOCODER_URL", "https://nominatim.openstreetmap.org/search"
)
# south,west,north,east of the operating area when no radio has reported a
# position yet (default: North Georgia around Dahlonega and Ellijay). Once
# radios report GPS, the area is their bounding box plus AREA_MARGIN_DEG.
AREA_BBOX = tuple(
    float(part) for part in
    os.environ.get("FIRETOWER_AREA_BBOX", "34.40,-84.70,34.85,-83.80").split(",")
)
AREA_MARGIN_DEG = float(os.environ.get("FIRETOWER_AREA_MARGIN_DEG", "0.15"))
RADIO_RANGE_M = int(os.environ.get("FIRETOWER_RADIO_RANGE_M", "5000"))
GEOCODER_TIMEOUT_SECONDS = float(os.environ.get("FIRETOWER_GEOCODER_TIMEOUT_SECONDS", "4"))
USER_AGENT = "Firetower mesh triage (hackathon project)"

# Set by gateway.main() from the gateway radio's own position, or from env.
_gateway_position: tuple[float, float] | None = None
if os.environ.get("FIRETOWER_GATEWAY_LAT") and os.environ.get("FIRETOWER_GATEWAY_LON"):
    _gateway_position = (
        float(os.environ["FIRETOWER_GATEWAY_LAT"]), float(os.environ["FIRETOWER_GATEWAY_LON"])
    )

_cache: dict[str, dict | None] = {}
_lock = threading.Lock()
_last_request = 0.0

# Words that describe a place relative to something, not the place itself.
_LEADING = re.compile(
    r"^(?:(?:right |just |somewhere )?(?:near|by|at|behind|beside|next to|across from|"
    r"in front of|outside|inside|on|in|off|around|close to)\s+)?(?:the\s+)?",
    re.I,
)
# Local names the map knows by another name. Extend with
# FIRETOWER_PLACE_ALIASES="square=Public Square;the dam=Lake Zwerner Dam".
# "a|b" tries each name and keeps the match nearest the sender: OpenStreetMap
# calls Dahlonega's square "Public Square" and Ellijay's "Northside Square".
PLACE_ALIASES = {
    "square": "Public Square North|Northside Square",
    "town square": "Public Square North|Northside Square",
}
for _pair in filter(None, os.environ.get("FIRETOWER_PLACE_ALIASES", "").split(";")):
    _name, _, _target = _pair.partition("=")
    if _name.strip() and _target.strip():
        PLACE_ALIASES[_name.strip().lower()] = _target.strip()
_VAGUE = re.compile(r"^(?:here|there|home|my house|my home|unknown|not sure|idk)$", re.I)


def set_gateway_position(latitude: float, longitude: float) -> None:
    global _gateway_position
    _gateway_position = (latitude, longitude)
    logger.info("Gateway position for radio-area estimates: %.5f, %.5f", latitude, longitude)


def _area() -> tuple[float, float, float, float]:
    """south, west, north, east: where the mesh's radios are, or the default."""
    rows = db.list_node_positions()
    if not rows:
        return AREA_BBOX
    lats = [row["latitude"] for row in rows]
    lons = [row["longitude"] for row in rows]
    return (min(lats) - AREA_MARGIN_DEG, min(lons) - AREA_MARGIN_DEG,
            max(lats) + AREA_MARGIN_DEG, max(lons) + AREA_MARGIN_DEG)


def _reference_point(near: tuple[float, float] | None) -> tuple[float, float]:
    """Prefer matches close to the sender; else close to the mesh's center."""
    if near is not None:
        return near
    rows = db.list_node_positions()
    if rows:
        return (sum(r["latitude"] for r in rows) / len(rows),
                sum(r["longitude"] for r in rows) / len(rows))
    south, west, north, east = _area()
    return ((south + north) / 2, (west + east) / 2)


def _distance_sq(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + ((a[1] - b[1]) * 0.83) ** 2


def _candidates(place: str) -> list[str]:
    """The stated place, cleaned, then the street or landmark inside it."""
    text = re.sub(r"\s+", " ", place).strip(" .,!?")
    cleaned = _LEADING.sub("", text).strip()
    out = []
    for candidate in (cleaned,) + tuple(
        part.strip() for part in re.split(r"\b(?:on|near|by|at|off)\b", cleaned, flags=re.I)[1:]
    ):
        candidate = _LEADING.sub("", candidate).strip(" .,")
        candidate = PLACE_ALIASES.get(candidate.lower(), candidate)
        if len(candidate) >= 3 and not _VAGUE.match(candidate) and candidate.lower() not in (
            c.lower() for c in out
        ):
            out.append(candidate)
    return out[:3]


def _radius_from_bbox(bbox) -> int:
    try:
        south, north, west, east = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return 250
    meters = max(north - south, (east - west) * 0.83) * 111_000 / 2
    return int(min(max(meters, 50), 3000))


def _query(text: str, near: tuple[float, float] | None = None) -> dict | None:
    global _last_request
    south, west, north, east = _area()
    reference = _reference_point(near)
    key = (f"{text.lower()}|{south:.2f},{west:.2f},{north:.2f},{east:.2f}"
           f"|{reference[0]:.2f},{reference[1]:.2f}")
    if key in _cache:
        return _cache[key]
    with _lock:
        # Nominatim's usage policy: at most one request per second.
        wait = 1.0 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        response = requests.get(
            GEOCODER_URL,
            params={
                "q": text, "format": "jsonv2", "limit": 5, "bounded": 1,
                "viewbox": f"{west},{north},{east},{south}", "countrycodes": "us",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=GEOCODER_TIMEOUT_SECONDS,
        )
    response.raise_for_status()
    hit = None
    in_area = [
        result for result in response.json()
        if south <= float(result["lat"]) <= north and west <= float(result["lon"]) <= east
    ]
    if in_area:
        # Several towns have a "Public Square"; take the one nearest the sender.
        best = min(in_area, key=lambda r: _distance_sq((float(r["lat"]), float(r["lon"])), reference))
        hit = {
            "latitude": float(best["lat"]), "longitude": float(best["lon"]),
            "radius_m": _radius_from_bbox(best.get("boundingbox")),
            "label": best.get("display_name", text),
        }
    _cache[key] = hit
    return hit


def from_text(place: str | None, near: tuple[float, float] | None = None) -> dict | None:
    """Map lookup of a stated place. None when offline or nothing matches.

    near is the sender's last known position, if any, to pick between
    same-named places.
    """
    if not place or not place.strip():
        return None
    if not router.is_online():
        logger.info("Map lookup skipped for %r: offline", place)
        return None
    reference = _reference_point(near)
    for candidate in _candidates(place):
        hits = []
        for name in candidate.split("|"):
            try:
                found = _query(name, near)
            except Exception:
                logger.warning("Map lookup failed for %r", name, exc_info=True)
                return None
            if found:
                hits.append(found)
        hit = min(hits, key=lambda h: _distance_sq((h["latitude"], h["longitude"]), reference),
                  default=None)
        if hit:
            logger.info("Map lookup %r -> %.5f, %.5f (+/-%dm) %s", candidate,
                        hit["latitude"], hit["longitude"], hit["radius_m"], hit["label"])
            return {**hit, "note": f"map lookup of \"{candidate}\": {hit['label']}"}
        logger.info("Map lookup %r: no match in the operating area", candidate)
    return None


def _relay_position(relay_byte: int, sender: str) -> dict | None:
    """Meshtastic only reports the last byte of the relaying node's number."""
    matches = []
    for row in db.list_node_positions():
        try:
            number = int(str(row["node_id"]).lstrip("!"), 16)
        except ValueError:
            continue
        if number & 0xFF == relay_byte and row["node_id"] != sender:
            matches.append(row)
    return matches[0] if len(matches) == 1 else None


def radio_area(packet: dict, sender: str) -> dict | None:
    """Where the radio link places the sender, as a center and radius."""
    hops = packet.get("hopsAway")
    if hops is None and "hopStart" in packet and "hopLimit" in packet:
        hops = max(0, packet["hopStart"] - packet["hopLimit"])
    relay = packet.get("relayNode")
    if isinstance(hops, int) and hops > 0 and isinstance(relay, int):
        row = _relay_position(relay & 0xFF, sender)
        if row is not None:
            logger.info("Radio area for %s: near relay %s (%d hop(s))", sender, row["node_id"], hops)
            return {
                "latitude": row["latitude"], "longitude": row["longitude"],
                "radius_m": RADIO_RANGE_M,
                "note": f"relayed by {row['node_id']} ({hops} hop{'s' if hops != 1 else ''})",
            }
    if _gateway_position is None:
        logger.info("Radio area for %s unknown: no gateway position and no known relay", sender)
        return None
    hops = hops if isinstance(hops, int) else 0
    radius = RADIO_RANGE_M * (hops + 1)
    how = "heard directly by the gateway" if hops == 0 else f"{hops} hop(s) from the gateway"
    logger.info("Radio area for %s: %s, within ~%d m", sender, how, radius)
    return {
        "latitude": _gateway_position[0], "longitude": _gateway_position[1],
        "radius_m": radius, "note": how,
    }
