"""Choose response teams from a ticket report.

Dispatch is an in-app record on the coordinator board. It never places a
phone call and never contacts 911 or a fire, police, or other public-safety
system.
"""

import re

TEAMS = ("fire", "police", "ems", "rescue")

TEAM_LABELS = {
    "fire": "Fire",
    "police": "Police",
    "ems": "EMS",
    "rescue": "Rescue",
}

_RULES = (
    ("fire", re.compile(
        r"\b(?:fire|smoke|flames?|burning|wildfire|blaze|explosion|gas leak|gas smell|sparking)\b",
        re.IGNORECASE,
    )),
    ("police", re.compile(
        r"\b(?:police|gun|gunshot|shooting|shots fired|weapon|assault|robbery|"
        r"theft|break-in|break in|intruder|stab(?:bed|bing)?|attack(?:ed)?)\b",
        re.IGNORECASE,
    )),
    ("ems", re.compile(
        r"\b(?:injur(?:y|ed|ies)|hurt|bleeding|bleed|unconscious|unresponsive|"
        r"passed out|heart attack|chest pain|not breathing|can't breathe|"
        r"cant breathe|cannot breathe|ambulance|overdose|insulin|medical|medic|cpr)\b",
        re.IGNORECASE,
    )),
    ("rescue", re.compile(
        r"\b(?:trapped|pinned|collapsed?|washed out|in the water|swift water|"
        r"search and rescue|missing)\b",
        re.IGNORECASE,
    )),
)


def suggest_teams(ticket: dict) -> list[str]:
    """Return team ids in stable order. An emergency with no match gets EMS."""
    text = " ".join(
        part for part in (ticket.get("what_happened"), ticket.get("raw_text")) if part
    )
    text = re.sub(r"\b(?:nobody|no one|not) hurt\b", " ", text, flags=re.IGNORECASE)
    matched = {team for team, pattern in _RULES if pattern.search(text)}
    injured = ticket.get("injured_count")
    if type(injured) is int and injured > 0:
        matched.add("ems")
    if not matched and ticket.get("urgency") == "EMERGENCY":
        matched.add("ems")
    return [team for team in TEAMS if team in matched]


def normalize_teams(teams: list[str]) -> list[str]:
    if not isinstance(teams, (list, tuple)) or any(team not in TEAMS for team in teams):
        raise ValueError(f"Invalid dispatch teams: {teams}")
    ordered = [team for team in TEAMS if team in teams]
    if not ordered:
        raise ValueError("Dispatch requires at least one team")
    return ordered
