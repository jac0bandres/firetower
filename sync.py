"""Snowflake sync: pushes tickets that synced_to_snowflake=0 whenever
router.is_online() is true, and runs Cortex queries for SITREP support."""

import os

import snowflake.connector

import db
import router

SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT")
SNOWFLAKE_USER = os.environ.get("SNOWFLAKE_USER")
SNOWFLAKE_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE")
SNOWFLAKE_SCHEMA = os.environ.get("SNOWFLAKE_SCHEMA")


def sync_pending_tickets() -> int:
    """Pushes unsynced tickets to Snowflake. Returns count synced. No-op offline."""
    if not router.is_online():
        return 0
    raise NotImplementedError
