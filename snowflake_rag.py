"""Snowflake as the RAG retrieval store for the !ai assistant.

Scaffolding only — not wired into ai_assistant.build_channel_context() yet.
Until SNOWFLAKE_* credentials are filled in (and the retrieval query below
is implemented), is_configured() is False and the assistant keeps using the
local SQLite ticket history as its only context, same as today.

Intended shape once implemented: sync.py mirrors tickets into
SNOWFLAKE_RAG_TABLE (account/warehouse/db/schema from env), and
query_context() runs a Cortex Search / vector-similarity query against that
table to retrieve the messages most relevant to the asker's question,
instead of build_channel_context()'s current "last 40 tickets" window.
"""

import os

SNOWFLAKE_ACCOUNT = os.environ.get("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER = os.environ.get("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD = os.environ.get("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_WAREHOUSE = os.environ.get("SNOWFLAKE_WAREHOUSE", "")
SNOWFLAKE_DATABASE = os.environ.get("SNOWFLAKE_DATABASE", "")
SNOWFLAKE_SCHEMA = os.environ.get("SNOWFLAKE_SCHEMA", "")
SNOWFLAKE_RAG_TABLE = os.environ.get("SNOWFLAKE_RAG_TABLE", "ticket_context")


def is_configured() -> bool:
    return bool(
        SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER and SNOWFLAKE_PASSWORD
        and SNOWFLAKE_WAREHOUSE and SNOWFLAKE_DATABASE and SNOWFLAKE_SCHEMA
    )


def query_context(question: str, limit: int = 10) -> list[str] | None:
    """Returns the most relevant prior messages for `question`, or None if
    Snowflake isn't configured. Raises NotImplementedError if it is
    configured but the retrieval query itself hasn't been built yet."""
    if not is_configured():
        return None
    raise NotImplementedError(
        "Snowflake is configured but query_context() isn't implemented yet — "
        "fill in the Cortex Search / vector-similarity query against "
        f"{SNOWFLAKE_DATABASE}.{SNOWFLAKE_SCHEMA}.{SNOWFLAKE_RAG_TABLE}."
    )
