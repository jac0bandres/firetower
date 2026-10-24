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

python db.py                 # creates data/firetower.db
uvicorn board:app --reload   # coordinator board at http://localhost:8000
python gateway.py            # gateway (needs a Meshtastic node on USB)
```

## What this is not

Not the first AI on Meshtastic. Not "disaster-proof." No medical advice, and
it does not replace or place 911 calls. "Works offline" applies to triage and
the local model only — Snowflake sync, cloud SITREPs, and map tiles need a
connection.

## License

MIT — see [LICENSE](LICENSE).
