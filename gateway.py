"""Meshtastic gateway: receives field messages, dedupes, triages, stores,
and sends a ticket-number acknowledgment back to the sender by DM.

Never broadcasts, never processes its own node's messages or anything
starting with our own gateway name (our ack prefix, see GATEWAY_NAME),
and only listens on our private channel + PSK.
"""

import json
import logging
import os
import re
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
# Replies are tagged with the gateway's own radio node name (e.g. "Responder"),
# not a generic "FT " label, so a sender can tell which gateway answered.
# main() overwrites this with the device's actual configured name at startup;
# this default only applies to tests / direct calls that skip main().
GATEWAY_NAME = os.environ.get("FIRETOWER_GATEWAY_NAME", "Firetower")
AI_PREFIX = "AI "
AI_COMMAND = "!ai"
AI_MAX_CHUNKS = 3
DEDUPE_WINDOW_SECONDS = 120
MAX_CHUNK_BYTES = 200
CHANNEL_INDEX = int(os.environ.get("FIRETOWER_CHANNEL_INDEX", "0"))
MAX_POSITION_AGE_SECONDS = int(os.environ.get("FIRETOWER_MAX_POSITION_AGE_SECONDS", "1800"))
CLARIFICATION_URGENCIES = {"EMERGENCY", "PRIORITY"}
CLARIFICATION_TTL_SECONDS = int(os.environ.get("FIRETOWER_CLARIFICATION_TTL_SECONDS", "600"))
# Gap before sending the clarification question after the ack. Confirmed
# live: two RELIABLE sends back-to-back (~1s apart) can collide on the
# half-duplex radio — the ack gets delivered, the question doesn't.
CLARIFICATION_SEND_DELAY_SECONDS = int(os.environ.get("FIRETOWER_CLARIFICATION_SEND_DELAY_SECONDS", "5"))


