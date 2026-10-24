"""Convert Meshtastic's received packet dictionaries into stored ticket data."""

import base64
import json
import math
from datetime import datetime, timezone


def node_id(packet: dict) -> str | None:
    """Use Meshtastic's !hex node ID, falling back to its numeric sender."""
    if packet.get("fromId"):
        return str(packet["fromId"])
    number = packet.get("from")
    return f"!{number:08x}" if isinstance(number, int) and number > 0 else None


def received_at(packet: dict) -> str:
    timestamp = packet.get("rxTime")
    if isinstance(timestamp, (int, float)) and timestamp > 0:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def position(packet: dict) -> tuple[float, float] | None:
    data = packet.get("decoded", {}).get("position", {})
    if not isinstance(data, dict):
        return None
    if data.get("precisionBits") == 0:
        return None
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    if latitude is None and "latitudeI" in data:
        latitude = data["latitudeI"] * 1e-7
    if longitude is None and "longitudeI" in data:
        longitude = data["longitudeI"] * 1e-7
    if latitude is None or longitude is None:
        return None
    try:
        # Round to the 1e-7 precision of Meshtastic's integer encoding so a
        # latitudeI/longitudeI round-trip doesn't pick up float noise (e.g.
        # 348683400 * 1e-7 == 34.868339999999996, not 34.86834).
        latitude, longitude = round(float(latitude), 7), round(float(longitude), 7)
    except (TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (latitude, longitude))):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if latitude == 0 and longitude == 0:
        return None
    return latitude, longitude


def packet_json(packet: dict) -> str:
    """Preserve the received fields; encode byte payloads and omit protobuf objects.

    Meshtastic adds ``raw`` protobuf objects at the packet and position levels.
    They are transport objects, while the remaining dictionary has their data.
    """
    def serializable(value):
        if isinstance(value, dict):
            return {key: serializable(item) for key, item in value.items() if key != "raw"}
        if isinstance(value, (list, tuple)):
            return [serializable(item) for item in value]
        if isinstance(value, bytes):
            return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise TypeError(f"Unsupported Meshtastic packet field: {type(value).__name__}")

    return json.dumps(serializable(packet), separators=(",", ":"), allow_nan=False)
