"""Online/offline detection. The single source of truth other modules consult
before using the DigitalOcean model. Offline callers get no model name."""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

HEALTHCHECK_HOST = os.environ.get("HEALTHCHECK_HOST", "https://api.ollama.com")
HEALTHCHECK_INTERVAL_SECONDS = float(os.environ.get("HEALTHCHECK_INTERVAL_SECONDS", "5"))
HEALTHCHECK_TIMEOUT_SECONDS = float(os.environ.get("HEALTHCHECK_TIMEOUT_SECONDS", "3"))

DIGITALOCEAN_MODEL = os.environ.get("DIGITALOCEAN_MODEL", "openai-gpt-oss-20b")

_last_check = 0.0
_last_result = False


def is_online(force: bool = False) -> bool:
    global _last_check, _last_result
    now = time.monotonic()
    if not force and now - _last_check < HEALTHCHECK_INTERVAL_SECONDS:
        return _last_result

    previous = _last_result
    try:
        response = requests.get(HEALTHCHECK_HOST, timeout=HEALTHCHECK_TIMEOUT_SECONDS)
        _last_result = response.status_code < 500
    except requests.RequestException as exc:
        _last_result = False
        if previous:
            logger.warning("healthcheck failed, going offline: %s", exc)

    if _last_result != previous:
        logger.info("link state changed: %s -> %s", "online" if previous else "offline",
                    "online" if _last_result else "offline")

    _last_check = now
    return _last_result


def sitrep_model() -> str | None:
    """DigitalOcean model for SITREP summaries when online.

    Returns None when offline. There is no local-model substitute.
    """
    if not is_online():
        logger.debug("sitrep_model -> none (offline)")
        return None
    logger.debug("sitrep_model -> %s", DIGITALOCEAN_MODEL)
    return DIGITALOCEAN_MODEL