def _accepted_packet(packet: dict, interface) -> bool:
    if not isinstance(packet, dict):
        return False
    # protobuf-to-dict conversion omits `channel` entirely when it's 0 (the
    # default value, and our primary channel) — a packet with no `channel`
    # key is on channel 0, not "no channel". Missing != rejected.
    channel = packet.get("channel")
    if channel is None:
        channel = 0
    if channel != CHANNEL_INDEX:
        logger.debug("Rejected packet: channel=%s (want %s) from=%s",
                      channel, CHANNEL_INDEX, packet.get("from"))
        return False
    own_number = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
    accepted = packet.get("from") != own_number
    if not accepted:
        logger.debug("Rejected packet: from our own node %s", own_number)
    return accepted


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
    logger.debug("on_receive fired: channel=%s from=%s portnum=%s",
                  packet.get("channel"), packet.get("from"),
                  packet.get("decoded", {}).get("portnum") if isinstance(packet, dict) else None)
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
    if message.startswith(f"{GATEWAY_NAME} ") or packet_id is None:
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
        ticket_code = pending["ticket_code"]
        extraction = triage.extract_with_model(message)
        ticket = _ticket_row(ticket_code) or {}
        steps = pending.get("steps") or []
        current_step = steps[0] if steps else None
        updates = {}
        # A later landmark or address reply can still fill an empty location.
        # An address answer replaces a vaguer place, because that reply is the
        # street or intersection the coordinator asked for.
        if extraction.location and (not ticket.get("location") or current_step == "address"):
            updates["location"] = extraction.location
        if ticket.get("injured_count") is None and extraction.injured_count is not None:
            updates["injured_count"] = extraction.injured_count
        if not updates and _looks_like_new_report(message, extraction, ticket, current_step):
            # The sender reported a different incident instead of answering.
            # Drop the pending question and open a ticket for the new report.
            logger.info(
                "Message from %s during %s follow-up names a new place (%s); opening a new ticket",
                sender, ticket_code, extraction.location,
            )
            db.clear_pending_clarification(sender)
            pending = None
    if pending is not None:
        db.append_clarification(ticket_code, response=message)
        if updates:
            db.update_ticket_fields(ticket_code, **updates)
            if "location" in updates:
                db.upsert_person(sender, last_known_address=updates["location"])
            logger.info("Updated ticket %s from clarification by %s: %s",
                        ticket_code, sender, updates)
            ticket = _ticket_row(ticket_code) or ticket
        remaining = _advance_clarification_steps(steps, ticket, pending["missing_fields"])
        if remaining:
            db.set_pending_clarification(
                sender, ticket_code, _missing_from_ticket(ticket), remaining,
                keep_created_at=True,
            )
            logger.info("Next clarification for %s is %s", ticket_code, remaining[0])
            question = _question_for_step(
                remaining[0], _missing_from_ticket(ticket),
                ticket.get("raw_text") or message, ticket.get("location"),
            )
            _record_and_send_question(sender, ticket_code, question, interface, channel)
            return
        db.clear_pending_clarification(sender)
        recorded = []
        if "location" in updates:
            recorded.append(f"location: {updates['location']}")
        if "injured_count" in updates:
            recorded.append(f"injured: {updates['injured_count']}")
        note = (
            f"Updated: {'; '.join(recorded)}. " if recorded else
            "Thanks, your reply is on the ticket. "
        )
        note += f"{ticket.get('urgency', 'PRIORITY')}; queued for coordinator review."
        try:
            send_ticket_note(sender, ticket_code, note, interface, channel)
        except Exception:
            logger.exception("Clarification confirmation to %s failed", sender)
        return

    result = triage.triage(message, sender)
    if not result.location:
        person = db.get_person(sender)
        if person and person.get("last_known_address"):
            result.location = person["last_known_address"]
            logger.info("Autofilled location for %s from known profile: %s", sender, result.location)
    if result.location:
        db.upsert_person(sender, last_known_address=result.location)

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
        send_ack(sender, ticket_code, interface, channel, result=result)
    except Exception:
        logger.exception("Ticket %s stored, but acknowledgment to %s failed", ticket_code, sender)

    has_gps = _has_gps(latest_position)
    steps = _clarification_steps(result, has_gps, message)
    if steps:
        missing = _missing_fields(result, message)
        db.set_pending_clarification(sender, ticket_code, missing, steps)
        if not has_gps:
            logger.info(
                "No GPS fix for %s on %s; clarification steps: %s",
                sender, ticket_code, ",".join(steps),
            )
        question = _question_for_step(steps[0], missing, message, result.location)
        # Give the ack time to transmit before the next DM. The same gap is
        # used before every later question so two RELIABLE sends do not
        # collide on the half-duplex radio.
        _record_and_send_question(sender, ticket_code, question, interface, channel)


def _missing_fields(result, text: str | None = None) -> list[str]:
    missing = []
    if not result.location:
        missing.append("location")
    if result.injured_count is None and not _is_missing_person(text, result.what_happened):
        missing.append("injured_count")
    return missing


# A missing-person sender usually cannot know an injury count, so those
# reports skip the injury question and ask for a description instead.
_MISSING_PERSON = re.compile(
    r"\b(?:missing|can'?t find|cannot find|unaccounted|"
    r"haven'?t (?:seen|heard from)|last seen)\b",
    re.I,
)
_CLOTHING = re.compile(
    r"\b(?:wearing|jacket|shirt|t-shirt|coat|hoodie|sweater|sweatshirt|dress|"
    r"hat|cap|pants|jeans|shorts|boots|shoes|raincoat|vest)\b",
    re.I,
)


def _is_missing_person(*texts: str | None) -> bool:
    return any(text and _MISSING_PERSON.search(text) for text in texts)


def _same_place(first: str, second: str) -> bool:
    a = " ".join(re.findall(r"[a-z0-9]+", first.lower()))
    b = " ".join(re.findall(r"[a-z0-9]+", second.lower()))
    return bool(a and b) and (a in b or b in a)


def _looks_like_new_report(message: str, extraction, ticket: dict, step: str | None) -> bool:
    """True when a "reply" is really a separate urgent report.

    It must fill none of the missing fields, be urgent on its own, and name a
    place different from the ticket's. Landmark and address answers always
    name another place, so those steps never split.
    """
    if step in ("landmark", "address"):
        return False
    urgent = (
        extraction.urgency in CLARIFICATION_URGENCIES
        or triage.keyword_floor(message) in CLARIFICATION_URGENCIES
    )
    known = ticket.get("location")
    new_place = extraction.location
    return bool(urgent and known and new_place and not _same_place(known, new_place))


