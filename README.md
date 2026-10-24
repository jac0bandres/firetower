# Firetower

Off-grid triage for when the towers go down.

People in the field text short messages over Meshtastic LoRa radios (no cell
service or internet needed). A gateway laptop receives every message, and a
**local open-weight model** ranks its urgency (EMERGENCY / PRIORITY / WELFARE
/ ROUTINE) and pulls out where, what happened, and how many are hurt. The
sender gets a ticket number back over the radio. A coordinator board shows
everything sorted by urgency. Triage always runs on the laptop, so it keeps
working with the internet unplugged.

Other mesh AI projects answer the person who asked. Firetower sorts
everyone's messages for the person who has to act, keeps working offline, and
syncs when the link returns.

See [CLAUDE.md](CLAUDE.md) for the full architecture, schema, and dev setup.

## Architecture

```mermaid
flowchart TD
    A["Field radio\nphone + node A"] -->|"LoRa mesh\nprivate channel + PSK"| B["Gateway laptop\nnode B on USB"]
    B --> G["gateway.py\n(long-running process)"]
    G -->|"dedupe by packet_id\n(seen_packets, 120s)"| G
    G --> T["triage.py\nkeyword net + priority floor"]
    T -->|"HTTP, localhost:11434\nformat=schema, think=false"| O[("Ollama\nqwen3:4b / llama3.2:3b")]
    T --> DB[("SQLite\nfiretower.db")]
    G -->|"ticket ack DM\n(FT prefix, <=200B)"| A

    DB --> BOARD["board.py\n(FastAPI, separate process)"]
    BOARD --> UI["Coordinator board\nstatic/"]
    BOARD --> R["router.py\nonline/offline probe"]
    R -->|online| CLOUD[("DigitalOcean /\nhome GPU via Tailscale")]
    R -->|offline| O

    DB -.->|"when online"| S["sync.py"]
    S -.-> SF[("Snowflake")]

    BOARD -.->|"911 call card\nread aloud (optional)"| EL[("ElevenLabs\n(base station only)")]
    EL -.->|"offline fallback"| PIP[("Piper\n(local TTS)")]
```

ElevenLabs/Piper is optional and one-directional: the board calls it to read
the 911 script aloud at the base station. It is never wired into the mesh,
never triggers a call by itself, and is not required for the core triage loop
to work.

## Quickstart

```
python -m venv .venv
source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env
ollama pull qwen3:4b

python db.py                 # creates data/firetower.db for live mode
FIRETOWER_MOCK=1 uvicorn board:app --reload  # demo board with North Georgia GPS pins
python gateway.py            # gateway (needs a Meshtastic node on USB)
```

The board defaults to the real SQLite database (`FIRETOWER_MOCK=0`). Use
`FIRETOWER_MOCK=1` to preview the synthetic tickets and map positions.

Emergency and priority tickets with missing location or injury counts trigger
one follow-up question, phrased by the local model with a static fallback.
The ticket's nullable `clarification_question` and `clarification_response`
fields preserve the original question and sender's raw reply for the
coordinator board, alongside the extracted details. They are included in the
ticket API and mock fixtures. `db.init_db()` safely adds these columns to an
existing database, leaving historical tickets with null values.

`!ai <question>` answers from the sender's conversation and channel records,
including full-channel counts by urgency/status and the 40 most recent detailed
tickets with status, timestamps and clarification exchanges. Zero injuries is
kept distinct from an unknown count; replies are prompted to cite specific
ticket details and identify missing facts.

Local `!ai` replies use Ollama's JSON `format` schema `{"reply": string}`
with `think=False` and temperature 0.2. Replies are JSON-parsed and checked for
a string `reply`, with reasoning text stripped before retrying failed parsing;
invalid or empty answers fall back through the model tiers to a fixed
unavailable message. Clarification questions use `{"question": string}` with
`think=False` and temperature 0. Strict JSON and question validation reject
reasoning text, extra fields, invalid characters, questions of 80 or more
characters, and wording that fails to ask for the missing details; failures
use the static question template. The optional cloud assistant uses plain-text
chat responses.

The gateway listens for Meshtastic `TEXT_MESSAGE_APP` and `POSITION_APP` packets
on `FIRETOWER_CHANNEL_INDEX`. Configure that channel with a private PSK on the
radios. The gateway exits at startup if that channel is missing, disabled, or
uses the unencrypted/public default key. Tickets retain a JSON snapshot of the
text packet and, when a position packet from the same node was received within
30 minutes, its coordinates and position packet. Tickets without a recent
position have null coordinates.

Receipt timestamps use the gateway laptop's UTC clock, so a radio clock that
lags the laptop does not make newly received GPS appear stale. The radio's
original `rxTime` remains in the JSON packet snapshots. Simulator GPS arriving
after a text report can attach to that ticket when `decoded.replyId` explicitly
references its text packet ID. The update requires the same sender and exact
packet ID and both ticket coordinates to be null; existing positions are
preserved, and older tickets are never broadly backfilled.

`fixtures/mock_meshtastic_packets.json` contains synthetic Meshtastic position
and text packets for local development; they are not captured radio
transmissions. Mock GPS positions are clustered in North Georgia and exist only
to demonstrate the map.

The board's topographic map plots only tickets with sender GPS coordinates.
It uses OpenFreeMap for streets and labels, with Mapterhorn hillshade and
contours. The map JavaScript and CSS are bundled under `static/vendor/`, but
the map tiles require internet unless a local tile source is configured.
`FIRETOWER_MOCK=1` includes synthetic GPS positions for every ticket to
demonstrate the map.

## What this is not

Not the first AI on Meshtastic. Not "disaster-proof." No medical advice, and
it does not replace or place 911 calls. "Works offline" applies to triage and
the local model only — Snowflake sync, cloud SITREPs, and map tiles need a
connection.

## License

MIT — see [LICENSE](LICENSE).
