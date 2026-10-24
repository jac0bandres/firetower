"""Meshtastic gateway: receives field messages, dedupes, triages, stores,
and sends a ticket-number acknowledgment back to the sender by DM.

Never broadcasts, never processes its own node's messages or anything
starting "FT " (our own ack prefix), and only listens on our private
channel + PSK.
"""

import logging
import os
import time

import meshtastic
import meshtastic.serial_interface
from meshtastic.protobuf import channel_pb2
from dotenv import load_dotenv
from pubsub import pub

load_dotenv()

import ai_assistant
import db
import meshtastic_packets as packets
import triage
from logging_config import setup_logging

logger = logging.getLogger(__name__)

MESHTASTIC_PORT = os.environ.get("MESHTASTIC_PORT")
ACK_PREFIX = "FT "
AI_PREFIX = "AI "
AI_COMMAND = "!ai"
AI_MAX_CHUNKS = 3
DEDUPE_WINDOW_SECONDS = 120
MAX_CHUNK_BYTES = 200
CHANNEL_INDEX = int(os.environ.get("FIRETOWER_CHANNEL_INDEX", "0"))
MAX_POSITION_AGE_SECONDS = int(os.environ.get("FIRETOWER_MAX_POSITION_AGE_SECONDS", "1800"))
CLARIFICATION_URGENCIES = {"EMERGENCY", "PRIORITY"}
CLARIFICATION_TTL_SECONDS = int(os.environ.get("FIRETOWER_CLARIFICATION_TTL_SECONDS", "600"))


def _accepted_packet(packet: dict, interface) -> bool:
    if not isinstance(packet, dict) or packet.get("channel") != CHANNEL_INDEX:
        return False
    own_number = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
    return packet.get("from") != own_number


def validate_radio_channel(interface) -> str:
    """Require the selected radio channel to have a non-default private PSK."""
    local_node = getattr(interface, "localNode", None)
    channel = local_node.getChannelByChannelIndex(CHANNEL_INDEX) if local_node else None
    if channel is None:
        raise RuntimeError(
            f"Meshtastic channel {CHANNEL_INDEX} is unavailable; configure it with a private PSK"
        )
    if channel_pb2.Channel.Role.Name(channel.role) == "DISABLED":
        raise RuntimeError(f"Meshtastic channel {CHANNEL_INDEX} is disabled")
    psk = bytes(channel.settings.psk)
    if len(psk) < 2:
        raise RuntimeError(
            f"Meshtastic channel {CHANNEL_INDEX} is unencrypted or uses the public default PSK; "
            "configure a private PSK on every radio"
        )
    name = channel.settings.name or f"channel {CHANNEL_INDEX}"
    return name