# A stated place counts as a street address when it names a road or starts
# with a street number. Vague places ("the old mill") still need the address question.
_ADDRESS_HINT = re.compile(
    r"\b(?:street|st|avenue|ave|road|rd|blvd|boulevard|highway|hwy|drive|dr|"
    r"lane|ln|court|ct|parkway|trail)\b|\d{1,6}\s+[A-Za-z]",
    re.I,
)


def _has_gps(position_or_ticket) -> bool:
    if not position_or_ticket:
        return False
    return (
        position_or_ticket.get("latitude") is not None
        and position_or_ticket.get("longitude") is not None
    )


def _looks_like_address(location: str | None) -> bool:
    return bool(location and _ADDRESS_HINT.search(location))


def _missing_from_ticket(ticket: dict) -> list[str]:
    missing = []
    if not ticket.get("location"):
        missing.append("location")
    if (
        ticket.get("injured_count") is None
        and not _is_missing_person(ticket.get("raw_text"), ticket.get("what_happened"))
    ):
        missing.append("injured_count")
    return missing


def _ticket_row(ticket_code: str) -> dict | None:
    with db.connect() as connection:
        row = connection.execute(
            "SELECT * FROM tickets WHERE ticket_code = ?", (ticket_code,)
        ).fetchone()
    return dict(row) if row else None


def _clarification_steps(result, has_gps: bool, text: str | None = None) -> list[str]:
    """One follow-up when the ticket already has a GPS fix.

    With no fix, keep asking for the place details a coordinator needs:
    where they are when that is still missing, then a landmark, then a
    street or address when we do not already have one. A missing-person
    report that does not describe the person also gets a last-seen question.
    """
    if result.urgency not in CLARIFICATION_URGENCIES:
        return []
    missing = _missing_fields(result, text)
    describe = (
        _is_missing_person(text, result.what_happened)
        and not _CLOTHING.search(text or "")
    )
    if has_gps:
        if missing:
            return ["fields"]
        return ["last_seen"] if describe else []
    steps = ["fields"] if missing else []
    if describe:
        steps.append("last_seen")
    steps.append("landmark")
    if not _looks_like_address(getattr(result, "location", None)):
        steps.append("address")
    return steps


def _prune_clarification_steps(steps: list[str], ticket: dict) -> list[str]:
    address_known = _looks_like_address(ticket.get("location"))
    pruned = []
    for step in steps:
        if step == "address" and address_known:
            continue
        if step == "injured_count" and ticket.get("injured_count") is not None:
            continue
        if step == "fields" and not _missing_from_ticket(ticket):
            continue
        pruned.append(step)
    return pruned


def _advance_clarification_steps(steps: list[str], ticket: dict, asked_missing: list[str]) -> list[str]:
    # A GPS fix, including one that arrives mid-conversation, ends the
    # location drill. The single-question path also ends after this reply.
    if _has_gps(ticket):
        return []
    remaining = list(steps[1:]) if steps else []
    if (
        steps
        and steps[0] == "fields"
        and "injured_count" in asked_missing
        and ticket.get("injured_count") is None
        and "injured_count" not in remaining
    ):
        remaining.append("injured_count")
    return _prune_clarification_steps(remaining, ticket)


def _gps_detail_question(step: str) -> str:
    if step == "landmark":
        return "What landmark is nearest to you?"
    if step == "address":
        return "What street or address are you near?"
    if step == "where":
        return "Where are you right now?"
    if step == "injured_count":
        return "How many people are hurt?"
    if step == "last_seen":
        return "When were they last seen, and what were they wearing?"
    return "Can you share more details?"


def _question_for_step(step: str, missing: list[str], raw_text: str,
                       known_location: str | None) -> str:
    if step == "fields":
        return _ai_clarification_question(missing or ["location"], raw_text)
    if step == "injured_count":
        return _ai_clarification_question(["injured_count"], raw_text)
    if step == "where":
        return _ai_clarification_question(["location"], raw_text)
    if step == "last_seen":
        return _gps_detail_question(step)
    return _ai_gps_detail_question(step, raw_text, known_location)


