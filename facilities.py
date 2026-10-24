"""Fire stations, hospitals and police stations around the mesh.

Fetched from OpenStreetMap's Overpass API when router.is_online() and cached
in data/facilities.json, so the board keeps showing them offline after one
successful fetch. Reference only: Firetower never contacts these places.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import locate
import router

logger = logging.getLogger(__name__)

# Tried in order; public Overpass servers are often busy (502s, timeouts).
OVERPASS_URLS = [url.strip() for url in os.environ.get(
    "FIRETOWER_OVERPASS_URLS",
    "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter",
).split(",") if url.strip()]
CACHE_PATH = Path(os.environ.get("FIRETOWER_FACILITIES_PATH", "data/facilities.json"))
REFRESH_SECONDS = int(os.environ.get("FIRETOWER_FACILITIES_REFRESH_SECONDS", str(6 * 3600)))
RETRY_SECONDS = 120
TIMEOUT_SECONDS = 30

KINDS = {"fire_station": "fire", "hospital": "hospital", "police": "police"}

_lock = threading.Lock()
_refreshing = False
_last_attempt = 0.0


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"facilities": [], "fetched_at": None, "area": None}
    except (OSError, ValueError):
        logger.warning("Facilities cache unreadable; refetching", exc_info=True)
        return {"facilities": [], "fetched_at": None, "area": None}


def _address(tags: dict) -> str | None:
    street = " ".join(part for part in (tags.get("addr:housenumber"), tags.get("addr:street")) if part)
    parts = [part for part in (street, tags.get("addr:city")) if part]
    return ", ".join(parts) or None


def _parse(elements: list[dict]) -> list[dict]:
    facilities = []
    for element in elements:
        tags = element.get("tags", {})
        kind = KINDS.get(tags.get("amenity"))
        latitude = element.get("lat", element.get("center", {}).get("lat"))
        longitude = element.get("lon", element.get("center", {}).get("lon"))
        if kind is None or latitude is None or longitude is None:
            continue
        facilities.append({
            "id": f"{element.get('type', 'node')}/{element.get('id')}",
            "kind": kind,
            "name": tags.get("name") or {"fire": "Fire station", "hospital": "Hospital",
                                         "police": "Police station"}[kind],
            "latitude": float(latitude),
            "longitude": float(longitude),
            "address": _address(tags),
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "emergency_room": tags.get("emergency") == "yes" if kind == "hospital" else None,
        })
    return facilities


def _fetch(area: tuple[float, float, float, float]) -> None:
    global _refreshing
    south, west, north, east = area
    query = (
        "[out:json][timeout:25];"
        f'nwr["amenity"~"^(fire_station|hospital|police)$"]({south},{west},{north},{east});'
        "out center tags;"
    )
    try:
        elements = None
        for url in OVERPASS_URLS:
            try:
                response = requests.post(url, data={"data": query}, timeout=TIMEOUT_SECONDS,
                                         headers={"User-Agent": locate.USER_AGENT})
                response.raise_for_status()
                elements = response.json().get("elements", [])
                break
            except Exception as error:
                logger.warning("Overpass %s failed: %s", url, error)
        if elements is None:
            raise RuntimeError("every Overpass server failed")
        facilities = _parse(elements)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "facilities": facilities,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "area": list(area),
        }, indent=1), encoding="utf-8")
        counts = {kind: sum(f["kind"] == kind for f in facilities) for kind in KINDS.values()}
        logger.info("Facilities refreshed for area %s: %s", [round(x, 3) for x in area], counts)
    except Exception:
        logger.warning("Facilities fetch failed; keeping the cached list", exc_info=True)
    finally:
        with _lock:
            _refreshing = False


def _area_changed(cached_area, area) -> bool:
    if not cached_area:
        return True
    return any(abs(a - b) > 0.05 for a, b in zip(cached_area, area))


def load() -> dict:
    """The cached list now; a background refresh starts when it is stale."""
    global _refreshing, _last_attempt
    cache = _read_cache()
    area = locate._area()
    stale = cache["fetched_at"] is None or _area_changed(cache.get("area"), area) or (
        datetime.now(timezone.utc) - datetime.fromisoformat(cache["fetched_at"])
    ).total_seconds() > REFRESH_SECONDS
    if stale and time.monotonic() - _last_attempt > RETRY_SECONDS and router.is_online():
        with _lock:
            if not _refreshing:
                _refreshing = True
                _last_attempt = time.monotonic()
                threading.Thread(target=_fetch, args=(area,), daemon=True).start()
    return cache