def on_receive(packet, interface):
    if not _accepted_packet(packet, interface):
        return
    sender = packets.node_id(packet)
    decoded = packet.get("decoded")
    if not isinstance(decoded, dict):
        return
    message = decoded.get("text")
    packet_id = packet.get("id")
    if not sender or not isinstance(message, str) or not message.strip():
        return
    if message.startswith(ACK_PREFIX) or packet_id is None:
        return
    packet_id = str(packet_id)
    dedupe_key = f"{sender}:{packet_id}"
    if db.is_duplicate_packet(dedupe_key):
        logger.debug("Ignored duplicate packet %s from %s", packet_id, sender)
        return

    stripped = message.strip()
    if stripped[:len(AI_COMMAND)].lower() == AI_COMMAND:
        db.mark_packet_seen(dedupe_key)
        question = stripped[len(AI_COMMAND):].strip()
        channel = packet.get("channel", CHANNEL_INDEX)
        if not question:
            send_ai_reply(sender, "Ask a question after !ai, e.g. '!ai how many open emergencies'.",
                           interface, channel)
            return
        reply = ai_assistant.answer(sender, question)
        send_ai_reply(sender, reply, interface, channel)
        return

    channel = packet.get("channel", CHANNEL_INDEX)
    pending = db.get_pending_clarification(sender, CLARIFICATION_TTL_SECONDS)
    if pending is not None:
        db.mark_packet_seen(dedupe_key)
        db.clear_pending_clarification(sender)
        extraction = triage.extract_with_model(message)
        updates = {}
        if "location" in pending["missing_fields"] and extraction.location:
            updates["location"] = extraction.location
        if "injured_count" in pending["missing_fields"] and extraction.injured_count is not None:
            updates["injured_count"] = extraction.injured_count
        if updates:
            db.update_ticket_fields(pending["ticket_code"], **updates)
            logger.info("Updated ticket %s from clarification by %s: %s",
                        pending["ticket_code"], sender, updates)
        note = "thanks, updated" if updates else "thanks, noted"
        try:
            send_ticket_note(sender, pending["ticket_code"], note, interface, channel)
        except Exception:
            logger.exception("Clarification confirmation to %s failed", sender)
        return

    result = triage.triage(message, sender)
    latest_position = db.get_node_position(sender, MAX_POSITION_AGE_SECONDS)
    hop_count = packet.get("hopsAway")
    if hop_count is None and "hopStart" in packet and "hopLimit" in packet:
        hop_count = max(0, packet["hopStart"] - packet["hopLimit"])
    ticket_code = db.next_ticket_code()
    db.insert_ticket({
        "ticket_code": ticket_code,
        "node_id": sender,
        "packet_id": packet_id,
        "received_at": packets.received_at(packet),
        "raw_text": message,
        "urgency": result.urgency,
        "location": result.location,
        "what_happened": result.what_happened,
        "injured_count": result.injured_count,
        "raised_by_keyword": result.raised_by_keyword,
        "priority_floor_applied": result.priority_floor_applied,
        "hop_count": hop_count,
        "latitude": latest_position["latitude"] if latest_position else None,
        "longitude": latest_position["longitude"] if latest_position else None,
        "position_received_at": latest_position["received_at"] if latest_position else None,
        "position_packet_json": latest_position["meshtastic_packet_json"] if latest_position else None,
        "meshtastic_packet_json": packets.packet_json(packet),
    })
    db.mark_packet_seen(dedupe_key)
    logger.info("Stored ticket %s from %s at urgency %s", ticket_code, sender, result.urgency)
    try:
        send_ack(sender, ticket_code, interface, channel)
    except Exception:
        logger.exception("Ticket %s stored, but acknowledgment to %s failed", ticket_code, sender)

    if result.urgency in CLARIFICATION_URGENCIES:
        missing = _missing_fields(result)
        if missing:
            db.set_pending_clarification(sender, ticket_code, missing)
            try:
                send_ticket_note(sender, ticket_code, _clarification_question(missing), interface, channel)
            except Exception:
                logger.exception("Clarification question to %s failed", sender)


def _missing_fields(result) -> list[str]:
    missing = []
    if not result.location:
        missing.append("location")
    if result.injured_count is None:
        missing.append("injured_count")
    return missing


def _clarification_question(missing_fields: list[str]) -> str:
    if "location" in missing_fields and "injured_count" in missing_fields:
        return "Can you share your exact location and how many people are hurt?"
    if "location" in missing_fields:
        return "What is your exact location?"
    if "injured_count" in missing_fields:
        return "How many people are hurt?"
    return "Can you share more details?"


def on_position(packet, interface):
    if not _accepted_packet(packet, interface):
        return
    sender = packets.node_id(packet)
    coordinates = packets.position(packet)
    if sender and coordinates:
        db.save_node_position(
            sender, *coordinates, packets.received_at(packet), packets.packet_json(packet)
        )


def send_ack(node_id: str, ticket_code: str, interface, channel_index: int = CHANNEL_INDEX) -> None:
    message = f"{ACK_PREFIX}{ticket_code} received"
    encoded = message.encode("utf-8")
    if len(encoded) > MAX_CHUNK_BYTES:
        raise ValueError("Acknowledgment exceeds the Meshtastic payload limit")
    interface.sendText(message, destinationId=node_id, channelIndex=channel_index)


