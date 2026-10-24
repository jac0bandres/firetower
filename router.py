"""Online/offline detection. The single source of truth other modules consult
before deciding which model tier (cloud vs. local) to use for SITREPs."""

import os
import time

import requests

HEALTHCHECK_HOST = os.environ.get("HEALTHCHECK_HOST", "https://api.ollama.com")
HEALTHCHECK_INTERVAL_SECONDS = float(os.environ.get("HEALTHCHECK_INTERVAL_SECONDS", "5"))
HEALTHCHECK_TIMEOUT_SECONDS = float(os.environ.get("HEALTHCHECK_TIMEOUT_SECONDS", "3"))

_last_check = 0.0
_last_result = False


def is_online(force: bool = False) -> bool:
    global _last_check, _last_result
    now = time.monotonic()
    if not force and now - _last_check < HEALTHCHECK_INTERVAL_SECONDS:
        return _last_result
    raise NotImplementedError


def sitrep_model() -> str:
    """Returns which model tier to use for SITREP summaries right now."""
    raise NotImplementedError
