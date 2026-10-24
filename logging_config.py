"""Shared logging setup. Call setup_logging() once per process entrypoint
(gateway.py, board.py, test runs) before anything else logs — modules
themselves only do `logging.getLogger(__name__)` and never configure
handlers, so we never get duplicate log lines from double-configuration.

Two files, both UTF-8:
- LOG_PATH (data/firetower.log): Firetower's own decisions at LOG_LEVEL,
  plus warnings from libraries. This is the one to read when debugging.
- DEBUG_LOG_PATH (data/firetower-debug.log): everything at DEBUG, including
  raw Meshtastic packets, HTTP traffic and model calls.
"""

import logging
import os
import sys

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOG_PATH = os.environ.get("FIRETOWER_LOG_PATH", "data/firetower.log")
DEBUG_LOG_PATH = os.environ.get("FIRETOWER_DEBUG_LOG_PATH", "data/firetower-debug.log")

# Library loggers that flood the main log at DEBUG/INFO (health probes every
# few seconds, raw radio frames, HTTP connection chatter).
NOISY_LOGGERS = (
    "meshtastic", "urllib3", "httpx", "httpx2", "httpcore", "httpcore2",
    "openai", "uvicorn.access", "asyncio", "multipart",
)

FORMAT = "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"

_configured = False


class _QuietLibraries(logging.Filter):
    """Keep library chatter below WARNING out of the main log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return not any(
            record.name == name or record.name.startswith(name + ".") for name in NOISY_LOGGERS
        )


def setup_logging() -> None:
    global _configured
    if _configured:
        return

    for path in (LOG_PATH, DEBUG_LOG_PATH):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    formatter = logging.Formatter(FORMAT)

    main_level = logging.getLevelName(LOG_LEVEL.upper())
    # A Windows console is cp1252; never let a radio message's emoji or a
    # model's curly quote turn a log line into a logging error.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="backslashreplace")
    console = logging.StreamHandler()
    main_file = logging.FileHandler(LOG_PATH, encoding="utf-8")
    for handler in (console, main_file):
        handler.setLevel(main_level)
        handler.addFilter(_QuietLibraries())
        handler.setFormatter(formatter)

    debug_file = logging.FileHandler(DEBUG_LOG_PATH, encoding="utf-8")
    debug_file.setLevel(logging.DEBUG)
    debug_file.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in (console, main_file, debug_file):
        root.addHandler(handler)
    _configured = True