def _chunk_text(text: str, max_bytes: int) -> list[str]:
    """Word-wrap text into chunks whose UTF-8 encoding stays under max_bytes,
    hard-splitting any single word that alone exceeds the budget."""
    chunks = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate.encode("utf-8")) <= max_bytes:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = word
        while len(current.encode("utf-8")) > max_bytes:
            piece = current.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            if not piece:
                break
            chunks.append(piece)
            current = current[len(piece):]
    if current:
        chunks.append(current)
    return chunks


def send_ai_reply(node_id: str, reply: str, interface, channel_index: int = CHANNEL_INDEX) -> None:
    """DM-only, never broadcast — chunked to the radio payload limit, capped
    at AI_MAX_CHUNKS so one answer can't flood the mesh with airtime."""
    reserve = len(AI_PREFIX.encode("utf-8")) + len(f"({AI_MAX_CHUNKS}/{AI_MAX_CHUNKS}) ".encode("utf-8"))
    budget = MAX_CHUNK_BYTES - reserve
    chunks = _chunk_text(reply.strip(), budget)

    if len(chunks) > AI_MAX_CHUNKS:
        chunks = chunks[:AI_MAX_CHUNKS]
        marker = "…"
        last = chunks[-1]
        while last and len((last + marker).encode("utf-8")) > budget:
            last = last[:-1]
        chunks[-1] = (last + marker) if last else marker

    total = len(chunks) or 1
    for index, chunk in enumerate(chunks or [""], start=1):
        label = AI_PREFIX if total == 1 else f"{AI_PREFIX}({index}/{total}) "
        interface.sendText(f"{label}{chunk}", destinationId=node_id, channelIndex=channel_index)
        if index < total:
            time.sleep(3)


def send_ticket_note(node_id: str, ticket_code: str, note: str, interface,
                      channel_index: int = CHANNEL_INDEX) -> None:
    """A short DM tagged to a ticket — the follow-up clarification question or
    the confirmation after the sender replies to one. Capped at AI_MAX_CHUNKS
    like send_ai_reply, though in practice these are short enough for one."""
    prefix = f"{ACK_PREFIX}{ticket_code}: "
    reserve = len(prefix.encode("utf-8")) + len(f"({AI_MAX_CHUNKS}/{AI_MAX_CHUNKS}) ".encode("utf-8"))
    budget = MAX_CHUNK_BYTES - reserve
    chunks = _chunk_text(note.strip(), budget) or [""]

    if len(chunks) > AI_MAX_CHUNKS:
        chunks = chunks[:AI_MAX_CHUNKS]
        marker = "…"
        last = chunks[-1]
        while last and len((last + marker).encode("utf-8")) > budget:
            last = last[:-1]
        chunks[-1] = (last + marker) if last else marker

    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        label = prefix if total == 1 else f"{ACK_PREFIX}{ticket_code} ({index}/{total}): "
        interface.sendText(f"{label}{chunk}", destinationId=node_id, channelIndex=channel_index)
        if index < total:
            time.sleep(3)


def main():
    setup_logging()
    db.init_db()
    interface = meshtastic.serial_interface.SerialInterface(devPath=MESHTASTIC_PORT)
    try:
        channel_name = validate_radio_channel(interface)
    except Exception:
        interface.close()
        raise
    own_number = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
    own_id = f"!{own_number:08x}" if own_number is not None else None
    nodes = getattr(interface, "nodes", {}) or {}
    own_node = nodes.get(own_id, {}) if own_id else {}
    user = own_node.get("user", {}) if isinstance(own_node, dict) else {}
    node_name = f"{user.get('longName', '')} {user.get('shortName', '')}".strip()
    if "bot" not in node_name.lower():
        logger.warning(
            "Gateway radio node name should contain 'bot' (reported name: %s)",
            node_name or "unknown",
        )
    logger.info("Gateway connected on private Meshtastic channel %s (%s)", CHANNEL_INDEX, channel_name)
    pub.subscribe(on_receive, "meshtastic.receive.text")
    pub.subscribe(on_position, "meshtastic.receive.position")
    logger.info("Firetower gateway listening")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        interface.close()


if __name__ == "__main__":
    main()
