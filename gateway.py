"""Meshtastic gateway: receives field messages, dedupes, triages, stores,
and sends a ticket-number acknowledgment back to the sender by DM.

Never broadcasts, never processes its own node's messages or anything
starting "FT " (our own ack prefix), and only listens on our private
channel + PSK.
"""

import os

import meshtastic
import meshtastic.serial_interface
from pubsub import pub

import db
import triage

MESHTASTIC_PORT = os.environ.get("MESHTASTIC_PORT")
ACK_PREFIX = "FT "
DEDUPE_WINDOW_SECONDS = 120
MAX_CHUNK_BYTES = 200


def on_receive(packet, interface):
    raise NotImplementedError


def send_ack(node_id: str, ticket_code: str, interface) -> None:
    raise NotImplementedError


def main():
    db.init_db()
    interface = meshtastic.serial_interface.SerialInterface(devPath=MESHTASTIC_PORT)
    pub.subscribe(on_receive, "meshtastic.receive.text")
    print("Firetower gateway listening...")
    try:
        while True:
            pass
    except KeyboardInterrupt:
        interface.close()


if __name__ == "__main__":
    main()
