# Firetower

Off-grid triage for Meshtastic LoRa mesh messages. People in the field text short
messages over the mesh (no cell/internet required). A gateway laptop receives
every message, triages urgency with a local model, stores it, and shows a
coordinator board sorted by urgency. Triage always runs locally, so it keeps
working with the internet unplugged.

## Architecture

```
Field radio (phone + node A)
   │  LoRa mesh, private channel + PSK
   ▼
Gateway laptop (node B on USB)             gateway.py
   │  dedupe by packet id, hop count stored
   ▼
Triage: keyword net + local model          triage.py   (always local)
   │  priority-node floor applied
   ├──► SQLite firetower.db                db.py
   │       ├──► Coordinator board          board.py + static/ (FastAPI)
   │       └──► Snowflake, when online      sync.py
   └──► Ticket ack DM to sender            gateway.py

router.py: online/offline check; SITREP summaries go to the cloud model
           when online, the laptop model when offline.
```

## Model tiers

| Job | Internet up | Internet down |
|---|---|---|
| Triage + extraction | `qwen3:4b` on the laptop (Ollama) | same |
| SITREP summaries | DigitalOcean serverless open-weight model, home GPU over Tailscale as backup | `qwen3:4b` on the laptop |
| Reports | synced to Snowflake | saved locally, queued |
| Read-aloud voice | ElevenLabs | Piper |

- Backup local model: `llama3.2:3b`.
- Turn qwen3 "thinking" off (`"think": false`, or `/no_think` in the prompt). Temperature 0 for triage.
- The home-GPU tier still needs internet (Tailscale) — it only protects against a provider outage, not a real offline scenario.

## SQLite schema (`db.py`)

```sql
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_code TEXT UNIQUE NOT NULL,       -- e.g. FT-0001
    node_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    received_at TEXT NOT NULL,              -- ISO 8601 UTC
    raw_text TEXT NOT NULL,
    urgency TEXT NOT NULL,                  -- EMERGENCY | PRIORITY | WELFARE | ROUTINE
    location TEXT,
    what_happened TEXT,
    injured_count INTEGER,
    raised_by_keyword INTEGER NOT NULL DEFAULT 0,
    priority_floor_applied INTEGER NOT NULL DEFAULT 0,
    hop_count INTEGER,
    status TEXT NOT NULL DEFAULT 'open',    -- open | assigned | closed
    assigned_at TEXT,
    synced_to_snowflake INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE priority_nodes (
    node_id TEXT PRIMARY KEY,
    role TEXT NOT NULL                      -- e.g. "shelter lead", "registered medical need"
);

CREATE TABLE seen_packets (
    packet_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL             -- dedupe window: 120s
);
```

Open in WAL mode (`PRAGMA journal_mode=WAL;`) since `board.py` polls while
`gateway.py` writes.

## Urgency precedence

`EMERGENCY > PRIORITY > WELFARE > ROUTINE`. A message from a node listed in
`priority_nodes` is never ranked below `PRIORITY`; `EMERGENCY` stays
`EMERGENCY`. The keyword safety net can only raise urgency, never lower it —
if it fires, the ticket is tagged `raised_by_keyword` and shown on the board.

## Radio rules (`gateway.py`)

- Payload limit is **bytes**, not characters: ≤ 200 bytes per chunk, UTF-8, no emoji; max 3 chunks ~3s apart.
- Reply by DM only; never broadcast; never process our own node's messages or anything starting `FT `.
- Dedupe by packet id for ≥ 120s (`seen_packets`).
- Use our own channel + PSK, never the default public channel. Gateway node name should contain "bot".

## Hard rules

- **Firetower never dials 911 itself.** The 911 card only prepares a script for a human to read and logs "I called 911" — it never places a call, in testing or in the demo.
- Voice output (ElevenLabs/Piper) is base-station only, never sent over the mesh.
- Don't claim this is the first AI-on-Meshtastic project, "disaster-proof", or a replacement for 911/medical advice. "Works offline" applies only to triage and the local model, not Snowflake sync, SITREP-via-cloud, or map tiles.

## Dev setup

```
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env         # fill in local values
ollama pull qwen3:4b
ollama pull llama3.2:3b
```

Run the board: `uvicorn board:app --reload`. Run the gateway: `python gateway.py`.

## Conventions

- No CDN links in `static/` — vendor JS/CSS into `static/vendor/` so the board works offline.
- Triage must degrade gracefully: if the JSON-schema extraction from Ollama fails, fall back to keyword-only classification rather than dropping the message.
- `router.py` is the only place that decides online vs. offline; other modules ask it, they don't probe the network themselves.
