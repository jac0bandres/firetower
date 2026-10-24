"""`!ai <question>` handler: answers questions sent over the Meshtastic
channel using everything triaged on that channel as context, in a
per-sender conversation thread that persists across messages.

Always runs on the local model — same offline guarantee as triage.py —
and never claims to take real-world action (e.g. calling 911) itself.
"""

import logging
import os

import ollama

import db

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "qwen3:4b")
TRIAGE_MODEL_BACKUP = os.environ.get("TRIAGE_MODEL_BACKUP", "llama3.2:3b")

CONTEXT_TICKET_LIMIT = 40
UNAVAILABLE_REPLY = "The local AI model is unavailable right now. Try again shortly."

SYSTEM_PROMPT = (
    "You are Firetower's radio assistant, reached over a Meshtastic mesh "
    "channel when someone sends '!ai <question>'. Answer using only the "
    "channel history and conversation below. Keep replies under about 400 "
    "characters — they go out over a slow radio link in small chunks. If you "
    "don't know, say so plainly. You cannot call 911 or take any real-world "
    "action; never claim to have done so. /no_think"
)

_client = ollama.Client(host=OLLAMA_HOST)


def build_channel_context() -> str:
    tickets = sorted(db.list_tickets(), key=lambda t: t["received_at"])[-CONTEXT_TICKET_LIMIT:]
    if not tickets:
        return "No messages have been received on this channel yet."

    lines = []
    for ticket in tickets:
        parts = [ticket["urgency"], ticket["node_id"], ticket["what_happened"] or ticket["raw_text"]]
        if ticket["location"]:
            parts.append(f"near {ticket['location']}")
        if ticket["injured_count"]:
            parts.append(f"{ticket['injured_count']} hurt")
        lines.append(f"- [{ticket['ticket_code']}] " + " | ".join(parts))
    return "\n".join(lines)


def answer(node_id: str, question: str) -> str:
    thread_id = db.get_or_create_ai_thread(node_id)
    db.add_ai_message(thread_id, "user", question)

    history = db.list_ai_thread_messages(thread_id)
    context = build_channel_context()
    messages = [{"role": "system", "content": f"{SYSTEM_PROMPT}\n\nChannel history:\n{context}"}]
    messages += [{"role": entry["role"], "content": entry["content"]} for entry in history]

    reply_text = None
    for model in (TRIAGE_MODEL, TRIAGE_MODEL_BACKUP):
        try:
            response = _client.chat(model=model, messages=messages, options={"temperature": 0.2}, think=False)
            # think=False doesn't reliably suppress qwen3's reasoning trace on
            # unconstrained chat calls (unlike triage.py's format-constrained
            # ones) — keep only what follows a closing </think>, if present.
            content = response.message.content
            reply_text = (content.split("</think>")[-1] if "</think>" in content else content).strip()
            logger.info("ai_assistant reply model=%s node_id=%s thread_id=%s", model, node_id, thread_id)
            break
        except Exception:
            logger.warning("ai_assistant model failed model=%s node_id=%s", model, node_id, exc_info=True)

    if not reply_text:
        reply_text = UNAVAILABLE_REPLY
        logger.error("ai_assistant both models unavailable node_id=%s", node_id)

    db.add_ai_message(thread_id, "assistant", reply_text)
    return reply_text