def _record_and_send_question(sender: str, ticket_code: str, question: str,
                              interface, channel: int) -> None:
    db.append_clarification(ticket_code, question=question)
    time.sleep(CLARIFICATION_SEND_DELAY_SECONDS)
    try:
        send_ticket_note(sender, ticket_code, question, interface, channel)
    except Exception:
        logger.exception("Clarification question to %s failed", sender)


def _clarification_question(missing_fields: list[str]) -> str:
    """Static template — the fallback used if the AI-generated question below
    is unavailable (model down, bad output), so a follow-up always goes out."""
    if "location" in missing_fields and "injured_count" in missing_fields:
        return "Can you share your exact location and how many people are hurt?"
    if "location" in missing_fields:
        return "What is your exact location?"
    if "injured_count" in missing_fields:
        return "How many people are hurt?"
    return "Can you share more details?"


def _accepted_question_text(content) -> str | None:
    """One short ASCII radio question, or None when the model text is unusable."""
    if not isinstance(content, str) or re.search(r"</?think\b", content, re.I):
        return None
    payload = json.loads(content)
    if not isinstance(payload, dict) or set(payload) != {"question"}:
        return None
    if not isinstance(payload["question"], str):
        return None
    question = payload["question"].strip()
    if (
        not question or len(question) >= 80 or not question.endswith("?")
        or question.count("?") != 1 or not question.isascii()
        or any(ord(character) < 32 or ord(character) == 127 for character in question)
    ):
        return None
    return question


def _ai_clarification_question(missing_fields: list[str], raw_text: str) -> str:
    fallback = _clarification_question(missing_fields)
    fields_desc = " and ".join(missing_fields)
    prompt = (
        f'A field radio report said: "{raw_text}". It is missing: {fields_desc}. '
        "Write ONE short, natural question (under 80 characters) asking only for "
        "that missing information, to send back over a slow radio link. For "
        "injuries, ask how many people are hurt, or ask whether a named person "
        "is hurt when the report is about one person. Return only a JSON object "
        'with a "question" string, without reasoning or preamble.'
    )
    try:
        question = _accepted_question_text(ai_assistant.complete_raw(
            [{"role": "user", "content": prompt}],
            temperature=0,
        ))
    except Exception:
        logger.warning("AI clarification question generation failed, using template", exc_info=True)
        return fallback
    if question is None:
        return fallback
    asks_location = re.search(r"\b(where|location|address|landmark|intersection)\b", question, re.I)
    # "How many are hurt?" or a yes/no form such as "Is your son hurt?"
    asks_count = re.search(
        r"\b(how many|number of|is|are|was|were|anyone|anybody)\b[^?]*"
        r"\b(injur\w*|hurt|wounded)\b",
        question, re.I,
    )
    if "location" in missing_fields and not asks_location:
        return fallback
    if "injured_count" in missing_fields and not asks_count:
        return fallback
    return question


def _ai_gps_detail_question(step: str, raw_text: str, known_location: str | None = None) -> str:
    """One landmark or address question. Static template if the model cannot."""
    fallback = _gps_detail_question(step)
    if step == "landmark":
        ask = "the nearest landmark"
        pattern = re.compile(r"\b(landmark|nearest|nearby|intersection|building)\b", re.I)
    elif step == "address":
        ask = "the street address or intersection"
        pattern = re.compile(r"\b(address|street|road|intersection)\b", re.I)
    else:
        return fallback
    known = f' Known location so far: "{known_location}".' if known_location else ""
    prompt = (
        f'A field radio report said: "{raw_text}".{known} '
        "The sender GPS position did not arrive. "
        f"Write ONE short question (under 80 characters) asking only for {ask}, "
        "to send back over a slow radio link. Return only "
        'a JSON object with a "question" string, without reasoning or preamble.'
    )
    try:
        question = _accepted_question_text(ai_assistant.complete_raw(
            [{"role": "user", "content": prompt}],
            temperature=0,
        ))
    except Exception:
        logger.warning("AI GPS clarification question failed, using template", exc_info=True)
        return fallback
    if question is None or pattern.search(question) is None:
        return fallback
    return question


