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

import db
import meshtastic_packets as packets
import triage
from logging_config import setup_logging

logger = logging.getLogger(__name__)

MESHTASTIC_PORT = os.environ.get("MESHTASTIC_PORT")
ACK_PREFIX = "FT "
DEDUPE_WINDOW_SECONDS = 120
MAX_CHUNK_BYTES = 200
CHANNEL_INDEX = int(os.environ.get("FIRETOWER_CHANNEL_INDEX", "0"))
MAX_POSITION_AGE_SECONDS = int(os.environ.get("FIRETOWER_MAX_POSITION_AGE_SECONDS", "1800"))


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
        send_ack(sender, ticket_code, interface, packet["channel"])
    except Exception:
        logger.exception("Ticket %s stored, but acknowledgment to %s failed", ticket_code, sender)


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
