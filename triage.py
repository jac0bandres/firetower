"""Urgency triage and extraction: keyword safety net + DigitalOcean model.

Precedence: EMERGENCY > PRIORITY > WELFARE > ROUTINE. The keyword net can only
raise urgency, never lower it. A priority-node floor (see db.priority_nodes)
additionally guarantees a sender's message never ranks below PRIORITY.

Extraction calls the configured DigitalOcean model. If that model is
unconfigured, offline, or the call fails, urgency falls back to the keyword
net. No local Ollama model is called.
"""

import json
import logging
import re

from pydantic import BaseModel

import ai_assistant
import db

logger = logging.getLogger(__name__)

URGENCY_LEVELS = ["ROUTINE", "WELFARE", "PRIORITY", "EMERGENCY"]

EMERGENCY_KEYWORDS = ["fire", "can't breathe", "bleeding", "unconscious", "trapped"]
PRIORITY_KEYWORDS = ["insulin", "medication", "injured", "hurt"]

# Fields the model should produce. raised_by_keyword and the priority floor
# are applied afterward in triage(), so neither is asked of the model.
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
from the message below. Respond with JSON matching this schema only.

Schema: {schema}

Message: "{text}"
"""


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


def _keyword_only(text: str) -> TriageResult:
    floor = keyword_floor(text)
    logger.error("cloud extraction unavailable, degrading to keyword-only urgency=%s", floor or "ROUTINE")
    return TriageResult(urgency=floor or "ROUTINE", raised_by_keyword=bool(floor))


def _extraction_payload(content: str) -> dict:
    text = ai_assistant._strip_thinking(content).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("extraction payload was not an object")
    urgency = payload.get("urgency")
    if isinstance(urgency, str):
        payload["urgency"] = urgency.strip().upper()
    if payload.get("urgency") not in URGENCY_LEVELS:
        raise ValueError("invalid urgency")
    return payload


def extract_with_model(text: str) -> TriageResult:
    prompt = (
        PROMPT_TEMPLATE
        .replace("{schema}", json.dumps(EXTRACTION_SCHEMA))
        .replace("{text}", text)
    )
    try:
        content = ai_assistant.complete_raw(
            [{"role": "user", "content": prompt}],
            temperature=0,
        )
        logger.debug("extraction raw model output: %r", content)
        result = TriageResult.model_validate(_extraction_payload(content))
        logger.info(
            "extraction ok model=%s urgency=%s location=%r injured=%s what=%r",
            ai_assistant.DIGITALOCEAN_MODEL,
            result.urgency, result.location, result.injured_count, result.what_happened,
        )
        return result
    except Exception:
        logger.warning("cloud extraction failed, falling back to keyword-only", exc_info=True)
        return _keyword_only(text)


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
