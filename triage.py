"""Urgency triage and extraction: keyword safety net + local Ollama model.

Precedence: EMERGENCY > PRIORITY > WELFARE > ROUTINE. The keyword net can only
raise urgency, never lower it. A priority-node floor (see db.priority_nodes)
additionally guarantees a sender's message never ranks below PRIORITY.
"""

import logging
import os

import ollama
from pydantic import BaseModel

import db

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "qwen3:4b")
TRIAGE_MODEL_BACKUP = os.environ.get("TRIAGE_MODEL_BACKUP", "llama3.2:3b")

URGENCY_LEVELS = ["ROUTINE", "WELFARE", "PRIORITY", "EMERGENCY"]

EMERGENCY_KEYWORDS = ["fire", "can't breathe", "bleeding", "unconscious", "trapped"]
PRIORITY_KEYWORDS = ["insulin", "medication", "injured", "hurt"]

# Passed to Ollama's `format` param. Only fields the model should produce —
# raised_by_keyword and the priority floor are applied afterward in triage().
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "urgency": {
            "type": "string",
            "enum": URGENCY_LEVELS,
        },
        "location": {"type": ["string", "null"]},
        "what_happened": {"type": ["string", "null"]},
        "injured_count": {"type": ["integer", "null"]},
    },
    "required": ["urgency", "location", "what_happened", "injured_count"],
}

PROMPT_TEMPLATE = """You triage short field radio messages during a disaster response.
Classify urgency as one of EMERGENCY, PRIORITY, WELFARE, or ROUTINE, and extract
the location, what happened, and injured_count (an integer, or null if unknown)
from the message below. Respond with JSON matching the schema only.

Message: "{text}"
"""

_client = ollama.Client(host=OLLAMA_HOST)


class TriageResult(BaseModel):
    urgency: str
    location: str | None = None
    what_happened: str | None = None
    injured_count: int | None = None
    raised_by_keyword: bool = False
    priority_floor_applied: bool = False


def _rank(level: str) -> int:
    return URGENCY_LEVELS.index(level)


def keyword_floor(text: str) -> str | None:
    lowered = text.lower()
    if any(keyword in lowered for keyword in EMERGENCY_KEYWORDS):
        return "EMERGENCY"
    if any(keyword in lowered for keyword in PRIORITY_KEYWORDS):
        return "PRIORITY"
    return None


def extract_with_model(text: str) -> TriageResult:
    prompt = PROMPT_TEMPLATE.format(text=text)
    for model in (TRIAGE_MODEL, TRIAGE_MODEL_BACKUP):
        try:
            response = _client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                format=EXTRACTION_SCHEMA,
                options={"temperature": 0},
                think=False,
            )
            result = TriageResult.model_validate_json(response.message.content)
            logger.info("extraction ok model=%s urgency=%s", model, result.urgency)
            return result
        except Exception:
            logger.warning("extraction failed model=%s, falling back", model, exc_info=True)

    # Both models failed (unreachable, bad JSON, etc.) — degrade to keyword-only
    # rather than dropping the message.
    floor = keyword_floor(text)
    logger.error("both models unreachable, degrading to keyword-only urgency=%s", floor or "ROUTINE")
    return TriageResult(urgency=floor or "ROUTINE", raised_by_keyword=bool(floor))


def apply_priority_floor(result: TriageResult, node_id: str) -> TriageResult:
    priority_nodes = db.list_priority_nodes()
    if node_id in priority_nodes and _rank(result.urgency) < _rank("PRIORITY"):
        logger.info("priority floor applied node_id=%s role=%s", node_id, priority_nodes[node_id])
        result.urgency = "PRIORITY"
        result.priority_floor_applied = True
    return result


def triage(text: str, node_id: str) -> TriageResult:
    result = extract_with_model(text)

    floor = keyword_floor(text)
    if floor and _rank(floor) > _rank(result.urgency):
        logger.info("keyword net raised urgency node_id=%s %s -> %s", node_id, result.urgency, floor)
        result.urgency = floor
        result.raised_by_keyword = True

    result = apply_priority_floor(result, node_id)
    logger.info("triage result node_id=%s urgency=%s", node_id, result.urgency)
    return result