def on_position(packet, interface):
    if not _accepted_packet(packet, interface):
        return
    sender = packets.node_id(packet)
    coordinates = packets.position(packet)
    if sender and coordinates:
        db.save_node_position(
            sender, *coordinates, packets.received_at(packet), packets.packet_json(packet)
        )


def on_simulator_position(packet, interface):
    """The team's virtual-node test harness sends GPS as JSON over
    PRIVATE_APP rather than a standard POSITION_APP packet — see
    meshtastic_packets.simulator_position(). Saved the same way real GPS is,
    so it's indistinguishable to the rest of the pipeline."""
    if not _accepted_packet(packet, interface):
        return
    sender = packets.node_id(packet)
    if not sender:
        return
    coordinates = packets.simulator_position(packet)
    if coordinates:
        received_at = packets.received_at(packet)
        packet_json = packets.packet_json(packet)
        db.save_node_position(
            sender, *coordinates, received_at, packet_json
        )
        reply_id = packet.get("decoded", {}).get("replyId")
        if reply_id is not None:
            db.attach_ticket_position(sender, str(reply_id), *coordinates, received_at, packet_json)
    name = packets.simulator_name(packet)
    if name:
        db.upsert_person(sender, display_name=name)


def send_ack(node_id: str, ticket_code: str, interface, channel_index: int = CHANNEL_INDEX,
             result=None) -> None:
    urgency = f"; {result.urgency}" if result is not None else ""
    message = f"{GATEWAY_NAME} {ticket_code} received{urgency}; queued for coordinator review."
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


def _send_chunked(node_id: str, text: str, label: str, multi_label, interface,
                   channel_index: int = CHANNEL_INDEX) -> None:
    """DM-only, never broadcast — chunked to the radio payload limit, capped
    at AI_MAX_CHUNKS so one message can't flood the mesh with airtime. `label`
    prefixes a single-chunk send; `multi_label(index, total)` prefixes each
    piece when more than one chunk is needed."""
    reserve = len(label.encode("utf-8")) + len(f"({AI_MAX_CHUNKS}/{AI_MAX_CHUNKS}) ".encode("utf-8"))
    budget = MAX_CHUNK_BYTES - reserve
    chunks = _chunk_text(text.strip(), budget) or [""]

    if len(chunks) > AI_MAX_CHUNKS:
        chunks = chunks[:AI_MAX_CHUNKS]
        marker = "…"
        last = chunks[-1]
        while last and len((last + marker).encode("utf-8")) > budget:
            last = last[:-1]
        chunks[-1] = (last + marker) if last else marker

    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        prefix = label if total == 1 else multi_label(index, total)
        interface.sendText(f"{prefix}{chunk}", destinationId=node_id, channelIndex=channel_index)
        if index < total:
            time.sleep(3)


def send_ai_reply(node_id: str, reply: str, interface, channel_index: int = CHANNEL_INDEX) -> None:
    _send_chunked(node_id, reply, AI_PREFIX, lambda i, n: f"{AI_PREFIX}({i}/{n}) ", interface, channel_index)


def send_ticket_note(node_id: str, ticket_code: str, note: str, interface,
                      channel_index: int = CHANNEL_INDEX) -> None:
    """A short DM tagged to a ticket — the follow-up clarification question or
    the confirmation after the sender replies to one."""
    _send_chunked(
        node_id, note, f"{GATEWAY_NAME} {ticket_code}: ",
        lambda i, n: f"{GATEWAY_NAME} {ticket_code} ({i}/{n}): ",
        interface, channel_index,
    )


def main():
    global GATEWAY_NAME
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
    if user.get("longName"):
        GATEWAY_NAME = user["longName"]
    logger.info("Gateway connected on private Meshtastic channel %s (%s) as %s",
                CHANNEL_INDEX, channel_name, GATEWAY_NAME)
    pub.subscribe(on_receive, "meshtastic.receive.text")
    pub.subscribe(on_position, "meshtastic.receive.position")
    pub.subscribe(on_simulator_position, "meshtastic.receive.data.PRIVATE_APP")
    logger.info("Firetower gateway listening")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        interface.close()


if __name__ == "__main__":
    main()
