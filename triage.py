"""Urgency triage and extraction: keyword safety net + local Ollama model.

Precedence: EMERGENCY > PRIORITY > WELFARE > ROUTINE. The keyword net can only
raise urgency, never lower it. A priority-node floor (see db.priority_nodes)
additionally guarantees a sender's message never ranks below PRIORITY.
"""

import os

from pydantic import BaseModel

TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "qwen3:4b")
TRIAGE_MODEL_BACKUP = os.environ.get("TRIAGE_MODEL_BACKUP", "llama3.2:3b")

URGENCY_LEVELS = ["ROUTINE", "WELFARE", "PRIORITY", "EMERGENCY"]

EMERGENCY_KEYWORDS = ["fire", "can't breathe", "bleeding", "unconscious", "trapped"]
PRIORITY_KEYWORDS = ["insulin", "medication", "injured", "hurt"]


class TriageResult(BaseModel):
    urgency: str
    location: str | None = None
    what_happened: str | None = None
    injured_count: int | None = None
    raised_by_keyword: bool = False


def keyword_floor(text: str) -> str | None:
    raise NotImplementedError


def extract_with_model(text: str) -> TriageResult:
    raise NotImplementedError


def apply_priority_floor(result: TriageResult, node_id: str) -> TriageResult:
    raise NotImplementedError


def triage(text: str, node_id: str) -> TriageResult:
    raise NotImplementedError
