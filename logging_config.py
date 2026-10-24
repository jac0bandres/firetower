"""Shared logging setup. Call setup_logging() once per process entrypoint
(gateway.py, board.py, test runs) before anything else logs — modules
themselves only do `logging.getLogger(__name__)` and never configure
handlers, so we never get duplicate log lines from double-configuration."""

import logging
import os

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_PATH = os.environ.get("FIRETOWER_LOG_PATH", "data/firetower.log")

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return

    os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_PATH),
        ],
    )
    _configured = True
