"""`!ai <question>` handler: answers questions sent over the Meshtastic
channel using everything triaged on that channel as context, in a
per-sender conversation thread that persists across messages.

Tries DigitalOcean's cloud model first when configured and online (a more
capable model for this conversational feature), then always falls back to
the local model — so this still works fully offline even if the cloud tier
is unset, unreachable, or fails. Never claims to take real-world action
(e.g. calling 911) itself.
"""

import logging
import os

import ollama
import openai

import db
import router

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
TRIAGE_MODEL = os.environ.get("TRIAGE_MODEL", "qwen3:4b")
TRIAGE_MODEL_BACKUP = os.environ.get("TRIAGE_MODEL_BACKUP", "llama3.2:3b")

DIGITALOCEAN_API_KEY = os.environ.get("DIGITALOCEAN_API_KEY", "")
DIGITALOCEAN_MODEL = os.environ.get("DIGITALOCEAN_MODEL", "openai-gpt-oss-20b")
DIGITALOCEAN_BASE_URL = os.environ.get("DIGITALOCEAN_BASE_URL", "")

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


def _cloud_configured() -> bool:
    return bool(DIGITALOCEAN_API_KEY and DIGITALOCEAN_BASE_URL)


def _cloud_client() -> openai.OpenAI | None:
    if not _cloud_configured():
        return None
    return openai.OpenAI(api_key=DIGITALOCEAN_API_KEY, base_url=DIGITALOCEAN_BASE_URL)


def _call_cloud(messages: list[dict]) -> str:
    client = _cloud_client()
    response = client.chat.completions.create(
        model=DIGITALOCEAN_MODEL, messages=messages, temperature=0.2,
    )
    return response.choices[0].message.content


def _call_local(model: str, messages: list[dict]) -> str:
    response = _client.chat(model=model, messages=messages, options={"temperature": 0.2}, think=False)
    return response.message.content


def _strip_thinking(content: str) -> str:
    # think=False doesn't reliably suppress qwen3's reasoning trace on
    # unconstrained chat calls (unlike triage.py's format-constrained ones)
    # — keep only what follows a closing </think>, if present. Harmless
    # no-op for models (local or cloud) that never emit one.
    return (content.split("</think>")[-1] if "</think>" in content else content).strip()


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

    tiers = []
    if _cloud_configured() and router.is_online():
        tiers.append(("digitalocean:" + DIGITALOCEAN_MODEL, lambda: _call_cloud(messages)))
    tiers.append((TRIAGE_MODEL, lambda m=TRIAGE_MODEL: _call_local(m, messages)))
    tiers.append((TRIAGE_MODEL_BACKUP, lambda m=TRIAGE_MODEL_BACKUP: _call_local(m, messages)))

    reply_text = None
    for label, call in tiers:
        try:
            reply_text = _strip_thinking(call())
            logger.info("ai_assistant reply tier=%s node_id=%s thread_id=%s", label, node_id, thread_id)
            break
        except Exception:
            logger.warning("ai_assistant tier failed tier=%s node_id=%s", label, node_id, exc_info=True)

    if not reply_text:
        reply_text = UNAVAILABLE_REPLY
        logger.error("ai_assistant all tiers unavailable node_id=%s", node_id)

    db.add_ai_message(thread_id, "assistant", reply_text)
    return reply_text
