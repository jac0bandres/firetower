"""Street routes for the board's simulated dispatch animation.

Uses the public OSRM server (OpenStreetMap driving routes) when
router.is_online(); otherwise, or if it fails, a straight line. The routes
only animate the coordinator's dispatch record: Firetower never contacts a
department and nobody is actually driving these routes.
"""

import logging
import os
import threading

import requests

import router

logger = logging.getLogger(__name__)

OSRM_URL = os.environ.get(
    "FIRETOWER_OSRM_URL", "https://router.project-osrm.org/route/v1/driving"
).rstrip("/")
TIMEOUT_SECONDS = float(os.environ.get("FIRETOWER_OSRM_TIMEOUT_SECONDS", "8"))
USER_AGENT = "Firetower mesh triage (hackathon project)"

_cache: dict[tuple, dict] = {}
_lock = threading.Lock()


def _straight(start: tuple[float, float], end: tuple[float, float]) -> dict:
    return {"coordinates": [list(start), list(end)], "distance_m": None,
            "duration_s": None, "source": "straight"}


def drive(start: tuple[float, float], end: tuple[float, float]) -> dict:
    """A driving route between two [lon, lat] points, as GeoJSON coordinates."""
    key = tuple(round(value, 5) for value in (*start, *end))
    with _lock:
        if key in _cache:
            return _cache[key]
    if not router.is_online():
        return _straight(start, end)
    url = f"{OSRM_URL}/{start[0]:.6f},{start[1]:.6f};{end[0]:.6f},{end[1]:.6f}"
    try:
        response = requests.get(
            url, params={"overview": "full", "geometries": "geojson"},
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != "Ok" or not body.get("routes"):
            raise ValueError(f"OSRM answered {body.get('code')}")
        best = body["routes"][0]
        route = {
            "coordinates": best["geometry"]["coordinates"],
            "distance_m": best.get("distance"),
            "duration_s": best.get("duration"),
            "source": "osrm",
        }
    except Exception:
        logger.warning("Street route failed %s -> %s; using a straight line",
                       start, end, exc_info=True)
        return _straight(start, end)
    logger.info("Street route %s -> %s: %d points, %.1f km, ~%.0f min by normal traffic",
                start, end, len(route["coordinates"]), (route["distance_m"] or 0) / 1000,
                (route["duration_s"] or 0) / 60)
    with _lock:
        _cache[key] = route
    return route
